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
from ui.privacy import CONTACT_EMAIL, EFFECTIVE_DATE

TERMS_OF_SERVICE = f"""
#WorkFlow Assistant Terms of Service 

## 1. Acceptance of Terms

By accessing or using WorkFlow Assistant, or any related services provided by the application, you consent to these Terms of Service. If you do not agree to these terms, please refrain from using the application.

## 2. How To Use This Service

You may use WorkFlow Assistant for any lawful and legal purpose in the jurisdiction of your residence and home country. You may not use the application for any illegal or unauthorized purposes.

## 3. Age Limits and User Responsibilities

You must be at least the age of 13 to use WorkFlow Assistant. You are inherently responsible for proper and appropriate usage of the services and features falling under WorkFlow Assistant. You are also responsible for any content you create, share, or interact with while using the application.

## 4. Privacy and Data Collection

Please refer to our Privacy Policy for information on how we collect and use your personal information. By using WorkFlow Assistant, you consent to the collection and use of data as described in our Privacy Policy.

## 5. Contact Information

Any further questions or concerns can be directed to the developer at {CONTACT_EMAIL}.
"""

def show_terms_of_service_page():
    st.markdown(
    """
    <style>
    /* Single optimized import string for both fonts */
    @import url('https://googleapis.com');

    /* Global Headings: Playfair Display */
    h1, h2, h3, .playfair-title {
        font-family: 'Playfair Display', Georgia, serif !important;
    }
    
    /* Global Body Paragraphs: Montserrat */
    p, .stMarkdown p, .montserrat-body {
        font-family: 'Montserrat', sans-serif !important;
        font-size: 16px;
        line-height: 1.6;
    }
    </style>
    """,
    unsafe_allow_html=True
    )

    st.set_page_config('<p class="playfair-title">WorkFlow Terms of Service</p>', layout="centered")

    st.markdown(f'<p class="montserrat-body">{TERMS_OF_SERVICE}</p>', unsafe_allow_html=True)