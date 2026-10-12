"""Allowlisted, simulated recovery tools.

Every tool is a pure Python function that returns a result dict. None of them
touch the database, send emails or move money. The model never supplies the
payment id; the application binds it when dispatching.
"""
import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from app.llm.proposal import MAX_RETRY_DELAY_HOURS


class ToolError(Exception):
    """Base class for tool dispatch failures."""


class UnknownToolError(ToolError):
    pass


class InvalidToolArgumentsError(ToolError):
    pass


class ToolExecutionError(ToolError):
    """A tool function itself failed."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryArgs(_Args):
    retry_after_hours: StrictInt = Field(ge=1, le=MAX_RETRY_DELAY_HOURS)

    @field_validator("retry_after_hours", mode="before")
    @classmethod
    def accept_digit_strings(cls, value):
        # Small local models often send integers as strings ("24"). Accept only
        # plain digit strings; everything else still goes through strict validation.
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 4:
            return int(value)
        return value


class ReminderArgs(_Args):
    tone: Literal["friendly", "urgent"]


class ReasonArgs(_Args):
    reason: str = Field(min_length=1, max_length=200)


class NoArgs(_Args):
    pass


def _simulated(tool: str, payment_id: str, detail: str) -> dict:
    return {
        "simulated": True,
        "status": "simulated_ok",
        "tool": tool,
        "payment_id": payment_id,
        "detail": detail,
        "note": "No real payment, email, ticket or database change was made.",
    }


def schedule_retry(payment_id: str, args: RetryArgs) -> dict:
    return _simulated(
        "schedule_retry", payment_id,
        f"Retry would be scheduled in {args.retry_after_hours} hours (not scheduled for real).",
    )


def request_payment_update(payment_id: str, args: NoArgs) -> dict:
    return _simulated(
        "request_payment_update", payment_id,
        "A request for new payment details would be sent (not sent).",
    )


def send_reminder(payment_id: str, args: ReminderArgs) -> dict:
    return _simulated(
        "send_reminder", payment_id,
        f"A {args.tone} reminder would be sent (not sent).",
    )


def escalate_to_human(payment_id: str, args: ReasonArgs) -> dict:
    return _simulated(
        "escalate_to_human", payment_id,
        f"A human review would be requested: {args.reason} (no ticket created).",
    )


def no_action(payment_id: str, args: ReasonArgs) -> dict:
    return _simulated("no_action", payment_id, f"No action taken: {args.reason}")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[_Args]
    func: Callable[[str, Any], dict]


# The allowlist. Only names in this dict can ever be dispatched.
TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool("schedule_retry",
             f"Schedule a retry of the failed payment after a delay of 1 to {MAX_RETRY_DELAY_HOURS} hours.",
             RetryArgs, schedule_retry),
        Tool("request_payment_update",
             "Ask the customer to provide new payment details. Takes no arguments.",
             NoArgs, request_payment_update),
        Tool("send_reminder",
             "Send the customer a payment reminder with a friendly or urgent tone.",
             ReminderArgs, send_reminder),
        Tool("escalate_to_human",
             "Escalate this payment to a human reviewer, with a short reason.",
             ReasonArgs, escalate_to_human),
        Tool("no_action",
             "Take no action for now, with a short reason.",
             ReasonArgs, no_action),
    )
}


def tool_schemas(tools: dict[str, Tool] = TOOLS) -> list[dict]:
    """Ollama/OpenAI-style tool definitions. payment_id is deliberately absent."""
    schemas = []
    for tool in tools.values():
        params = tool.args_model.model_json_schema()
        params.pop("title", None)
        for prop in params.get("properties", {}).values():
            prop.pop("title", None)
        schemas.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": params,
            },
        })
    return schemas


def dispatch(name: Any, raw_arguments: Any, payment_id: str,
             tools: dict[str, Tool] = TOOLS) -> dict:
    """Validate a model-requested tool call and run the simulated function."""
    if not isinstance(name, str) or name not in tools:
        raise UnknownToolError(f"Tool {name!r} is not in the allowlist")
    tool = tools[name]

    arguments = raw_arguments
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            raise InvalidToolArgumentsError("Arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise InvalidToolArgumentsError("Arguments must be a JSON object")

    # The payment id is bound by the application. A model-supplied id is
    # tolerated only if it matches; anything else is rejected.
    if "payment_id" in arguments:
        if arguments["payment_id"] != payment_id:
            raise InvalidToolArgumentsError("payment_id does not match the payment being processed")
        arguments = {k: v for k, v in arguments.items() if k != "payment_id"}

    try:
        args = tool.args_model.model_validate(arguments)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
        raise InvalidToolArgumentsError(details) from exc

    try:
        return tool.func(payment_id, args)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolExecutionError(f"Tool {name} failed: {type(exc).__name__}") from exc
