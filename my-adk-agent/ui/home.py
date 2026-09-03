import streamlit as st
PREMISE = """
Scheduling is essential. But I think it can be less of a pain point for busy people. Meet WorkFlow, an AI-powered application scheduling your meetings or study sessions so you don't have to.
"""
def show_home_page():
    """
    Displays the home page of the application.
    """
    st.set_page_config(page_title="About WorkFlow", layout="wide")
    st.title("About WorkFlow")
    st.markdown(PREMISE)
    



