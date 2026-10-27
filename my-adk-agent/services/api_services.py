import logging
from google.auth.transport.requests import Request
import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)
    
from authentication_login.creds_db import get_valid_credentials

def check_creds():
    """
    Evaluates the quality of the creds.
    """
    creds = get_valid_credentials()

    if not creds:
        raise ValueError("No credentials.")
        return None

    try:
        if creds and creds.expired and creds.refresh_token():
            creds.refresh(Request())
            
        return creds
    except Exception as e:
        st.error(f"Error parsing saved credentials: {e}")
        return None

def get_tasks_service():
    """
    Returns build object enabling access to creds.
    """
    creds = check_creds()

    try:
            
        return build('tasks', 'v1', credentials=creds)
        
    except Exception as e:
        st.error(f"Error parsing saved credentials: {e}")
        return None

def get_calendar_service():
    """
    Returns build() object enabling access to calendar.
    """
    creds = check_creds()

    try:
            
        return build('calendar', 'v3', credentials=creds)
        
    except Exception as e:
        st.error(f"Error parsing saved credentials: {e}")
        return None

def get_drive_service():
    """
    Returns build() object enabling access to drive.
    """
    creds = check_creds()

    try:
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        st.error(f"Error parsing saved credentials: {e}")
        return None