"""LangGraph orchestration of the simulated tool-calling workflow.

call_model -> (tool calls?) -> run_tools -> (limits?) -> call_model ... -> finish

The graph only routes. Tool validation and execution reuse app/llm/tools.py via
execute_tool_call, and the tool trace in the state is the authoritative record.
"""
import json
from typing import Any, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from app.context.recovery_context import RecoveryContext
from app.llm.decision_maker import InvalidModelOutputError, OllamaDecisionMaker
from app.llm.tool_runner import (
    MAX_ACTIONS,
    MAX_FINAL_MESSAGE_CHARS,
    MAX_REJECTIONS,
    MAX_TURNS,
    SYSTEM_PROMPT,
    SimulationResult,
    ToolCallRecord,
    execute_tool_call,
)
from app.llm.tools import TOOLS, Tool, tool_schemas

# Each turn is call_model + run_tools; plus finish. Second guard behind MAX_TURNS.
RECURSION_LIMIT = 2 * MAX_TURNS + 4


class GraphState(TypedDict):
    payment_id: str
    messages: list[dict]
    last_message: dict
    tool_calls: list[ToolCallRecord]  # authoritative trace
    actions: int
    rejections: int
    turns: int
    result: SimulationResult | None


def route_after_model(state: GraphState) -> str:
    return "run_tools" if state["last_message"].get("tool_calls") else "finish"


def route_after_tools(state: GraphState) -> str:
    if state["rejections"] > MAX_REJECTIONS or state["turns"] >= MAX_TURNS:
        return "finish"
    return "call_model"


def build_graph(model, tools: dict[str, Tool] = TOOLS):
    schemas = tool_schemas(tools)

    def call_model(state: GraphState) -> dict:
        message = model.chat(state["messages"], schemas)
        return {"last_message": message, "turns": state["turns"] + 1}

    def run_tools(state: GraphState) -> dict:
        message = state["last_message"]
        messages = [*state["messages"], message]
        records = list(state["tool_calls"])
        actions, rejections = state["actions"], state["rejections"]

        for call in message.get("tool_calls") or []:
            record = execute_tool_call(call, state["payment_id"], tools, actions < MAX_ACTIONS)
            if record.status == "executed_simulated":
                actions += 1
            else:
                rejections += 1
            records.append(record)
            messages.append({
                "role": "tool",
                "tool_name": record.name or "unknown",
                "content": json.dumps(record.result if record.result is not None else {"error": record.error}),
            })

        return {"messages": messages, "tool_calls": records,
                "actions": actions, "rejections": rejections}

    def finish(state: GraphState) -> dict:
        records, actions = state["tool_calls"], state["actions"]
        message = state["last_message"]
        ended_without_tools = not message.get("tool_calls")

        if ended_without_tools:
            stopped = "completed"
        elif state["rejections"] > MAX_REJECTIONS:
            stopped = "rejection_limit"
        else:
            stopped = "turn_limit"

        if actions == 0:
            # No valid decision was ever executed: a failed run, not an outcome.
            if ended_without_tools and records:
                raise InvalidModelOutputError(
                    f"Model made no valid tool call; last rejection: {records[-1].error}"
                )
            if ended_without_tools:
                raise InvalidModelOutputError("Model did not call any tool")
            raise InvalidModelOutputError(
                f"Model never produced a valid tool call (stopped: {stopped}; "
                f"rejected calls: {len(records)})"
            )

        final_message = None
        if ended_without_tools:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                final_message = content.strip()[:MAX_FINAL_MESSAGE_CHARS]

        return {"result": SimulationResult(
            payment_id=state["payment_id"],
            action_taken=next((r.name for r in records if r.status == "executed_simulated"), None),
            stopped_reason=stopped,
            tool_calls=records,
            final_message=final_message,
        )}

    graph = StateGraph(GraphState)
    graph.add_node("call_model", call_model)
    graph.add_node("run_tools", run_tools)
    graph.add_node("finish", finish)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges("call_model", route_after_model,
                                {"run_tools": "run_tools", "finish": "finish"})
    graph.add_conditional_edges("run_tools", route_after_tools,
                                {"call_model": "call_model", "finish": "finish"})
    graph.add_edge("finish", END)
    return graph.compile()


def run_simulation(
    context: RecoveryContext,
    model,
    tools: dict[str, Tool] = TOOLS,
) -> SimulationResult:
    initial: GraphState = {
        "payment_id": context.failed_payment.id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": OllamaDecisionMaker._context_json(context)},
        ],
        "last_message": {},
        "tool_calls": [],
        "actions": 0,
        "rejections": 0,
        "turns": 0,
        "result": None,
    }
    try:
        final = build_graph(model, tools).invoke(initial, {"recursion_limit": RECURSION_LIMIT})
    except GraphRecursionError as exc:
        raise InvalidModelOutputError("Workflow exceeded its step limit") from exc
    return final["result"]
