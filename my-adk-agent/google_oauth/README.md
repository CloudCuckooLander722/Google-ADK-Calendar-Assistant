# Google OAuth Credential System — Design & Requirements

## 1. System Overview

Four components, one responsibility each:

| File | Responsibility | Runs in |
|---|---|---|
| `oauth_login.py` | Drive the browser-facing OAuth handshake; identify the user; hand off to storage | Streamlit app |
| `creds_db.py` | Encrypted persistence of one credential row per `user_id` | Shared (imported by both sides) |
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
- Encrypt `access_token`, `refresh_token`, and `client_secret` before writing to disk. `client_id`, `email`, `scopes`, `expiry` may remain plaintext (not bearer-usable on their own).
- `upsert_credentials` must preserve an existing `refresh_token` when the incoming write has none (e.g., a plain access-token refresh cycle). Never let a refresh operation null out a previously stored refresh token.
- `user_id` is the primary key. One row per user, full stop — no per-session rows, no per-browser rows.
- `init_db()` must be idempotent (`CREATE TABLE IF NOT EXISTS`) and safe to call on every app boot.

**Must not:**
- Must not expose raw SQL to callers outside this module. `oauth_login.py` and `backend_example.py` interact with credentials only through `credentials_store.py`, never with `sqlite3` directly.
- Must not be the concurrency bottleneck once traffic grows — SQLite's single-writer lock is acceptable for now (same-machine, moderate load) but is the flagged first thing to swap for Postgres if write contention appears (see §5).

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
| `GOOGLE_CREDS_ENCRYPTION_KEY` | `creds_db.py` | Fernet key, generate once, store in secrets manager / env, never commit |
| `GOOGLE_CREDS_DB_PATH` | `creds_db.py` | Defaults to `google_oauth_creds.db` in CWD — should be set explicitly in production to a persistent volume path |
| `OAUTHLIB_INSECURE_TRANSPORT` | `oauth_login.py` | Dev/Codespaces only — **must be unset in production**, since it disables HTTPS enforcement on the redirect URI |
| `credentials.json` (`CREDS_PATH`) | `oauth_login.py` | Google client secrets file — treat with same sensitivity as `client_secret` |

---

## 5. Known Gaps / Open Issues (not yet handled — flag before shipping)

1. **No re-consent path when `refresh_token` is missing.** If a user's stored row has no refresh token (edge case: they revoked access in their Google account, or the consent screen was skipped some other way) and their access token expires, `get_valid_credentials` currently returns a `Credentials` object that will fail on next actual API call, not a clean "please log in again" signal. Recommend: check `creds.expired and not creds.refresh_token` explicitly and return `None` in that case instead.
2. **No token revocation / logout flow.** `creds_db.delete_credentials(user_id)` exists but nothing in `oauth_login.py` calls it yet. Needed for a logout button and for handling Google-side revocation gracefully (a 401 from `build()` should trigger a DB row deletion and re-auth prompt, not a raw exception).
3. **SQLite concurrency ceiling.** Fine for same-machine, low-to-moderate concurrent writes. If backend function and Streamlit app start fighting over write locks under load, migrate `creds_db.py`'s connection layer to Postgres — the public function signatures (`upsert_credentials`, `get_credentials_dict`, `delete_credentials`) are designed to stay stable across that swap.
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
