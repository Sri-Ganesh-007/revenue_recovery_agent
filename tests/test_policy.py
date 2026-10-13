"""Deterministic policy tests. All inputs are fabricated snapshots (see policy_fixtures.py);
nothing here touches a database or describes verified gateway behaviour."""
import dataclasses
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.recovery.policy import (DEFAULT_CONFIG, ESCALATE, NO_ACTION, PAYMENT_UPDATE, REMINDER, RETRY,
                                 FailureCategory as C, PolicyConfig, classify, evaluate,
                                 make_policy_check, normalize_reason)
from tests.policy_fixtures import (NOW, ORDINARY, SAFE, SCENARIOS, make_snapshot, params_for,
                                   prior_action, prior_retry)


def decide(action=RETRY, params=None, config=DEFAULT_CONFIG, **snapshot_overrides):
    return evaluate(make_snapshot(**snapshot_overrides), action,
                    params_for(action) if params is None else params, config)


# ---------- every synthetic category x every action ----------

@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_category_is_classified_as_expected(scenario):
    assert classify(scenario.failure_reason) == scenario.category


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.parametrize("action", ORDINARY)
def test_ordinary_actions_by_category(scenario, action):
    d = decide(action, failure_reason=scenario.failure_reason)
    assert d.allowed is (action in scenario.allowed_ordinary), d.reason
    assert d.category == scenario.category.value
    if not d.allowed:
        assert d.rule == "failure_category"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.parametrize("action", SAFE)
def test_safe_fallbacks_always_allowed_in_every_category(scenario, action):
    d = decide(action, failure_reason=scenario.failure_reason)
    assert d.allowed and d.rule == "always_allowed" and d.rules_evaluated == ["always_allowed"]


def test_only_insufficient_funds_permits_a_retry():
    retry_ok = [s.id for s in SCENARIOS if decide(RETRY, failure_reason=s.failure_reason).allowed]
    assert retry_ok == ["insufficient_funds"]


def test_escalation_only_categories_block_every_ordinary_action():
    for reason in ("suspected_fraud", "gateway_timeout"):
        for action in ORDINARY:
            assert not decide(action, failure_reason=reason).allowed
        assert decide(ESCALATE, failure_reason=reason).allowed


# ---------- failure reason normalisation ----------

@pytest.mark.parametrize("reason", ["INSUFFICIENT_FUNDS", "  insufficient_funds  ", "Insufficient Funds",
                                    "insufficient-funds", "insufficient   funds"])
def test_normalised_variants_of_insufficient_funds_permit_retry(reason):
    assert decide(RETRY, failure_reason=reason).allowed


@pytest.mark.parametrize("reason", ["insufficient", "insufficient_funds_x", "no_insufficient_funds",
                                    "insufficient_funds.", "funds", "", "   ", None, "card_declined"])
def test_unknown_or_near_miss_reasons_never_permit_retry(reason):
    d = decide(RETRY, failure_reason=reason)
    assert not d.allowed and d.rule == "failure_category" and d.category == "unknown"


@pytest.mark.parametrize("reason", ["stolen_card", "card_reported_stolen", "Possible FRAUD", "insufficient_funds_fraud"])
def test_fraud_or_stolen_hint_makes_unrecognised_reasons_escalation_only(reason):
    assert classify(reason) == C.FRAUD_RISK
    for action in ORDINARY:
        assert not decide(action, failure_reason=reason).allowed
    assert decide(ESCALATE, failure_reason=reason).allowed


def test_normalize_reason():
    assert normalize_reason(None) is None and normalize_reason("  ") is None
    assert normalize_reason(" A-B  c ") == "a_b_c"


# ---------- payment state, subscription, precedence ----------

@pytest.mark.parametrize("status", ["success", "pending", "refunded", "", None])
def test_payment_must_still_be_failed(status):
    for action in ORDINARY:
        d = decide(action, payment_status=status)
        assert not d.allowed and d.rule == "payment_must_be_failed"
    for action in SAFE:
        assert decide(action, payment_status=status).allowed


def test_payment_status_comparison_is_normalised():
    assert decide(REMINDER, payment_status=" FAILED ").allowed


@pytest.mark.parametrize("status", ["canceled", "cancelled", "paused", "past_due", "expired", "", None])
def test_subscription_must_be_active(status):
    for action in ORDINARY:
        d = decide(action, subscription_status=status)
        assert not d.allowed and d.rule == "subscription_eligible"
    for action in SAFE:
        assert decide(action, subscription_status=status).allowed


def test_active_subscription_comparison_is_normalised():
    assert decide(REMINDER, subscription_status=" Active ").allowed


def test_first_failing_rule_wins():
    d = decide(RETRY, payment_status="success", failure_reason="suspected_fraud",
               subscription_status="canceled")
    assert d.rule == "payment_must_be_failed" and d.rules_evaluated == ["payment_must_be_failed"]


# ---------- recovery window boundaries ----------

def test_window_boundary_is_inclusive():
    on_edge = make_snapshot(failed_at=NOW - timedelta(days=14))
    assert evaluate(on_edge, REMINDER, {}).allowed
    just_after = make_snapshot(failed_at=NOW - timedelta(days=14, seconds=1))
    d = evaluate(just_after, REMINDER, {})
    assert not d.allowed and d.rule == "recovery_window"


def test_window_applies_to_every_ordinary_action_but_not_safe_ones():
    old = dict(failed_at=NOW - timedelta(days=30))
    for action in ORDINARY:
        assert decide(action, **old).rule == "recovery_window"
    for action in SAFE:
        assert decide(action, **old).allowed


def test_retry_must_land_inside_the_window():
    snap = dict(failed_at=NOW - timedelta(days=13))          # deadline is NOW + 1 day
    assert decide(RETRY, {"retry_after_hours": 24}, **snap).allowed        # exactly at the deadline
    d = decide(RETRY, {"retry_after_hours": 25}, **snap)
    assert not d.allowed and d.rule == "retry_within_window"


# ---------- retry cap ----------

THREE = (prior_retry(110, -100), prior_retry(80, -70), prior_retry(50, -40))
BASE = dict(failed_at=NOW - timedelta(days=5))


def test_retry_cap_boundary():
    assert decide(RETRY, {"retry_after_hours": 48}, prior_actions=THREE[:2], **BASE).allowed
    d = decide(RETRY, {"retry_after_hours": 48}, prior_actions=THREE, **BASE)
    assert not d.allowed and d.rule == "retry_cap"


def test_cancelled_retries_do_not_count_toward_the_cap():
    cancelled = tuple(prior_retry(h, -h + 10, status="cancelled") for h in (110, 80, 50))
    assert decide(RETRY, {"retry_after_hours": 48}, prior_actions=cancelled, **BASE).allowed


def test_other_actions_do_not_count_toward_the_retry_cap():
    others = (prior_action(REMINDER, 100), prior_action(PAYMENT_UPDATE, 90), prior_action(ESCALATE, 80))
    assert decide(RETRY, {"retry_after_hours": 48}, prior_actions=others, **BASE).allowed


def test_retry_cap_is_configurable():
    one = PolicyConfig(max_retries=1)
    assert not decide(RETRY, {"retry_after_hours": 48}, prior_actions=THREE[:1], config=one, **BASE).allowed
    zero = PolicyConfig(max_retries=0)
    assert decide(RETRY, {"retry_after_hours": 48}, config=zero, **BASE).rule == "retry_cap"
    many = PolicyConfig(max_retries=10)
    assert decide(RETRY, {"retry_after_hours": 48}, prior_actions=THREE, config=many, **BASE).allowed


def test_an_open_scheduled_retry_blocks_another():
    open_retry = (prior_retry(50, 40, status="scheduled"),)
    d = decide(RETRY, {"retry_after_hours": 100}, prior_actions=open_retry, **BASE)
    assert not d.allowed and d.rule == "retry_not_already_scheduled"
    closed = (prior_retry(50, 40, status="cancelled"),)
    assert decide(RETRY, {"retry_after_hours": 100}, prior_actions=closed, **BASE).allowed


# ---------- minimum interval between retries ----------

def test_retry_interval_boundary_after_a_prior_retry():
    prior = (prior_retry(100, 48),)                            # a prior retry at NOW + 48h
    assert decide(RETRY, {"retry_after_hours": 72}, prior_actions=prior, **BASE).allowed      # exactly 24h later
    d = decide(RETRY, {"retry_after_hours": 71}, prior_actions=prior, **BASE)
    assert not d.allowed and d.rule == "retry_interval"


def test_retry_interval_boundary_before_a_prior_retry():
    prior = (prior_retry(100, 48),)
    assert decide(RETRY, {"retry_after_hours": 24}, prior_actions=prior, **BASE).allowed      # exactly 24h earlier
    assert decide(RETRY, {"retry_after_hours": 25}, prior_actions=prior, **BASE).rule == "retry_interval"


def test_retry_interval_is_configurable():
    prior = (prior_retry(100, 48),)
    assert decide(RETRY, {"retry_after_hours": 50}, prior_actions=prior,
                  config=PolicyConfig(min_retry_interval_hours=2), **BASE).allowed
    assert decide(RETRY, {"retry_after_hours": 50}, prior_actions=prior,
                  config=PolicyConfig(min_retry_interval_hours=3), **BASE).rule == "retry_interval"


@pytest.mark.parametrize("hours", [None, 0, -5, True, "24"])
def test_invalid_retry_hours_are_blocked(hours):
    params = {} if hours is None else {"retry_after_hours": hours}
    d = decide(RETRY, params)
    assert not d.allowed and d.rule == "retry_parameters"


# ---------- same-action cooldown ----------

def test_cooldown_boundary():
    assert decide(REMINDER, prior_actions=(prior_action(REMINDER, 24),)).allowed               # exactly 24h ago
    d = decide(REMINDER, prior_actions=(prior_action(REMINDER, 23.99),))
    assert not d.allowed and d.rule == "action_cooldown"


def test_cooldown_ignores_cancelled_and_different_actions():
    assert decide(REMINDER, prior_actions=(prior_action(REMINDER, 1, status="cancelled"),)).allowed
    assert decide(REMINDER, prior_actions=(prior_action(PAYMENT_UPDATE, 1),)).allowed
    assert decide(PAYMENT_UPDATE, prior_actions=(prior_action(REMINDER, 1),)).allowed


def test_cooldown_does_not_apply_to_safe_fallbacks():
    recent = (prior_action(ESCALATE, 1), prior_action(NO_ACTION, 1))
    assert decide(ESCALATE, prior_actions=recent).allowed
    assert decide(NO_ACTION, prior_actions=recent).allowed


def test_cooldown_is_configurable():
    recent = (prior_action(REMINDER, 30),)
    assert decide(REMINDER, prior_actions=recent).allowed
    assert decide(REMINDER, prior_actions=recent, config=PolicyConfig(action_cooldown_hours=48)).rule == "action_cooldown"


def test_future_dated_prior_action_is_treated_as_recent():
    future = (prior_action(REMINDER, -5),)                      # created "5h in the future" (clock skew)
    assert decide(REMINDER, prior_actions=future).rule == "action_cooldown"


# ---------- configuration ----------

def test_window_and_subscription_overrides():
    assert not decide(REMINDER, failed_at=NOW - timedelta(days=3), config=PolicyConfig(recovery_window_days=2)).allowed
    assert decide(REMINDER, failed_at=NOW - timedelta(days=3), config=PolicyConfig(recovery_window_days=4)).allowed
    wider = PolicyConfig(eligible_subscription_statuses=frozenset({"active", "past_due"}))
    assert decide(REMINDER, subscription_status="past_due", config=wider).allowed


def test_category_actions_can_be_overridden():
    cfg = PolicyConfig(category_actions={**DEFAULT_CONFIG.category_actions,
                                         C.PROCESSOR_DECLINE: frozenset({RETRY})})
    assert decide(RETRY, failure_reason="processor_decline", config=cfg).allowed


@pytest.mark.parametrize("kwargs", [{"recovery_window_days": 0}, {"max_retries": -1},
                                    {"min_retry_interval_hours": -1}, {"action_cooldown_hours": -1}])
def test_invalid_thresholds_are_rejected(kwargs):
    with pytest.raises(ValueError):
        PolicyConfig(**kwargs)


# ---------- unknown actions, result shape, purity ----------

def test_unknown_action_is_blocked():
    for name in ("charge_card", "", "SCHEDULE_RETRY"):
        d = evaluate(make_snapshot(), name, {})
        assert not d.allowed and d.rule == "known_action"


def test_rules_evaluated_for_a_fully_allowed_retry():
    d = decide(RETRY)
    assert d.allowed and d.rule == "all_rules_passed"
    assert d.rules_evaluated == ["payment_must_be_failed", "subscription_eligible", "recovery_window",
                                 "failure_category", "retry_parameters", "retry_not_already_scheduled",
                                 "retry_cap", "retry_interval", "retry_within_window", "action_cooldown"]


def test_result_is_structured_and_serialisable():
    d = decide(RETRY, failure_reason="expired_card")
    data = d.model_dump(mode="json")
    assert set(data) == {"allowed", "action", "rule", "reason", "category", "rules_evaluated", "alternatives"}
    assert data["alternatives"] == []                    # evaluate() itself never fills guidance
    assert data["allowed"] is False and data["action"] == RETRY and data["category"] == "expired_card"
    assert "Permitted" in data["reason"]


def test_results_and_snapshots_are_immutable():
    d = decide(REMINDER)
    with pytest.raises(ValidationError):
        d.allowed = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        make_snapshot().payment_status = "success"


def test_evaluation_is_deterministic_and_does_not_mutate_inputs():
    snap = make_snapshot(prior_actions=THREE, failed_at=NOW - timedelta(days=5))
    params = {"retry_after_hours": 48}
    first = evaluate(snap, RETRY, params)
    assert evaluate(snap, RETRY, params) == first
    assert params == {"retry_after_hours": 48} and snap.prior_actions == THREE


def test_make_policy_check_binds_the_snapshot():
    check = make_policy_check(make_snapshot(failure_reason="suspected_fraud"))
    assert check(RETRY, {"retry_after_hours": 24}).allowed is False
    assert check(ESCALATE, {"reason": "x"}).allowed is True


# ---------------------------------------------------------------- the policy reads a RecoveryContext directly

from datetime import timedelta as _td  # noqa: E402

from app.recovery.policy import snapshot_from_context  # noqa: E402
from tests.context_factory import FAILED_AT, history_item, make_context  # noqa: E402

CTX_NOW = FAILED_AT + _td(days=1)


def decide_ctx(action, params=None, **context_kwargs):
    snapshot = snapshot_from_context(make_context(**context_kwargs), CTX_NOW)
    return evaluate(snapshot, action, params_for(action) if params is None else params)


def test_snapshot_comes_straight_from_the_context():
    snap = snapshot_from_context(make_context(failure_reason="Expired Card", subscription_status="active"), CTX_NOW)
    assert (snap.payment_id, snap.payment_status, snap.failure_reason) == ("pay_1", "failed", "Expired Card")
    assert (snap.failed_at, snap.subscription_status, snap.now, snap.prior_actions) == (FAILED_AT, "active", CTX_NOW, ())


def test_history_becomes_prior_actions():
    planned = CTX_NOW + _td(hours=48)
    snap = snapshot_from_context(make_context(history=[
        history_item("schedule_retry", "scheduled", planned), history_item("send_reminder", "executed_simulated", FAILED_AT)]), CTX_NOW)
    retry, reminder = snap.prior_actions
    assert (retry.action, retry.status, retry.scheduled_for, retry.created_at) == ("schedule_retry", "scheduled", planned, planned)
    assert (reminder.action, reminder.scheduled_for, reminder.created_at) == ("send_reminder", None, FAILED_AT)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
@pytest.mark.parametrize("action", ORDINARY)
def test_every_synthetic_category_behaves_the_same_from_a_context(scenario, action):
    from_context = decide_ctx(action, failure_reason=scenario.failure_reason)
    assert from_context.allowed is (action in scenario.allowed_ordinary)
    assert from_context.category == scenario.category.value            # the ten-label category is preserved


def test_an_open_retry_in_the_history_blocks_another_retry():
    history = [history_item("schedule_retry", "scheduled", CTX_NOW + _td(hours=30))]
    d = decide_ctx(RETRY, {"retry_after_hours": 100}, history=history)
    assert not d.allowed and d.rule == "retry_not_already_scheduled"


def test_a_past_retry_in_the_history_does_not_block_a_new_one():
    history = [history_item("schedule_retry", "executed_simulated", CTX_NOW - _td(hours=30))]
    assert decide_ctx(RETRY, {"retry_after_hours": 48}, history=history).allowed


def test_a_retry_planned_for_the_future_counts_as_recent_even_if_it_is_not_marked_scheduled():
    # recovery_attempts has one timestamp: for a retry it is the PLANNED time. A future-dated retry therefore
    # trips the same-action cooldown (stricter, never more permissive). Spacing boundaries are covered above
    # on the snapshot; in real data such a retry is 'scheduled' and the open-retry rule blocks first.
    history = [history_item("schedule_retry", "executed_simulated", CTX_NOW + _td(hours=48))]
    d = decide_ctx(RETRY, {"retry_after_hours": 72}, history=history)
    assert not d.allowed and d.rule == "action_cooldown"


def test_cooldown_is_checked_against_the_history():
    recent = [history_item("send_reminder", "executed_simulated", CTX_NOW - _td(hours=2))]
    assert decide_ctx(REMINDER, history=recent).rule == "action_cooldown"
    assert decide_ctx(PAYMENT_UPDATE, history=recent).allowed


def test_retry_cap_counts_retries_in_the_history():
    history = [history_item("schedule_retry", "executed_simulated", CTX_NOW - _td(hours=h)) for h in (100, 70, 40)]
    assert decide_ctx(RETRY, {"retry_after_hours": 48}, history=history).rule == "retry_cap"


def test_history_rows_with_an_unknown_strategy_are_ignored():
    history = [history_item("manual_call", "done", CTX_NOW - _td(hours=1))]
    assert decide_ctx(RETRY, {"retry_after_hours": 48}, history=history).allowed
    assert decide_ctx(REMINDER, history=history).allowed


def test_a_same_strategy_row_with_another_status_counts_for_cooldown_but_not_for_open_or_cap():
    history = [history_item("schedule_retry", "failed", CTX_NOW - _td(hours=30))]            # old enough: not recent
    assert decide_ctx(RETRY, {"retry_after_hours": 48}, history=history).allowed
    recent = [history_item("schedule_retry", "failed", CTX_NOW - _td(hours=1))]
    d = decide_ctx(RETRY, {"retry_after_hours": 48}, history=recent)
    assert not d.allowed and d.rule == "action_cooldown"                                      # not "scheduled", not counted as a retry


def test_subscription_and_window_rules_apply_from_a_context():
    assert decide_ctx(REMINDER, subscription_status="canceled").rule == "subscription_eligible"
    assert decide_ctx(REMINDER, failed_at=CTX_NOW - _td(days=15)).rule == "recovery_window"
    assert decide_ctx(ESCALATE, {"reason": "x"}, subscription_status="canceled", failed_at=CTX_NOW - _td(days=15)).allowed


# ---------------------------------------------------------------- the allowed-alternatives guidance (policy.alternatives)

from app.recovery.policy import suggest_alternatives  # noqa: E402


def _snap(**kw):
    return make_snapshot(payment_id="pay_1", **kw)


@pytest.mark.parametrize("snapshot,blocked,expected", [
    (_snap(), REMINDER, [RETRY, PAYMENT_UPDATE, ESCALATE, NO_ACTION]),
    (_snap(prior_actions=(prior_retry(50, 40, "scheduled"),)), RETRY, [REMINDER, PAYMENT_UPDATE, ESCALATE, NO_ACTION]),
    (_snap(failure_reason="suspected_fraud"), RETRY, [ESCALATE, NO_ACTION]),
    (_snap(failure_reason="gateway_timeout"), RETRY, [ESCALATE, NO_ACTION]),
    (_snap(failure_reason="expired_card"), RETRY, [REMINDER, PAYMENT_UPDATE, ESCALATE, NO_ACTION]),
    (_snap(failure_reason=None), RETRY, [REMINDER, PAYMENT_UPDATE, ESCALATE, NO_ACTION]),
    (_snap(subscription_status="canceled"), RETRY, [ESCALATE, NO_ACTION]),
    (_snap(failed_at=NOW - timedelta(days=30)), RETRY, [ESCALATE, NO_ACTION]),
    (_snap(prior_actions=(prior_action(REMINDER, 2),)), RETRY, [PAYMENT_UPDATE, ESCALATE, NO_ACTION]),
])
def test_alternatives_reflect_what_the_policy_would_allow_now(snapshot, blocked, expected):
    assert suggest_alternatives(snapshot, blocked) == expected


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_suggested_alternative_is_genuinely_allowed(scenario):
    snapshot = _snap(failure_reason=scenario.failure_reason)
    for blocked in ORDINARY:
        alternatives = suggest_alternatives(snapshot, blocked)
        assert blocked not in alternatives and alternatives[-2:] == [ESCALATE, NO_ACTION]
        for action in alternatives:
            if action == RETRY:
                continue                                   # a retry is listed if SOME delay is allowed
            assert evaluate(snapshot, action, {}).allowed, (scenario.id, blocked, action)


def test_only_blocked_decisions_carry_alternatives():
    check = make_policy_check(_snap(failure_reason="suspected_fraud"))
    blocked, allowed = check(RETRY, {"retry_after_hours": 24}), check(ESCALATE, {"reason": "x"})
    assert blocked.allowed is False and blocked.alternatives == [ESCALATE, NO_ACTION]
    assert allowed.allowed is True and allowed.alternatives == []
