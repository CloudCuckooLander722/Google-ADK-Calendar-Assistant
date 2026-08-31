import streamlit as st
from services.adk_service import initialize_adk, run_adk_sync
from config.settings import MESSAGE_HISTORY_KEY, get_api_key

def run_chat_interface():
    """
    Sets up and runs the Streamlit web application for the ADK chat assistant.
    """
    st.set_page_config(page_title="ADK Greeting & Chat Agent", layout="wide") # Configures the browser tab title and page layout.
    st.title("👋 Greeting & Chat Assistant (Powered by ADK & Gemini)") # Main title of the app.
    st.markdown("This application uses the Google Agent Development Kit (ADK) to provide a chat interface.") # Descriptive text.
    st.divider() # A visual separator.
    api_key = get_api_key() # Retrieve the API key from settings.
    if not api_key:
        st.error("⚠️ Action Required: Google API Key Not Found or Invalid! Please set GOOGLE_API_KEY in your .env file. ⚠️")
        st.stop() # Stop the application if the API key is missing, prompting the user for action.
    # Initialize ADK runner and session ID (cached to run only once).
    adk_runner, current_session_id = initialize_adk()
    
    st.divider()
    st.subheader("Chat with the Assistant") # Subheading for the chat section.
    # Initialize chat message history in Streamlit's session state if it doesn't exist.
    if MESSAGE_HISTORY_KEY not in st.session_state:
        st.session_state[MESSAGE_HISTORY_KEY] = []
    # Display existing chat messages from the session state.
    for message in st.session_state[MESSAGE_HISTORY_KEY]:
        with st.chat_message(message["role"]): # Use Streamlit's chat message container for styling.
            st.markdown(message["content"])
    # Handle new user input.
    if prompt := st.chat_input("Ask for a greeting (e.g., 'greet me'), or just chat..."):
        # Append user's message to history and display it.
        st.session_state[MESSAGE_HISTORY_KEY].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        # Process the user's message with the ADK agent and display the response.
        with st.chat_message("assistant"):
            message_placeholder = st.empty() # Create an empty placeholder to update with the assistant's response.
            with st.spinner("Assistant is thinking..."): # Show a spinner while the agent processes the request.
                agent_response = run_adk_sync(adk_runner, current_session_id, prompt) # Call the synchronous ADK runner.
                message_placeholder.markdown(agent_response) # Update the placeholder with the final response.
        
        # Append assistant's response to history.
        st.session_state[MESSAGE_HISTORY_KEY].append({"role": "assistant", "content": agent_response})

run_chat_interface()