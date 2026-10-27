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

### 1.22 `retrieve_conversation` requirements

- [] params user_id
- [] retrieves conversation (messages) from cookie_controller and displays it in `run_chat_interface`

