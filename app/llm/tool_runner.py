"""Result models and per-call execution for the simulated tool workflow.

The loop itself lives in app/llm/graph.py (LangGraph). The trace of tool calls
(validated and executed by this application) is the authoritative record. The
model's final message is narrative only.
"""
from typing import Any, Literal

from pydantic import BaseModel

from app.llm.tools import Tool, ToolError, ToolExecutionError, dispatch

MAX_TURNS = 3
MAX_ACTIONS = 1
MAX_REJECTIONS = 2
MAX_FINAL_MESSAGE_CHARS = 500

SYSTEM_PROMPT = """You are a payment recovery assistant for a subscription business.
You are given the context of one failed subscription payment. Choose exactly one
recovery action by calling one of the provided tools. Do not include a payment id.
All tools are simulated. After the tool result, reply with one short sentence that
only restates what the tool result says."""


class ToolCallRecord(BaseModel):
    name: str | None
    arguments: dict[str, Any]
    status: Literal["executed_simulated", "rejected", "error"]
    result: dict | None = None
    error: str | None = None


class SimulationResult(BaseModel):
    simulated: Literal[True] = True
    payment_id: str
    # Derived by the application from the trace, never from model text.
    action_taken: str | None
    stopped_reason: Literal["completed", "turn_limit", "rejection_limit"]
    tool_calls: list[ToolCallRecord]
    # Model narrative only. It may be wrong; rely on tool_calls.
    final_message: str | None
    final_message_is_narrative_only: Literal[True] = True


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {"_raw": str(value)[:200]}


def execute_tool_call(
    call: Any, payment_id: str, tools: dict[str, Tool], action_allowed: bool
) -> ToolCallRecord:
    """Validate and run one model-requested tool call; always returns a trace record."""
    function = call.get("function", {}) if isinstance(call, dict) else {}
    name = function.get("name")
    raw_args = function.get("arguments")
    shown_name = name if isinstance(name, str) else None
    shown_args = _as_dict(raw_args) if raw_args is not None else {}

    if not action_allowed:
        return ToolCallRecord(
            name=shown_name, arguments=shown_args, status="rejected",
            error=f"Only {MAX_ACTIONS} action per run is allowed",
        )
    try:
        result = dispatch(name, raw_args, payment_id, tools)
    except ToolError as exc:
        status = "error" if isinstance(exc, ToolExecutionError) else "rejected"
        return ToolCallRecord(name=shown_name, arguments=shown_args, status=status, error=str(exc))
    return ToolCallRecord(name=name, arguments=shown_args, status="executed_simulated", result=result)
