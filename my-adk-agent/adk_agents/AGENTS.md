# 1.0 Agent Requirements

## 1.1 `root_agent`.py

### 1.11 Requirements

- `root_agent` is a WorkFlow agent that must pass a request (e.g. "schedule an appointment with Dr. Chong") through a decision-making model (e.g. Jev)
- Based off the probability, `root_agent` will call `calendar_agent` to schedule the requested tasks in the plan or `consultant_agent` to offer critiques + suggest revised plan.

### 1.12 Test Cases

- "Make me a problem set for Calc I and schedule events": supposed to use Jev to route, then has the `workspace_agent` generate a problem set on derivatives, for example, and then uses similarity search to place it in a related folder, or creates it if None.
- Then it invokes the consultation agent when prompting the user if they want a plan to complete the problem set by the exam date, for example.
- And finally, it's SUPPOSED to invoke the `calendar_agent` to schedule all of the work sessions.

- "Schedule a hangout with Kaoruko Waguri at the Cheesecake Factory.": routes to `calendar_agent`, which directly schedules the events.

- "I have plan from Document A. Scan it, list all of the plans, and then schedule events with steps in calendar description." 
- collects info, then routes it to `consultant_agent` for processing, then asks for revision or direct scheduling. if revision, then revised plan, with scheduled work sessions.

-"I want to schedule a plan to study Chapter 10 test for Acc Geo / Alg 2 Trig" 
- compare against the exam due date: ask
- collects info, then routes it to `consultant_agent` for steps, may prompt the user if its okay to ask `workspace_agent` to create documents and save it to shared Drive folder
- then asks to schedule
- then uses `calendar_agent`.

## 1.2 `calendar_agent`.py

### 1.21 Requirements 

- The `calendar_agent` must sort incoming requests from the context window (e.g. requests to schedule a barbecue for Walter White) to categories like "SCHEDULE_EVENT", "DELETE_EVENT", "SCHEDULE_TASK", "DELETE_TASK", "LIST_TASKS", "LIST_EVENTS".

- Associated functions with these categories must execute tasks (e.g scheduling an appointment on Google Calendar) based on the commands listed.

### 1.22 Test Cases

- "Request to schedule an appointment with Dr. Chong from 3:20 PM - 4:00 PM" schedules an appointment with Dr. Chong at that specific time, along with additional information


