"""
Wraps the mentor plugin's plain functions (mentor_data/tools_objectives.py) as
in-process SDK MCP tools, so a real claude_agent_sdk session can call them
directly during a query() — see server.py for how the session is built.
"""

from claude_agent_sdk import tool, create_sdk_mcp_server

from mentor_data import db, schemas
from mentor_data import tools_objectives as impl

db.init_schema()


def _wrap(name: str, fn):
    schema = getattr(schemas, name.upper())

    @tool(name, schema["description"], schema["parameters"])
    async def handler(args: dict) -> dict:
        result = fn(args)
        return {"content": [{"type": "text", "text": result}]}

    return handler


create_objective = _wrap("create_objective", impl.create_objective)
add_milestone = _wrap("add_milestone", impl.add_milestone)
log_progress = _wrap("log_progress", impl.log_progress)
list_objectives = _wrap("list_objectives", impl.list_objectives)
get_objective = _wrap("get_objective", impl.get_objective)
list_due_followups = _wrap("list_due_followups", impl.list_due_followups)
acknowledge_followup = _wrap("acknowledge_followup", impl.acknowledge_followup)

MENTOR_TOOL_NAMES = [
    "create_objective",
    "add_milestone",
    "log_progress",
    "list_objectives",
    "get_objective",
    "list_due_followups",
    "acknowledge_followup",
]

mentor_server = create_sdk_mcp_server(
    name="mentor",
    tools=[create_objective, add_milestone, log_progress, list_objectives, get_objective,
           list_due_followups, acknowledge_followup],
)
