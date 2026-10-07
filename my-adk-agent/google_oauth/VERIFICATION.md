# Google OAuth Verification Guide

How to get WorkFlow's Google sign-in verified, so users don't see the
"Google hasn't verified this app" warning and the 100-user cap is lifted.

WorkFlow asks for `calendar` and `tasks`, which Google classes as **sensitive**
scopes, and `drive.file` (only files the user picks), which is **non-sensitive**. Sensitive scopes need a manual review (usually a few days to a few weeks),
but no paid security assessment. Adding a **restricted** scope such as
`drive.readonly` later would require one, so avoid that if you can.

Project: `gen-lang-client-0565764733` · Cloud Run service: `workflow` (us-west1)

---

## Step 1: Get a domain you own

Google won't accept `*.run.app` as an authorized domain, because you can't prove
you own it. You need your own domain (for example `workflowplanner.app`, bought from
any registrar, usually $10–20/year).

1. Buy the domain.
2. Verify it in [Google Search Console](https://search.google.com/search-console)
   with the **same Google account** that owns the Cloud project
   (mt9312025@gmail.com). Use the "Domain" property type and add the TXT record it
   gives you at your registrar.

## Step 2: Point the domain at Cloud Run

Cloud Run domain mappings work in us-west1. Use a subdomain such as
`app.<your-domain>`:

```bash
gcloud components install beta
gcloud beta run domain-mappings create \
  --service=workflow --domain=app.<your-domain> \
  --region=us-west1 --project=gen-lang-client-0565764733
```

Add the DNS records the command prints (a CNAME to `ghs.googlehosted.com.` for a
subdomain) at your registrar. The HTTPS certificate is issued automatically; it can
take up to a day.

> Firebase Hosting is **not** an option here: it can't proxy the WebSocket
> connection Streamlit needs.

## Step 3: Move sign-in to the new domain

1. **OAuth client:** Google Cloud Console → Google Auth Platform → Clients → your
   web client → add `https://app.<your-domain>/` under *Authorized redirect URIs*.
   Keep the old run.app URI until the switch is done.
2. **Cloud Run:** point the app's redirect at the new domain:
   ```bash
   gcloud run services update workflow --region=us-west1 \
     --project=gen-lang-client-0565764733 \
     --update-env-vars=OAUTH_REDIRECT_URI=https://app.<your-domain>/
   ```
3. Sign in at `https://app.<your-domain>/` to check it works.

## Step 4: Fill in the consent screen

Google Cloud Console → **Google Auth Platform**:

**Branding**

| Field | Value |
|---|---|
| App name | WorkFlow |
| User support email | mt9312025@gmail.com |
| App logo | 120×120 px PNG/JPG (optional, but adding one also triggers brand review) |
| Application home page | `https://app.<your-domain>/` |
| Application privacy policy link | `https://app.<your-domain>/privacy` |
| Application terms of service link | optional |
| Authorized domains | `<your-domain>` |
| Developer contact email | mt9312025@gmail.com |

**Data Access** (scopes): `openid`, `.../auth/userinfo.email`,
`.../auth/calendar`, `.../auth/tasks`, `.../auth/drive.file`. These must match `SCOPES` in
`oauth_login.py` exactly; remove anything else listed there.

**Audience:** set *Publishing status* to **In production**.

Then open the **Verification Center** and click **Prepare for verification** /
**Submit**.

## Step 5: Scope justifications (paste into the form)

**`https://www.googleapis.com/auth/calendar`**

> WorkFlow is an AI scheduling assistant. Users manage their Google Calendar by
> chatting with it: "schedule a study block Friday at 3pm", "move my meeting to
> Tuesday", "delete the 4pm event", "what's on this week?". The app needs to list the
> user's calendars (to put an event on the calendar that matches its topic), read
> events (to show them in the app's calendar view and answer questions), create,
> update, and delete events when the user asks, and query free/busy to suggest
> meeting times. A read-only scope would not allow creating or changing events,
> which is the app's core feature. `calendar.events` alone is not enough, because the
> app also lists the user's calendars and queries free/busy across them.

**`https://www.googleapis.com/auth/tasks`**

> Users create and manage Google Tasks through the same chat (for example "add a
> task to finish my essay by Oct 21", "mark that done"). The app shows tasks with due
> dates on its calendar view and creates, updates, and deletes tasks when the user
> asks. `tasks.readonly` would not allow creating or updating tasks.

**`https://www.googleapis.com/auth/drive.file`**: non-sensitive. Users attach
Google Drive files to a chat message through the Google Picker; the app downloads
only the files they pick and sends them to Gemini with that message. It cannot see
any other Drive file.

**`openid` / `userinfo.email`**: non-sensitive; used to identify the user so their
encrypted tokens can be stored and found again.

## Step 6: Demo video

Upload to YouTube as **unlisted** and paste the link into the form. Google rejects
videos that skip any of these, so record them in this order (3–5 minutes):

1. Show the browser address bar with `https://app.<your-domain>/` (the homepage and
   its Privacy Policy link).
2. Click **Log in with Google**. On the consent screen, show the **app name** and the
   **full URL** in the address bar, including the `client_id=` parameter; zoom in if
   needed.
3. Grant the permissions, and show the list of requested scopes.
4. **Calendar:** ask the chat to create an event, show it appear in the app's calendar
   and in Google Calendar; then update it and delete it. Ask "suggest a 1-hour meeting
   time tomorrow" to show free/busy use.
5. **Tasks:** ask the chat to create a task with a due date, show it on the calendar
   view and in Google Tasks; then mark it done or delete it.
6. Open `/privacy` and show the **Delete my data** option.

## Step 7: After submitting

- Google emails the developer contact. Reply in the same email thread; reviewers
  often ask for a re-recorded video or a wording change in the privacy policy.
- Keep the app, the privacy policy (`ui/privacy.py`), and the scope list in sync. If
  you change what data is used or where it goes, update the policy *before*
  deploying, and re-verify if you add scopes.

## While you wait

The app keeps working. In **Testing** status, only listed test users (up to 100) can
sign in, and their sign-ins expire after 7 days. In **In production** but unverified,
anyone can sign in after clicking through the warning, up to 100 users.
