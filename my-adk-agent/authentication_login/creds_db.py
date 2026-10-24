"""
Handles google db stuff for later.
"""
import os
from google.cloud import firestore
from cryptography.fernet import Fernet

@st.cache_resource
def get_db():
    return firestore.Client(project=os.environ.get("PROJECT_ID"))




    