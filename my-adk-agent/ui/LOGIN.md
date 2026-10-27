# 1. ui

## 1.1 login.py

### 1.11 `login_page` Requirements

- [] Arguments: OAuthLogin
- [] Brief: Checks if logged in is True for st.query_params
- [] initiates login if logged in is False

### 1.12 `login` Requirements

- [] Arguments: OAuthLogin
- [] Brief: Initializes login link + login and download_creds from OAuthLogin

## 1.2 chat_interface.py

### 1.21 `restore_signed_in_user` requirements

- [] retrieves user_id from cookie_controller defined as cookie_service.controller() 

### 1.22 `current_conversation` requirements

- [] params runner, user_id
- [] retrieves the user's conversation (messages) for this browser tab and displays it in `run_chat_interface`
- [] the tab is identified by a tab id kept in the browser's sessionStorage: a refresh keeps the tab id, so the conversation comes back
- [] a new tab or a later visit gets a new tab id and starts with an empty conversation, which replaces the old one (at most one conversation per user in memory)
- [] the conversation is never saved in a cookie

