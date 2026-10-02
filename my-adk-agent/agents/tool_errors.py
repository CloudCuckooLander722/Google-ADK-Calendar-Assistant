import logging
from typing import Any, Optional

from google.adk.tools import BaseTool, ToolContext


def report_tool_error(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, error: Exception) -> Optional[dict]:
    """
    on_tool_error_callback for every LlmAgent: turns an exception raised by a tool
    into a normal tool result the model can read, instead of letting it abort the
    whole turn (which the user only sees as a generic "unexpected error").

    The model gets which tool failed, with which arguments, and why, so it can
    carry on with the remaining steps and tell the user exactly what went wrong.
    """
    logging.warning("Tool %s failed with args %s: %s", tool.name, args, error, exc_info=error)
    return {
        "status": "error",
        "tool": tool.name,
        "arguments": args,
        "error": str(error) or type(error).__name__,
    }
