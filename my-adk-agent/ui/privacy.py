import logging
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

import streamlit as st

CONTACT_EMAIL = "mt9312025@gmail.com"
EFFECTIVE_DATE = "October 7, 2026"

# Keep this text in step with the code: it is what Google's OAuth reviewers
# check the app against. Update it whenever scopes, storage, or the services
# user data is sent to change.
POLICY = f"""
# WorkFlow Assistant Privacy Policy

**Effective date:** {EFFECTIVE_DATE}

WorkFlow is an AI scheduling assistant run by an individual developer. It connects
to your Google Calendar and Google Tasks so you can plan, create, and change events
and tasks by chatting with it. This policy explains what WorkFlow accesses, why, and
what happens to it.

## What WorkFlow accesses

When you sign in with Google, you are asked to grant:

| Permission | What WorkFlow does with it |
|---|---|
| Your basic profile: Google account ID and email address (`openid`, `userinfo.email`) | Identifies you, so your sign-in can be kept and your stored tokens can be found again. |
| Google Calendar (`calendar`) | Lists your calendars and shows your events; creates, updates, and deletes events **when you ask it to in the chat**; checks free/busy times to suggest meeting slots. |
| Google Tasks (`tasks`) | Shows your tasks; creates, updates, and deletes tasks **when you ask it to in the chat**. |
| Google Drive, only files you pick (`drive.file`) | Opens a file **only after you choose it in the Google Drive picker** and send it with a chat message, so the assistant can read it. WorkFlow cannot see, list, or change any other file in your Drive, and never changes or deletes the files you pick. |

WorkFlow only changes your calendar or tasks in response to your own requests in the
chat. It does not act on your data in the background.

You can also give WorkFlow content directly: chat messages, screenshots, documents,
voice recordings, and Google Drive files you attach to a message.

## What WorkFlow stores

- **Sign-in tokens.** Your Google access and refresh tokens are stored **encrypted**
  in Google Cloud Firestore, together with your Google account ID, email address, the
  permissions you granted, and the token expiry time. They are kept until you delete
  your data (see below), so you stay signed in.
- **Your calendar events, tasks, and Google Drive files are not stored by WorkFlow.**
  They are read from Google each time they are shown or needed, and stay in your
  Google account.
- **Chat history and attachments** are kept only in the server's memory for your
  current browser session. They are not written to a database and are lost when the
  session ends or the server restarts.
- **Error logs.** When something fails, the app writes an error log to Google Cloud
  Logging. A log entry may include details of the failed request, such as an event
  title or date. Logs are kept for up to 30 days and are used only to fix problems.
- **Cookies.** WorkFlow sets cookies in your browser to keep you signed in
  (`user_id`, `logged_in`) and to protect the sign-in process (`oauth_state`,
  `oauth_code_verifier`). They expire after one day. WorkFlow also reads your
  browser's time zone to show times correctly. It does not use advertising or
  tracking cookies.

## Services your data is shared with

WorkFlow runs on Google Cloud and uses Google services to work. Data is sent only to:

- **Google Gemini API**: your chat messages and attachments, plus the calendar and
  task details needed to answer them, are sent to Gemini, the AI model that powers the
  assistant. WorkFlow uses the paid Gemini API, under which Google does not use
  prompts or responses to improve its products.
- **Google Maps Platform**: place names or addresses you mention, to look up a real
  address for an event's location.
- **Google Search**: when you ask for planning help, the assistant may search the web
  for relevant facts. Search queries are written by the assistant from your request.
- **Google Cloud** (Cloud Run, Firestore, Cloud Logging, Secret Manager): hosting,
  token storage, and error logs, as described above.

WorkFlow does **not** sell your data, show ads, or share your data with anyone else,
and does not use your Google data to train AI models.

## Google API Services User Data Policy

WorkFlow's use and transfer to any other app of information received from Google APIs
will adhere to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Deleting your data

You can disconnect WorkFlow and delete your data at any time:

1. **On this page:** while signed in, use **Delete my data** below. This revokes
   WorkFlow's access to your Google account, deletes your stored tokens, and signs
   you out.
2. **From your Google account:** remove WorkFlow at
   [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
   This stops all access; your stored tokens become unusable, and you can still ask
   for them to be deleted.
3. **By email:** write to [{CONTACT_EMAIL}](mailto:{CONTACT_EMAIL}) and your data will
   be deleted within 30 days.

Deleting your WorkFlow data does not delete events or tasks in your Google Calendar
or Google Tasks.

## Security

Tokens are encrypted before they are stored, secrets are kept in Google Secret
Manager, and all traffic uses HTTPS.

## Children

WorkFlow is not directed to children under 13 and does not knowingly collect their
data.

## Changes to this policy

If this policy changes, the new version will be posted on this page with a new
effective date.

## Contact

Questions or requests: [{CONTACT_EMAIL}](mailto:{CONTACT_EMAIL})
"""


def show_privacy_page():
    """Public privacy policy page, with a delete-my-data control for signed-in users."""
    st.set_page_config(page_title="WorkFlow Privacy Policy", layout="centered")
    st.markdown(POLICY)

    st.divider()
    