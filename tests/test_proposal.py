import json
import urllib.error
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.context.recovery_context import (
    CurrentRecoveryState,
    CustomerContext,
    FailedPaymentContext,
    PaymentPattern,
    RecoveryContext,
    SubscriptionContext,
)
from app.llm.decision_maker import (
    InvalidModelOutputError,
    OllamaDecisionMaker,
    OllamaUnavailableError,
)
from app.llm.proposal import RecoveryProposal


def make_context(payment_id="pay_1"):
    return RecoveryContext(
        customer=CustomerContext(id="c1", name="Jane Doe", email="jane@example.com"),
        subscription=SubscriptionContext(
            id="s1", status="active", plan="pro", amount=10.0,
            currency="USD", billing_frequency="monthly",
        ),
        failed_payment=FailedPaymentContext(
            id=payment_id, amount=10.0, currency="USD",
            failure_reason="insufficient_funds", failed_at=datetime(2026, 1, 1),
        ),
        payment_pattern=PaymentPattern(
            total_payments=1, successful_payments=0, failed_payments=1,
            success_rate=0.0, recent_history=[],
        ),
        recovery_history=[],
        current_recovery_state=CurrentRecoveryState(
            attempt_count=0, last_action=None, last_action_at=None,
            next_scheduled_action=None,
        ),
    )


def proposal_json(**overrides):
    data = {
        "payment_id": "pay_1",
        "action": "schedule_retry",
        "parameters": {"retry_after_hours": 24},
        "summary": "Retry in a day.",
    }
    data.update(overrides)
    return json.dumps(data)


# ---- proposal validation ----

def test_valid_proposal_parses():
    p = RecoveryProposal.model_validate_json(proposal_json())
    assert p.action.value == "schedule_retry"
    assert p.parameters == {"retry_after_hours": 24}


@pytest.mark.parametrize("bad", [
    {"action": "refund_customer"},                                  # not in enum
    {"parameters": {}},                                             # retry missing hours
    {"parameters": {"retry_after_hours": 0}},                       # below range
    {"parameters": {"retry_after_hours": 9999}},                    # above range
    {"parameters": {"retry_after_hours": "24"}},                    # wrong type
    {"parameters": {"retry_after_hours": True}},                    # bool is not an int here
    {"parameters": {"retry_after_hours": 24, "charge_now": True}},  # unexpected key
    {"summary": ""},                                                # empty summary
    {"summary": "x" * 301},                                         # too long
])
def test_invalid_proposals_rejected(bad):
    with pytest.raises(ValidationError):
        RecoveryProposal.model_validate_json(proposal_json(**bad))


def test_extra_top_level_field_rejected():
    data = json.loads(proposal_json())
    data["execute_now"] = True
    with pytest.raises(ValidationError):
        RecoveryProposal.model_validate(data)


def test_other_actions_need_their_parameters():
    ok = RecoveryProposal(payment_id="p", action="send_reminder",
                          parameters={"tone": "friendly"}, summary="s")
    assert ok.parameters["tone"] == "friendly"
    with pytest.raises(ValidationError):
        RecoveryProposal(payment_id="p", action="send_reminder",
                         parameters={"tone": "threatening"}, summary="s")
    with pytest.raises(ValidationError):
        RecoveryProposal(payment_id="p", action="escalate_to_human",
                         parameters={}, summary="s")
    with pytest.raises(ValidationError):
        RecoveryProposal(payment_id="p", action="request_payment_update",
                         parameters={"x": 1}, summary="s")


# ---- decision maker (no live LLM) ----

def maker_returning(content):
    maker = OllamaDecisionMaker(base_url="http://x", model="m", timeout=1)
    maker._call_ollama = lambda payload: content
    return maker


def test_decision_maker_returns_valid_proposal():
    p = maker_returning(proposal_json()).propose(make_context())
    assert p.payment_id == "pay_1"


def test_decision_maker_rejects_non_json():
    with pytest.raises(InvalidModelOutputError):
        maker_returning("not json").propose(make_context())


def test_decision_maker_rejects_invalid_proposal():
    with pytest.raises(InvalidModelOutputError):
        maker_returning(proposal_json(action="refund_customer")).propose(make_context())


def test_decision_maker_rejects_wrong_payment_id():
    with pytest.raises(InvalidModelOutputError):
        maker_returning(proposal_json(payment_id="pay_other")).propose(make_context())


def test_ollama_unreachable_raises_unavailable(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    maker = OllamaDecisionMaker(base_url="http://x", model="m", timeout=1)
    with pytest.raises(OllamaUnavailableError):
        maker.propose(make_context())


def test_payload_excludes_customer_name_and_email():
    sent = {}
    maker = OllamaDecisionMaker(base_url="http://x", model="m", timeout=1)

    def capture(payload):
        sent.update(payload)
        return proposal_json()

    maker._call_ollama = capture
    maker.propose(make_context())
    user_message = sent["messages"][1]["content"]
    assert "Jane Doe" not in user_message
    assert "jane@example.com" not in user_message
    assert '"c1"' in user_message
