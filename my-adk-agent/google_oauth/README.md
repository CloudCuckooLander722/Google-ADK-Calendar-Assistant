# Google OAuth Credential System — Design & Requirements

## 1. System Overview

Four components, one responsibility each:

| File | Responsibility | Runs in |
|---|---|---|
| `oauth_login.py` | Drive the browser-facing OAuth handshake; identify the user; hand off to storage | Streamlit app |
| `creds_db.py` | Encrypted persistence of one credential document per `user_id` (Firestore) | Shared (imported by both sides) |
| `credentials_store.py` | Single source of truth for "give me a *valid* Credentials object for this user" (includes refresh) | Shared (imported by both sides) |
| `backend_example.py` | Consume credentials to call Google APIs | Backend function (separate process) |

**Core architectural rule:** the browser cookie holds only a `user_id` (a pointer). It never holds token material. Anything that needs actual credentials — Streamlit app or backend — goes through `credentials_store.get_valid_credentials(user_id)`, which reads from `creds_db`. This is what makes the backend function possible at all, since it has no browser and can't read cookies.

---

## 2. Component Responsibilities & Requirements

### 2.1 `oauth_login.py` (OAuthLogin class)

**Must:**
- Build the Google consent URL (`init_google_auth`) with `prompt='consent'` so a refresh token is issued on every login, not just the first.
- Store `oauth_state` in a cookie before redirecting to Google, and validate it matches on return (CSRF protection — already handled by passing `state=saved_state` into `Flow`).
- On successful `fetch_token()`, decode the ID token to extract `sub` (→ `user_id`) and `email`. Must not proceed to storage if `id_token` is missing (means `openid` scope wasn't granted).
- Call `creds_db.upsert_credentials(user_id, creds, email)` before setting any cookie — storage is the source of truth; the cookie is just a pointer to it.
- Set only `user_id` in the cookie after successful auth. Never write `access_token`, `refresh_token`, `client_secret`, etc. into cookies.
- Clear `oauth_state` cookie and `st.query_params` after use, so a page refresh doesn't attempt to replay the same authorization code (Google authorization codes are single-use; a replay will throw).
- On repeat visits, `get_creds()` must check for `user_id` in cookies **before** checking `st.query_params` for a `code` — an already-authenticated user should never be routed back through the token exchange branch.

**Must not:**
- Must not assume `cookies.get_all()` is populated on the very first Streamlit rerun — the cookie manager iframe loads asynchronously. The existing `st.stop()` guard on empty cookies is required, not optional.
- Must not catch-and-swallow exceptions from `fetch_token()` beyond logging — a silent `None` return with no user-visible cause makes this undebuggable in production. At minimum, log `str(e)` server-side even though `st.error` shows it client-side too.

### 2.2 `creds_db.py`

**Must:**
- Fail loudly at import time if `GOOGLE_CREDS_ENCRYPTION_KEY` is unset (already implemented as a `RuntimeError`). Silent fallback to an unencrypted mode is explicitly disallowed.
- Encrypt `access_token`, `refresh_token`, and `client_secret` before writing to Firestore. `client_id`, `email`, `scopes`, `expiry` may remain plaintext (not bearer-usable on their own).
- `upsert_credentials` must preserve an existing `refresh_token` when the incoming write has none (e.g., a plain access-token refresh cycle) — done via a Firestore `merge=True` write that omits the field entirely rather than writing `None`. Never let a refresh operation null out a previously stored refresh token.
- `user_id` is the document ID. One document per user, full stop — no per-session documents, no per-browser documents.
- `init_db()` is a no-op kept for interface compatibility (Firestore has no schema to create); safe to call on every app boot.

**Must not:**
- Must not expose the raw Firestore client to callers outside this module. `oauth_login.py` and `backend_example.py` interact with credentials only through `credentials_store.py`, never with `google.cloud.firestore` directly.
- Must not assume a single-writer bottleneck the way the previous SQLite-file version did — Firestore is shared across every Cloud Run instance and scales writes independently, which is exactly the property Cloud Run's stateless/ephemeral instances require (see §5).

### 2.3 `credentials_store.py`

**Must:**
- Be the *only* place that calls `.refresh()` on a `Credentials` object. Both `oauth_login.py` (on returning-user path) and `backend_example.py` call `get_valid_credentials(user_id)` — neither reimplements refresh logic.
- Reattach `expiry` after reconstructing `Credentials` from stored fields — `google-auth`'s `.expired` property depends on it being set; skipping this makes every credential look permanently valid (or permanently expired, depending on library defaults), silently breaking refresh.
- Persist the refreshed token back to `creds_db` immediately after a successful `.refresh()` call, so the next caller (either side) doesn't redundantly refresh again.
- Return `None` (not raise) when no stored credentials exist for a `user_id` — callers are expected to handle "user hasn't logged in" as a normal case, not an exceptional one.

**Must not:**
- Must not attempt a refresh when `refresh_token` is `None` — this happens if a user's first login didn't include `prompt='consent'` and Google withheld the refresh token. Calling `.refresh()` in that state will raise; the current guard (`creds.expired and creds.refresh_token`) is required to avoid that, but note it means such a user's access will simply die silently once the access token expires — they need to be routed back through full re-auth. (See open issue in §5.)

### 2.4 `backend_example.py` (and any future backend function)

**Must:**
- Only ever call `credentials_store.get_valid_credentials(user_id)` — never read `creds_db` fields directly, never touch cookies (it has none available), never reconstruct `Credentials` by hand.
- Treat a `None` return as "this user must complete browser-based OAuth again" and fail with a clear, actionable error rather than a bare `NoneType` exception further down in `build()`.
- Receive `user_id` from whatever triggers it (scheduled job payload, API call, queue message, etc.) — this system does not define how `user_id` gets to the backend function, only that it's the sole required input.

---

## 3. End-to-End Sequence

### 3.1 First-time login
1. User hits the Streamlit app → `OAuthLogin().get_creds()` → no `user_id` cookie, no `code` in query params → returns `None` → app shows the login link built from `init_google_auth()`.
2. User clicks through Google consent (forced via `prompt='consent'`) → Google redirects back with `?code=...&state=...`.
3. `get_creds()`: validates `state`, calls `fetch_token()`, decodes ID token → `user_id`, `email`.
4. `creds_db.upsert_credentials(user_id, creds, email)` writes the encrypted row.
5. Cookie manager sets `user_id` cookie. Query params cleared, `oauth_state` cookie deleted.
6. `creds` returned directly to the caller for immediate use in this session (no round-trip to DB needed on the very first pass).

### 3.2 Returning user (same browser, valid cookie)
1. `get_creds()` finds `user_id` in cookies.
2. Calls `credentials_store.get_valid_credentials(user_id)` → DB row found → decrypted → `Credentials` reconstructed → refreshed if expired → returned.
3. No Google redirect, no consent screen.

### 3.3 Backend function (any time, no browser involved)
1. Backend function receives a `user_id` from its own trigger source.
2. Calls the same `get_valid_credentials(user_id)`.
3. Gets back a valid `Credentials` object (refreshed if needed, transparently).
4. Passes it to `build("calendar", "v3", credentials=creds)`.

---

## 4. Environment / Configuration Requirements

| Variable | Required by | Notes |
|---|---|---|
| `GOOGLE_CREDS_ENCRYPTION_KEY` | `creds_db.py` | Fernet key, generate once, store in Secret Manager / env, never commit |
| `PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` | `creds_db.py` | GCP project hosting the Firestore database; falls back to ADC's default project if unset |
| `FIRESTORE_CREDS_COLLECTION` | `creds_db.py` | Optional, defaults to `users_google_creds` |
| `OAUTH_REDIRECT_URI` | `oauth_login.py` | Required in production (no dev fallback) — must exactly match a redirect URI registered on the OAuth client in Google Cloud Console |
| `OAUTHLIB_INSECURE_TRANSPORT` | `oauth_login.py` | Dev/Codespaces only — **must be unset in production**, since it disables HTTPS enforcement on the redirect URI |
| `GOOGLE_CREDENTIALS_PATH` | `oauth_login.py` | Path to the Google client-secrets JSON — required in production, typically a Secret Manager secret mounted as a volume |

---

## 5. Known Gaps / Open Issues (not yet handled — flag before shipping)

1. **No re-consent path when `refresh_token` is missing.** If a user's stored row has no refresh token (edge case: they revoked access in their Google account, or the consent screen was skipped some other way) and their access token expires, `get_valid_credentials` currently returns a `Credentials` object that will fail on next actual API call, not a clean "please log in again" signal. Recommend: check `creds.expired and not creds.refresh_token` explicitly and return `None` in that case instead.
2. **No token revocation / logout flow.** `creds_db.delete_credentials(user_id)` exists but nothing in `oauth_login.py` calls it yet. Needed for a logout button and for handling Google-side revocation gracefully (a 401 from `build()` should trigger a DB row deletion and re-auth prompt, not a raw exception).
3. **~~SQLite concurrency ceiling~~ (resolved).** `creds_db.py` now stores credentials in Firestore rather than a local SQLite file, so every Cloud Run instance (and the backend function) shares one consistent, durable store instead of each instance having its own invisible copy. The public function signatures (`upsert_credentials`, `get_credentials_dict`, `delete_credentials`) were kept stable across the swap.
4. **No audit trail.** `updated_at` exists but nothing logs *who* refreshed a token *when* for security review purposes. Low priority unless this handles sensitive calendars.

---

## 6. Testing Checklist

- [ ] First-time login writes exactly one row to `users_google_creds`, with `refresh_token` non-null.
- [ ] Second login (same Google account) does not create a duplicate row (upsert on `user_id`).
- [ ] Killing the Streamlit session and returning later (cookie persists) skips the consent screen entirely.
- [ ] Manually expiring a stored `expiry` timestamp in the DB and calling `get_valid_credentials` triggers a real `.refresh()` and updates the row.
- [ ] Backend function, called with a `user_id` that has never logged in, returns/raises a clear error rather than crashing inside `build()`.
- [ ] Deleting `GOOGLE_CREDS_ENCRYPTION_KEY` from the environment causes `creds_db.py` to fail at import time, not at first write.
- [ ] Inspecting the raw SQLite file confirms `access_token` / `refresh_token` / `client_secret` are not readable as plaintext.

# Streamlit Login Page UI

## System Overview

### 1.

main.py

-> Responsible for redirecting a user to a chat_interface after clicking on a login page.

chat_interface.py

-> Primary function displaying the chat_interface.

login.py

Logs in the user, uses oauth_login.OAuthLogin to authenticate credentials / store them in SQLite database for further usage.

workflow

check for st.session_state["logged_in"] = True

if not

then use login()

else

then use chat_interface()

#Credentials And Services

##System Overview

###1.

Get creds from oauth_login.creds_db import get_credentials_dict

Output: build() function from get_calendar_service and get_tasks_service

## Calendar and Tasks API Requirements

This section defines the behavioral contract for the calendar tools and the
Google API services they use. The requirements are intentionally stated so
that each one can be verified by a test or an observable API request.

### 7.1 Common Preconditions

Every calendar or task operation must satisfy these conditions before it
calls Google:

- `get_calendar_service()` or `get_tasks_service()` must return an
	authenticated service. A missing credential must produce an actionable
	authentication error; it must not become an unexplained `NoneType` failure.
- The user's IANA timezone (for example, `America/New_York`) must be present
	in Streamlit session state or be obtainable from the browser.
- Natural-language dates must be parsed in the user's timezone, not the
	server's timezone.
- API failures must be converted into a `ValueError` that identifies the
	failed operation while retaining the original error as its cause.
- User input must be validated before the API request is constructed.

### 7.2 Date and Time Representation

The parser returns local values. It must not silently convert a user's local
wall-clock time to UTC before returning it.

| Meaning | Accepted representation | Google Calendar field |
|---|---|---|
| All-day date | `YYYY-MM-DD` | `date` |
| Timed local value | `YYYY-MM-DDTHH:MM:SS+/-HH:MM` | `dateTime` plus `timeZone` |

Requirements:

- A value matching exactly `YYYY-MM-DD` is an all-day date. It must not be
	sent as a Calendar `dateTime`.
- A timed value must be timezone-aware or must be localized using the user's
	timezone before it is sent to Google.
- Invalid dates, invalid offsets, and malformed timestamps must be rejected
	before the API request.
- Calendar all-day end dates are exclusive. For a one-day event, the end date
	must be the following local date.
- A timed event's duration must be computed in the user's timezone. Daylight
	saving transitions must not change the displayed local start time or create
	an invalid offset.
- Search and free/busy requests may be converted to UTC at the API boundary,
	but returned and displayed values must be converted back to the user's
	timezone.

### 7.3 Calendar Event Requirements

#### Create

`create_event` must:

- Require a non-empty summary, valid start, and valid end.
- Use `start.date` and `end.date` for all-day events.
- Use `start.dateTime`, `end.dateTime`, and the user's `timeZone` for timed
	events.
- Preserve optional location, description, recurrence, and attendees only
	when supplied.
- Return the created event link or a stable event identifier.
- Never place access tokens, refresh tokens, or client secrets in the event
	body or the returned message.

#### Patch

`update_event` must:

- Require an event ID and at least one changed field.
- Send only fields explicitly provided by the caller.
- Apply the same `date` versus `dateTime` rules as event creation.
- Use the requested attendee notification policy and default to no
	notifications unless attendees may be affected.

#### Delete

`delete_event` must:

- Require an event ID.
- Delete only the requested calendar event.
- Return a clear success message and convert API errors to an actionable
	application error.

#### Search and list

`search_events` and `list_events` must:

- Use RFC3339 bounds for `timeMin` and `timeMax`.
- Expand recurring events when displaying individual occurrences.
- Format timed results in the user's local timezone, including the timezone
	abbreviation.
- Display all-day results as local dates without inventing a time.
- Include the event ID in results so a later patch or delete is unambiguous.

### 7.4 Google Tasks Requirements

Google Tasks has a different due-date contract from Google Calendar: it has a
single `due` RFC3339 timestamp and no separate all-day `date` field.

#### Create

`create_task` must:

- Require a non-empty title.
- Accept optional notes and a task-list ID, defaulting to `@default`.
- Convert a date-only due value such as `2026-09-07` to local midnight with
	the user's timezone offset.
- Preserve the local timezone offset for timed due values.
- Send `title`, `notes` when present, and `due` when present, with no
	unrelated fields.
- Return the task title and task ID after a successful insert.

#### Patch

`patch_task` must:

- Require a task ID and at least one changed field.
- Send only changed fields: `title`, `notes`, `due`, or `status`.
- Normalize `due` using the same local-date and local-time rules as creation.
- Permit completion through `status="completed"` and reopening through
	`status="needsAction"`.
- Define a separate explicit operation if clearing an existing due date is
	required; an omitted due value must mean "leave unchanged," not "clear."

#### Delete

`delete_task` must require both a task ID and the target task list ID, then
return a clear success result after the API confirms deletion.

#### Search

`search_tasks` must:

- Use the Tasks API `q` parameter for title/notes search when a query is
	provided.
- Return task title, status, and task ID for every result.
- Support completed and incomplete tasks explicitly.
- Return a stable no-results message rather than an empty or ambiguous
	response.
- Avoid exposing token material or other credential fields in results.

### 7.5 Service and Identity Requirements

- Calendar and Tasks services must use the same authenticated user selected by
	the OAuth `user_id` pointer.
- `get_calendar_service` and `get_tasks_service` must obtain credentials only
	through `credentials_store.get_valid_credentials`; callers must not rebuild
	credentials or read encrypted database fields directly.
- A user ID must never be accepted from untrusted event/task content. It must
	come from the authenticated session or a trusted backend request context.
- The OAuth scope set must include the minimum Calendar and Tasks scopes
	required by the operations enabled in the agent.
- All user-facing confirmations must use local timezone formatting and must
	distinguish all-day dates from timed values.

### 7.6 Verification Checklist

- [ ] Creating a timed event at `09:00` in `America/New_York` sends a local
	offset value and displays `09:00`, not a UTC-shifted time.
- [ ] Creating an all-day event sends `start.date` and `end.date`, not
	`start.dateTime` or `end.dateTime`.
- [ ] Patching only an event title leaves its dates and attendees unchanged.
- [ ] Searching an event returns its ID and formats timed results locally.
- [ ] Creating a task with `2026-09-07` stores a due timestamp at local
	midnight with the correct offset.
- [ ] Creating a task with an explicit time preserves that local time.
- [ ] Patching a task with only `status="completed"` changes no other field.
- [ ] Searching tasks can return both completed and incomplete tasks.
- [ ] Deleting an event or task with an unknown ID returns a clear failure.
- [ ] A missing or expired credential produces a re-authentication path,
	rather than an API call with invalid credentials.
