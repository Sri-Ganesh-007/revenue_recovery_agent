"""Simulated actions: pure functions that only describe what would happen."""
from datetime import datetime, timedelta

import pytest

from app.recovery.actions import NOTE, simulate_action

NOW = datetime(2026, 10, 12, 12, 0, 0)

CASES = [
    ("schedule_retry", {"retry_after_hours": 24}, "scheduled"),
    ("request_payment_update", {}, "executed_simulated"),
    ("send_reminder", {"tone": "friendly"}, "executed_simulated"),
    ("escalate_to_human", {"reason": "possible fraud"}, "executed_simulated"),
    ("no_action", {"reason": "wait a day"}, "noop"),
]


@pytest.mark.parametrize("action,params,status", CASES)
def test_each_action_is_simulated_and_labelled(action, params, status):
    out = simulate_action(action, params, "pay_1", NOW)
    assert (out.action, out.status) == (action, status)
    assert out.result["simulated"] is True and out.result["tool"] == action and out.result["payment_id"] == "pay_1"
    assert out.result["note"] == NOTE and "No real" in NOTE
    assert out.outcome.startswith("SIMULATED") and len(out.outcome) <= 100        # fits recovery_attempts.outcome


def test_only_a_retry_gets_a_planned_time():
    assert simulate_action("schedule_retry", {"retry_after_hours": 48}, "p", NOW).scheduled_for == NOW + timedelta(hours=48)
    for action, params, _ in CASES[1:]:
        assert simulate_action(action, params, "p", NOW).scheduled_for is None


def test_long_reasons_do_not_overflow_the_outcome_column():
    out = simulate_action("escalate_to_human", {"reason": "x" * 500}, "p", NOW)
    assert len(out.outcome) <= 100


def test_unknown_actions_are_refused():
    for bad in ("charge_card", "", "SCHEDULE_RETRY"):
        with pytest.raises(ValueError):
            simulate_action(bad, {}, "p", NOW)


def test_the_same_input_gives_the_same_output():
    assert simulate_action("send_reminder", {"tone": "urgent"}, "p", NOW) == simulate_action("send_reminder", {"tone": "urgent"}, "p", NOW)


def test_the_actions_module_cannot_touch_a_database_or_the_network():
    import app.recovery.actions as actions
    source = open(actions.__file__).read()
    for forbidden in ("sqlalchemy", "requests", "httpx", "smtplib", "socket", "urllib"):
        assert forbidden not in source
