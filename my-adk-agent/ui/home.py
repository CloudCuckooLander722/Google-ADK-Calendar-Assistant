import streamlit as st

PREMISE = """
WorkFlow is an AI scheduling workspace that helps people plan their calendars, protect attention, and turn scattered commitments into a calmer rhythm.
"""


def show_home_page():
    """
    Displays a plain WorkFlow about page.
    """
    st.set_page_config(page_title="WorkFlow", layout="wide")

    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Space+Grotesk:wght@700&display=swap');

        .workflow-about {
            min-height: 100vh;
            padding: 40px 60px;
            background:
                radial-gradient(circle at 72% 16%, rgba(126, 222, 195, 0.14), transparent 18%),
                radial-gradient(circle at 20% 88%, rgba(122, 119, 255, 0.11), transparent 20%),
                linear-gradient(180deg, #eef7f6 0%, #eef3f8 100%);
            color: #172033;
            font-family: 'Inter', Arial, sans-serif;
        }

        .workflow-about-wrap {
            max-width: 980px;
            margin: 0 auto;
            padding: 30px 20px;
        }

        .workflow-about-tag {
            font-size: 12px;
            font-weight: 900;
            letter-spacing: 0.16em;
            color: #005a4b;
            text-transform: uppercase;
            margin-bottom: 12px;
        }

        .workflow-about h1 {
            font-size: clamp(52px, 6vw, 88px);
            line-height: 0.96;
            letter-spacing: -0.052em;
            font-family: 'Space Grotesk', 'Inter', Arial, sans-serif;
            color: #14213d;
            margin: 0 0 24px;
        }

        .workflow-about p {
            font-size: 19px;
            line-height: 1.8;
            color: #56616f;
            max-width: 780px;
            margin-bottom: 26px;
        }

        .workflow-about-list {
            padding: 0;
            margin: 0 0 30px;
            list-style: none;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
        }

        .workflow-about-list li {
            padding: 10px 16px;
            border-radius: 999px;
            border: 1px solid rgba(20, 33, 61, 0.14);
            color: #14213d;
            font-size: 12px;
            font-weight: 800;
            background: rgba(255,255,255,0.44);
        }

        .workflow-about footer {
            font-size: 12px;
            font-weight: 800;
            color: #56616f;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            border-top: 1px solid rgba(20,33,61,0.1);
            padding-top: 18px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="workflow-about">
            <div class="workflow-about-wrap">
                <div class="workflow-about-tag">About WorkFlow</div>
                <h1>Turn pressure into momentum.</h1>
                <p>WorkFlow is an AI scheduling workspace built to help people plan calendars, protect attention, and manage your schedules with less friction. It turns scattered obligations into a clear operating rhythm for better decisions, better focus, and better days.</p>
                <ul class="workflow-about-list">
                    <li>Plan the week</li>
                    <li>Shape your day</li>
                    <li>Protect focus</li>
                    <li>Use WorkFlow</li>
                </ul>
                <footer>Calendar OS • Tasks • Planning</footer>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

