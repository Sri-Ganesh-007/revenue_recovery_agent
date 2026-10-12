import pytest

from app.llm.decision_maker import InvalidModelOutputError
from app.llm.graph import run_simulation
from app.llm.tool_runner import MAX_TURNS
from app.llm.tools import NoArgs, Tool, TOOLS
from tests.test_proposal import make_context


def call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


def assistant(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def final(text="Done."):
    return {"role": "assistant", "content": text}


class FakeModel:
    """Returns scripted messages and records what it was sent."""

    def __init__(self, *messages):
        self.script = list(messages)
        self.sent = []

    def chat(self, messages, tools=None):
        self.sent.append([dict(m) for m in messages])
        if not self.script:
            raise AssertionError("model called more times than scripted")
        return self.script.pop(0)


def test_valid_call_then_final_message():
    model = FakeModel(assistant(call("schedule_retry", {"retry_after_hours": 24})), final("Retry scheduled."))
    result = run_simulation(make_context(), model)
    assert result.simulated is True
    assert result.action_taken == "schedule_retry"
    assert result.stopped_reason == "completed"
    assert result.tool_calls[0].status == "executed_simulated"
    assert result.tool_calls[0].result["simulated"] is True
    assert result.final_message == "Retry scheduled."
    assert result.final_message_is_narrative_only is True


def test_tool_result_is_returned_to_the_model():
    model = FakeModel(assistant(call("no_action", {"reason": "ok"})), final())
    run_simulation(make_context(), model)
    second_turn = model.sent[1]
    assert second_turn[-1]["role"] == "tool"
    assert "simulated" in second_turn[-1]["content"]


def test_wrong_final_message_cannot_change_the_recorded_action():
    model = FakeModel(
        assistant(call("request_payment_update", {})),
        final("The payment was charged successfully and the account was updated."),
    )
    result = run_simulation(make_context(), model)
    assert result.action_taken == "request_payment_update"
    assert result.tool_calls[0].result["note"].startswith("No real")
    assert result.tool_calls[0].status == "executed_simulated"


def test_unknown_tool_then_recovery():
    model = FakeModel(
        assistant(call("charge_card", {"amount": 10})),
        assistant(call("no_action", {"reason": "retry with valid tool"})),
        final(),
    )
    result = run_simulation(make_context(), model)
    assert [r.status for r in result.tool_calls] == ["rejected", "executed_simulated"]
    assert result.tool_calls[0].name == "charge_card"
    assert result.action_taken == "no_action"


def test_malformed_arguments_rejected_then_recovery():
    model = FakeModel(
        assistant(call("schedule_retry", {"retry_after_hours": 9999})),
        assistant(call("schedule_retry", {"retry_after_hours": 12})),
        final(),
    )
    result = run_simulation(make_context(), model)
    assert result.tool_calls[0].status == "rejected"
    assert result.tool_calls[1].status == "executed_simulated"


def test_only_one_action_per_run():
    model = FakeModel(
        assistant(call("send_reminder", {"tone": "friendly"}),
                  call("escalate_to_human", {"reason": "second action"})),
        final(),
    )
    result = run_simulation(make_context(), model)
    assert [r.status for r in result.tool_calls] == ["executed_simulated", "rejected"]
    assert result.action_taken == "send_reminder"


def test_model_supplied_payment_id_for_another_payment_is_rejected():
    model = FakeModel(
        assistant(call("no_action", {"reason": "x", "payment_id": "pay_other"})),
        assistant(call("no_action", {"reason": "x"})),
        final(),
    )
    result = run_simulation(make_context("pay_1"), model)
    assert result.tool_calls[0].status == "rejected"
    assert result.tool_calls[1].result["payment_id"] == "pay_1"


def test_no_tool_call_is_invalid_output():
    with pytest.raises(InvalidModelOutputError):
        run_simulation(make_context(), FakeModel(final("I think we should wait.")))


def test_repeated_rejections_stop_the_run_without_action():
    bad = assistant(call("charge_card", {}))
    model = FakeModel(bad, bad, bad)
    with pytest.raises(InvalidModelOutputError):
        run_simulation(make_context(), model)
    assert model.script == []


def test_tool_execution_error_is_reported_as_error_and_loop_continues():
    def boom(payment_id, args):
        raise RuntimeError("db exploded")

    tools = {**TOOLS, "no_action": Tool("no_action", "d", NoArgs, boom)}
    model = FakeModel(
        assistant(call("no_action", {})),
        assistant(call("request_payment_update", {})),
        final(),
    )
    result = run_simulation(make_context(), model, tools)
    assert result.tool_calls[0].status == "error"
    assert "db exploded" not in (result.tool_calls[0].error or "")
    assert result.action_taken == "request_payment_update"


def test_turn_limit_stops_after_action_with_no_final_message():
    model = FakeModel(
        assistant(call("no_action", {"reason": "a"})),
        assistant(call("no_action", {"reason": "b"})),
        assistant(call("no_action", {"reason": "c"})),
    )
    result = run_simulation(make_context(), model)
    assert result.stopped_reason == "turn_limit"
    assert result.final_message is None
    assert result.action_taken == "no_action"
    assert MAX_TURNS == 3
    assert len([r for r in result.tool_calls if r.status == "executed_simulated"]) == 1


def test_empty_final_message_is_none():
    model = FakeModel(assistant(call("no_action", {"reason": "a"})), final("   "))
    assert run_simulation(make_context(), model).final_message is None


def test_arguments_as_json_string_from_model():
    model = FakeModel(assistant(call("schedule_retry", '{"retry_after_hours": 6}')), final())
    result = run_simulation(make_context(), model)
    assert result.action_taken == "schedule_retry"


def test_failure_after_rejection_reports_the_reason():
    model = FakeModel(
        assistant(call("schedule_retry", {"retry_after_hours": "soon"})),
        final("Sorry, I will not retry."),
    )
    with pytest.raises(InvalidModelOutputError) as exc:
        run_simulation(make_context(), model)
    assert "retry_after_hours" in str(exc.value)


def test_digit_string_hours_from_model_run_successfully():
    model = FakeModel(assistant(call("schedule_retry", {"retry_after_hours": "24"})), final())
    assert run_simulation(make_context(), model).action_taken == "schedule_retry"
