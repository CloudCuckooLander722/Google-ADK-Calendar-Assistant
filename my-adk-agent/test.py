import os
import asyncio
import os
import vertexai
from google.adk.memory import VertexAiMemoryBankService
from google.adk.sessions import VertexAiSessionService
from google.adk.runners import Runner
from google.genai import Client, types
from agent import root_agent
from google.cloud import aiplatform
from tenacity import retry, wait_random_exponential, stop_after_attempt

from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(dotenv_path)

project_id = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
if project_id:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
else:
    raise RuntimeError(
        "Missing GCP_PROJECT_ID or GOOGLE_CLOUD_PROJECT. "
        "Set one in my-adk-agent/.env or the shell environment."
    )

vertexai.init(project=project_id, location="us-central1")

agent_engine_id = os.environ.get("AGENT_ENGINE_ID", "your-reasoning-engine-id")
app_name = "your-app-name"


APP_NAME = f"projects/{project_id}/locations/us-central1/reasoningEngines/{agent_engine_id}"
USER_ID = "cloud_cuckoo"

memory_service = VertexAiMemoryBankService(
    project=project_id,
    location="us-central1",
    agent_engine_id=agent_engine_id
)

session_service = VertexAiSessionService(
    project=project_id,
    location="us-central1",
    agent_engine_id=agent_engine_id
)

async def setup_session_and_runner():
    session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    runner = Runner(agent=root_agent, 
                    app_name=APP_NAME, 
                    session_service=session_service, 
                    memory_service=memory_service,
                    )
    
    return session, runner

async def run_single_turn(query, session, user_id, runner):
        """Run a single conversation turn."""
        content = types.Content(role="user", parts=[types.Part(text=query)])
        events = runner.run_async(user_id=user_id, session_id=session.id, new_message=content)

        response_content = None
        
        async for event in events:
                if event is None:
                    continue
                if hasattr(event, 'is_final_response') and event.is_final_response():
                    if getattr(event, 'content', None) and getattr(event.content, 'parts', None):
                        response_content = event.content.parts[0].text
                
        return response_content

async def chat_loop(session, user_id, runner) -> None:
            """Main chat interface loop."""
            print("\nStarting chat. Type 'exit' or 'quit' to end.")
            print("Every message will be automatically stored in memory.\n")
        
            while True:
                user_input = input("\nYou: ")
                if user_input.lower() in ["quit", "exit", "bye"]:
                    print("\nAssistant: Thank you for chatting. Have a great day!")
                    break
        
                response = await run_single_turn(user_input, session, user_id, runner=runner)
                if response:
                    print(f"\nAssistant: {response}")
        
            completed_session = await runner.session_service.get_session(app_name=app_name, user_id=USER_ID, session_id=session.id)
            
            await memory_service.add_session_to_memory(completed_session)





def run_session():

    session, runner = asyncio.run(setup_session_and_runner())

    asyncio.run(chat_loop(session=session, user_id=USER_ID, runner=runner))

print("hello world")
run_session()