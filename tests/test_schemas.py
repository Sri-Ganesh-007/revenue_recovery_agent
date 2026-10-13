"""The /recovery/proposal response contract (RecoveryProposal) and its validation."""
import json

import pytest
from pydantic import ValidationError

from app.schemas.recovery import RecoveryProposal


def proposal_json(**overrides):
    data = {"payment_id": "pay_1", "action": "schedule_retry", "parameters": {"retry_after_hours": 24},
            "summary": "Retry in a day."}
    data.update(overrides)
    return json.dumps(data)


def test_valid_proposal_parses():
    p = RecoveryProposal.model_validate_json(proposal_json())
    assert p.action.value == "schedule_retry" and p.parameters == {"retry_after_hours": 24}


@pytest.mark.parametrize("bad", [
    {"action": "refund_customer"},                                  # not an allowed action
    {"parameters": {}},                                             # retry without hours
    {"parameters": {"retry_after_hours": 0}},                       # below range
    {"parameters": {"retry_after_hours": 9999}},                    # above range
    {"parameters": {"retry_after_hours": "24"}},                    # wrong type
    {"parameters": {"retry_after_hours": True}},                    # bool is not an int here
    {"parameters": {"retry_after_hours": 24, "charge_now": True}},  # unexpected key
    {"summary": ""},                                                # empty summary
    {"summary": "x" * 301},                                         # too long
])
def test_invalid_proposals_are_rejected(bad):
    with pytest.raises(ValidationError):
        RecoveryProposal.model_validate_json(proposal_json(**bad))


def test_extra_top_level_fields_are_rejected():
    data = json.loads(proposal_json())
    data["execute_now"] = True
    with pytest.raises(ValidationError):
        RecoveryProposal.model_validate(data)


def test_each_action_needs_exactly_its_own_parameters():
    ok = RecoveryProposal(payment_id="p", action="send_reminder", parameters={"tone": "friendly"}, summary="s")
    assert ok.parameters["tone"] == "friendly"
    with pytest.raises(ValidationError):
        RecoveryProposal(payment_id="p", action="send_reminder", parameters={"tone": "threatening"}, summary="s")
    with pytest.raises(ValidationError):
        RecoveryProposal(payment_id="p", action="escalate_to_human", parameters={}, summary="s")
    with pytest.raises(ValidationError):
        RecoveryProposal(payment_id="p", action="request_payment_update", parameters={"x": 1}, summary="s")
