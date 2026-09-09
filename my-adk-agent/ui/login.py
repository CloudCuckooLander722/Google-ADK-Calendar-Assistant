import os
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
APP_ROOT = MODULE_DIR.parent
PROJECT_ROOT = APP_ROOT.parent

for base in (str(PROJECT_ROOT), str(APP_ROOT)):
    if base not in sys.path:
        sys.path.insert(0, base)

import streamlit as st
from google_oauth.oauth_login import OAuthLogin


def login():
    """
    Logs in the user and authenticates their credentials.
    """
    authenticator = OAuthLogin()

    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

        .login-wrapper {
            min-height: 84vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background:
                radial-gradient(circle at 72% 16%, rgba(126, 222, 195, 0.13), transparent 18%),
                radial-gradient(circle at 50% 84%, rgba(122, 119, 255, 0.12), transparent 22%),
                linear-gradient(180deg, #eef7f6 0%, #eef3f8 100%);
            font-family: 'Inter', Arial, sans-serif;
            color: #172033;
        }

        .login-card {
            width: min(880px, 92vw);
            display: grid;
            grid-template-columns: 1fr 0.85fr;
            background: rgba(255,255,255,0.84);
            border: 1px solid rgba(255,255,255,0.95);
            border-radius: 36px;
            box-shadow: 0 55px 160px rgba(20,33,61,0.18);
            backdrop-filter: blur(18px);
            overflow: hidden;
        }

        .login-panel-left {
            padding: 50px 50px;
            background: linear-gradient(140deg, #14213d 0%, #223b59 100%);
            color: #eefcf8;
            position: relative;
            min-height: 420px;
        }

        .login-panel-left:after {
            content: '';
            width: 200px;
            height: 200px;
            border-radius: 50%;
            border: 1px solid rgba(255,255,255,0.4);
            position: absolute;
            right: -40px;
            top: -40px;
            box-shadow: inset 0 0 0 14px rgba(255,255,255,0.05);
        }

        .login-panel-left .brand {
            font-size: 28px;
            font-weight: 900;
            letter-spacing: -0.04em;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .login-panel-left .brand span {
            width: 40px;
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            background: #eefcf8;
            color: #14213d;
            font-size: 18px;
            font-weight: 900;
        }

        .login-panel-left .headline {
            margin-top: 56px;
            font-size: clamp(40px, 3vw, 58px);
            line-height: 1.05;
            font-weight: 800;
            letter-spacing: -0.045em;
            max-width: 420px;
        }

        .login-panel-left .tagline {
            margin-top: 22px;
            color: rgba(238,252,248,0.8);
            font-size: 14px;
            line-height: 1.9;
            max-width: 420px;
            font-weight: 600;
        }

        .login-panel-left .mini-grid {
            display: flex;
            gap: 12px;
            margin-top: 50px;
            flex-wrap: wrap;
        }

        .login-panel-left .mini-grid span {
            padding: 8px 12px;
            border-radius: 999px;
            border: 1px solid rgba(238,252,248,0.35);
            font-size: 11px;
            font-weight: 800;
            color: #eefcf8;
            background: rgba(255,255,255,0.03);
        }

        .login-panel-right {
            background: #ffffff;
            padding: 46px 40px;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }

        .login-panel-right .small-label {
            color: #5b687b;
            font-size: 11px;
            font-weight: 900;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            margin-bottom: 16px;
        }

        .login-panel-right h2 {
            color: #14213d;
            font-size: 34px;
            line-height: 1.2;
            font-weight: 800;
            letter-spacing: -0.035em;
            margin-bottom: 14px;
        }

        .login-panel-right p {
            color: #56616f;
            font-size: 14px;
            line-height: 1.8;
            margin-bottom: 24px;
        }

        .login-panel-right .login-button {
            width: 100%;
            background: #14213d;
            color: #eefcf8;
            border: none;
            border-radius: 14px;
            padding: 14px 20px;
            font-size: 14px;
            font-weight: 900;
            letter-spacing: 0.08em;
            cursor: pointer;
        }

        .login-panel-right .login-button:hover {
            background: #223b59;
        }

        .login-panel-right .terms {
            margin-top: 20px;
            color: #56616f;
            font-size: 11px;
            font-weight: 600;
            line-height: 1.8;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="login-wrapper">
            <div class="login-card">
                <section class="login-panel-left">
                    <div class="brand"><span>W</span>WorkFlow</div>
                    <div class="headline">Plan clearer. Work deeper.</div>
                    <div class="tagline">Turn your schedule into a simple operating system for your priorities.</div>
                    <div class="mini-grid">
                        <span>Calendar</span>
                        <span>Tasks</span>
                        <span>Focus</span>
                    </div>
                </section>
                <section class="login-panel-right">
                    <div class="small-label">Workspace access</div>
                    <h2>Welcome back</h2>
                    <p>Connect your Google Workspace to begin planning your calendar and task system.</p>
                    <div class="login-button-wrapper"></div>
                    <div class="terms">Secure sign-in with Google OAuth</div>
                </section>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Keep the authorized Google OAuth flow intact while showing a polished card interface.
    authenticator.login()
    creds = authenticator.get_creds()
    if creds is not None:
        st.session_state.logged_in = True
   