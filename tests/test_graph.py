import socket

import pytest

from app.llm.decision_maker import InvalidModelOutputError, OllamaUnavailableError
from app.llm.graph import (
    RECURSION_LIMIT,
    build_graph,
    route_after_model,
    route_after_tools,
    run_simulation,
)
from app.llm.tool_runner import MAX_REJECTIONS, MAX_TURNS
from tests.test_proposal import make_context
from tests.test_tool_runner import FakeModel, assistant, call, final

TRACING_VARS = ["LANGSMITH_TRACING", "LANGSMITH_TRACING_V2",
                "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"]


# ---- routing functions ----

def test_route_after_model():
    assert route_after_model({"last_message": assistant(call("no_action", {}))}) == "run_tools"
    assert route_after_model({"last_message": final("hello")}) == "finish"
    assert route_after_model({"last_message": {"content": "x", "tool_calls": []}}) == "finish"


def test_route_after_tools_continues_while_under_limits():
    assert route_after_tools({"rejections": 0, "turns": 1}) == "call_model"
    assert route_after_tools({"rejections": MAX_REJECTIONS, "turns": MAX_TURNS - 1}) == "call_model"


def test_route_after_tools_finishes_on_rejection_or_turn_limit():
    assert route_after_tools({"rejections": MAX_REJECTIONS + 1, "turns": 1}) == "finish"
    assert route_after_tools({"rejections": 0, "turns": MAX_TURNS}) == "finish"


def test_graph_has_the_expected_nodes():
    graph = build_graph(FakeModel()).get_graph()
    assert {"call_model", "run_tools", "finish"} <= set(graph.nodes)


# ---- no_action vs failed run ----

def test_valid_no_action_is_a_completed_outcome():
    model = FakeModel(assistant(call("no_action", {"reason": "customer already contacted"})),
                      final("No action."))
    result = run_simulation(make_context(), model)
    assert result.action_taken == "no_action"
    assert result.stopped_reason == "completed"
    assert result.tool_calls[0].status == "executed_simulated"


def test_run_with_no_valid_decision_is_a_failure_not_an_outcome():
    with pytest.raises(InvalidModelOutputError):
        run_simulation(make_context(), FakeModel(final("I would wait.")))


# ---- tool execution, trace, completion ----

def test_trace_is_ordered_and_authoritative():
    model = FakeModel(
        assistant(call("charge_card", {})),
        assistant(call("send_reminder", {"tone": "urgent"})),
        final("Payment was recovered!"),
    )
    result = run_simulation(make_context(), model)
    assert [(r.name, r.status) for r in result.tool_calls] == [
        ("charge_card", "rejected"), ("send_reminder", "executed_simulated")]
    assert result.action_taken == "send_reminder"
    assert result.final_message_is_narrative_only is True


def test_tool_results_are_sent_back_to_the_model_between_turns():
    model = FakeModel(assistant(call("request_payment_update", {})), final())
    run_simulation(make_context(), model)
    assert model.sent[1][-1]["role"] == "tool"
    assert len(model.sent) == 2


# ---- limits ----

def test_model_is_called_at_most_max_turns_times():
    model = FakeModel(*[assistant(call("no_action", {"reason": str(i)})) for i in range(MAX_TURNS)])
    result = run_simulation(make_context(), model)
    assert result.stopped_reason == "turn_limit"
    assert len(model.sent) == MAX_TURNS
    assert model.script == []


def test_one_action_per_run():
    model = FakeModel(
        assistant(call("send_reminder", {"tone": "friendly"}), call("no_action", {"reason": "x"})),
        final(),
    )
    result = run_simulation(make_context(), model)
    assert [r.status for r in result.tool_calls] == ["executed_simulated", "rejected"]


def test_rejection_limit_stops_the_run_before_the_turn_limit():
    bad_three = assistant(*[call("charge_card", {})] * (MAX_REJECTIONS + 1))
    model = FakeModel(bad_three)
    with pytest.raises(InvalidModelOutputError) as exc:
        run_simulation(make_context(), model)
    assert "rejection_limit" in str(exc.value)
    assert len(model.sent) == 1


def test_rejection_limit_after_an_action_returns_the_trace():
    model = FakeModel(assistant(
        call("no_action", {"reason": "ok"}),
        call("charge_card", {}), call("charge_card", {}), call("charge_card", {}),
    ))
    result = run_simulation(make_context(), model)
    assert result.stopped_reason == "rejection_limit"
    assert result.action_taken == "no_action"


def test_recursion_guard_is_above_the_normal_maximum():
    assert RECURSION_LIMIT > 2 * MAX_TURNS + 1


def test_recursion_limit_error_is_reported_as_invalid_output(monkeypatch):
    monkeypatch.setattr("app.llm.graph.RECURSION_LIMIT", 2)
    model = FakeModel(assistant(call("charge_card", {})), assistant(call("charge_card", {})))
    with pytest.raises(InvalidModelOutputError) as exc:
        run_simulation(make_context(), model)
    assert "step limit" in str(exc.value)


# ---- error handling ----

class RaisingModel:
    def __init__(self, error):
        self.error = error

    def chat(self, messages, tools=None):
        raise self.error


def test_ollama_errors_pass_through_the_graph_unchanged():
    with pytest.raises(OllamaUnavailableError):
        run_simulation(make_context(), RaisingModel(OllamaUnavailableError("down")))
    with pytest.raises(InvalidModelOutputError):
        run_simulation(make_context(), RaisingModel(InvalidModelOutputError("bad")))


# ---- LangSmith stays off ----

def _clear_langsmith_cache():
    from langsmith.utils import get_env_var
    get_env_var.cache_clear()


def test_langsmith_tracing_is_disabled_by_default(monkeypatch):
    for var in TRACING_VARS:
        monkeypatch.delenv(var, raising=False)
    _clear_langsmith_cache()
    from langsmith.utils import tracing_is_enabled
    assert tracing_is_enabled() is False


def test_the_tracing_check_would_notice_it_being_enabled(monkeypatch):
    # Control: proves the previous test can actually fail.
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    _clear_langsmith_cache()
    from langsmith.utils import tracing_is_enabled
    assert tracing_is_enabled() is True
    monkeypatch.delenv("LANGSMITH_TRACING")
    _clear_langsmith_cache()


def test_graph_run_makes_no_network_connections(monkeypatch):
    for var in TRACING_VARS:
        monkeypatch.delenv(var, raising=False)
    _clear_langsmith_cache()

    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted during a graph run")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    model = FakeModel(assistant(call("no_action", {"reason": "x"})), final())
    assert run_simulation(make_context(), model).action_taken == "no_action"
