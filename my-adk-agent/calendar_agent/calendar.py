import datetime
import os.path
import re
from dateutil import parser as dateutil_parser
import dateparser
import pytz
from tzlocal import get_localzone
from typing import Optional, List, Dict 

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.adk.agents import Agent
from google.genai import types

calendar_agent = Agent(
    name="calendar_agent",
    model="gemini-2.0-flash",
    description="schedules events and creates tasks",
    instruction=ROOT_INSTRUCTIONS,
    tools=[create_event]
)

def create_event():
    pass

def delete_event():
    pass

