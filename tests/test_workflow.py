"""The small LangGraph workflow: LLM proposes, policy approves or blocks, at most two tries. No database."""
import pytest

from app.recovery.decision_maker import InvalidModelOutputError, OllamaUnavailableError
from app.recovery.workflow import MAX_PROPOSALS, build_workflow, run_workflow
from tests.context_factory import make_context
from tests.policy_fixtures import SCENARIOS
from tests.service_fixtures import FIXED_NOW, FakeDecisionMaker, escalate, invalid, reminder, retry, update, wait


def run(maker, **context):
    return run_workflow(maker, make_context(**context), FIXED_NOW)


def test_the_graph_has_three_small_steps():
    graph = build_workflow(FakeDecisionMaker(), make_context(), FIXED_NOW).get_graph()
    assert {n for n in graph.nodes if not n.startswith("__")} == {"propose", "check", "retry"}


def test_an_approved_first_proposal_ends_the_workflow():
    maker = FakeDecisionMaker(retry(24))
    attempts = run(maker)
    assert len(attempts) == 1 and attempts[0].approved and maker.calls == 1 and maker.feedbacks == [None]


def test_a_blocked_proposal_gets_one_more_try_with_the_reason():
    maker = FakeDecisionMaker(retry(24), escalate("possible fraud"))
    attempts = run(maker, failure_reason="suspected_fraud")
    assert [a.action for a in attempts] == ["schedule_retry", "escalate_to_human"]
    assert not attempts[0].approved and attempts[1].approved
    feedback = maker.feedbacks[1]
    assert "refused by the safety policy" in feedback and "schedule_retry" in feedback
    assert "escalate_to_human" in feedback and "Propose a DIFFERENT action" in feedback       # includes the alternatives


def test_two_blocked_proposals_end_safely_with_nothing_approved():
    maker = FakeDecisionMaker(retry(24), update())
    attempts = run(maker, failure_reason="suspected_fraud")
    assert len(attempts) == 2 and not any(a.approved for a in attempts) and maker.calls == 2


def test_blocked_gateway_timeout_proposals_get_one_policy_checked_escalation_without_a_third_model_call():
    maker = FakeDecisionMaker(retry(24), update())
    attempts = run(maker, failure_reason="gateway_timeout")
    assert maker.calls == 2 and [a.action for a in attempts] == ["schedule_retry", "request_payment_update", "escalate_to_human"]
    assert [a.approved for a in attempts] == [False, False, True] and attempts[2].verdict.rule == "always_allowed"


def test_an_approved_proposal_for_a_gateway_timeout_gets_no_fallback():
    attempts = run(FakeDecisionMaker(escalate("timeout")), failure_reason="gateway_timeout")
    assert len(attempts) == 1 and attempts[0].approved


def test_the_llm_is_never_asked_more_than_twice():
    maker = FakeDecisionMaker(retry(24), retry(48), retry(72))
    run(maker, failure_reason="gateway_timeout")
    assert maker.calls == MAX_PROPOSALS == 2 and len(maker.script) == 1


def test_invalid_output_counts_as_a_try_and_the_model_is_told_why():
    maker = FakeDecisionMaker(invalid("schedule_retry needs retry_after_hours"), reminder())
    attempts = run(maker)
    assert attempts[0].decision is None and "retry_after_hours" in attempts[0].error
    assert attempts[1].approved and "not a valid decision" in maker.feedbacks[1]


def test_two_invalid_outputs_raise():
    maker = FakeDecisionMaker(invalid("a"), invalid("b"))
    with pytest.raises(InvalidModelOutputError):
        run(maker)
    assert maker.calls == 2


def test_blocked_then_invalid_still_returns_the_blocked_proposal():
    maker = FakeDecisionMaker(retry(24), invalid("oops"))
    attempts = run(maker, failure_reason="expired_card")
    assert attempts[0].verdict is not None and not attempts[0].approved and attempts[1].decision is None


def test_an_unreachable_ollama_stops_immediately():
    maker = FakeDecisionMaker(OllamaUnavailableError("down"), retry())
    with pytest.raises(OllamaUnavailableError):
        run(maker)
    assert maker.calls == 1


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s.id != "insufficient_funds"], ids=lambda s: s.id)
def test_the_policy_is_the_final_authority_a_retry_is_never_approved(scenario):
    attempts = run(FakeDecisionMaker(retry(24), retry(48)), failure_reason=scenario.failure_reason)
    assert not any(a.approved for a in attempts if a.action == "schedule_retry")


def test_the_workflow_never_executes_anything():
    attempts = run(FakeDecisionMaker(wait()))
    assert attempts[0].approved and attempts[0].verdict.rule == "always_allowed"      # approval only; nothing was run
    assert not hasattr(attempts[0], "outcome")
