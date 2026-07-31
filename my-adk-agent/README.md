# my-adk-agent

This directory contains the agent implementation for the Google ADK Calendar Assistant.

## Contents

- `calendar_agent/` — Calendar assistant package module
- `requirements.txt` — Python dependencies for this agent package

## Purpose

The `my-adk-agent` package is intended to host the Google ADK Calendar Assistant agent logic and supporting files. It is a nested package within the repository, separate from the root project documentation.

## Setup

1. Create a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Directory structure

- `calendar_agent/__init__.py` — package initializer
- `calendar_agent/calendar_agent.py` — main calendar agent implementation
- `calendar_agent/credentials.json` — stored credentials for Google Calendar integration
- `calendar_agent/test.py` — package-specific test script

## Agent Requirements

### root_agent (Orchestrator)
- Objective: Act as the primary entry point for user interaction and accurately route intents.
- Routing Logic:
  - Direct Command Route: If user intent contains only transactional scheduling requests (e.g., "Schedule an event", "Delete task x"), route the session to `calendar_agent`.
  - Consultative Route: If user intent contains strategic planning, exam prep, or open-ended project discussion (e.g., "Here is my plan for..."), route the session to `consultant_agent`.

### calendar_agent (Functional Utility Sub-Agent)
- Objective: Execute deterministic CRUD operations on Google Calendar and Google Tasks via specialized tool schemas.
- Toolbox Requirements:
  - Calendar API Tools: `create_event`, `delete_event`, `get_event`, `search_events`, `list_events`, `update_event`.
  - Tasks API Tools: `create_task`, `get_task`, `list_tasks`, `patch_task`, `delete_task`.
  - Parsing Utilities: `parse_recurrence`, `parse_natural_language_datetime`.
- Input/Output Constraints:
  - Input: Natural language commands regarding dates/times/tasks.
  - Output: Structured confirmation payload (e.g., "Task 'Feed Dog' successfully committed to Google Tasks for August 11, 2026").

### consultant_agent (Strategic Sub-Agent)
- Objective: Provide rigorous, objective, and anti-sycophantic evaluation of user plans, and programmatically schedule the optimized result.
- Toolbox Requirements:
  - `AgentTool(calendar_agent)`: Invoked as an atomic function call to commit schedules without relinquishing conversational control to the user.
- Behavioral Constraints (Anti-Sycophancy):
  - The agent must not default to praising the user's initial plan.
  - The agent must actively seek out logical gaps, unrealistic timelines, cramming, or missing milestones in the user's strategy.
- Execution Lifecycle (The 3-Step Output):
  1. Critique Stage: Generate an explicit "Pros and Cons" matrix detailing structural weaknesses in the user's approach.
  2. Revision Stage: Output an optimized, revised step-by-step strategy. Offer a choice to update or not.
  3. Execution Stage: If yes, Pass the finalized milestone dates to `AgentTool(calendar_agent)` to automatically create the relevant calendar events and tasks.
  If no, hand back control to the root_agent.

### Error Handling & Edge Cases
- Conflicting Tools: If a user asks the `consultant_agent` a pure scheduling question, it must bypass logic processing and pass the request directly to its `AgentTool(calendar_agent)`.
- Temporal Conflicts: If the `calendar_agent` detects an event overlap, it must bubble up a warning to the calling agent or user rather than silently overwriting.

## Usage

Run the calendar agent or test module from inside the `my-adk-agent` folder:

```bash
cd my-adk-agent
python calendar_agent/test.py
```

Adjust `credentials.json` and Google API settings before first use.
