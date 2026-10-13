"""Deterministic recovery policy.

Pure functions only: no database access and no side effects. Callers build a
PolicySnapshot (app/recovery/snapshot.py) and ask evaluate() whether a proposed
action is allowed. The same function can run before a tool is dispatched and
again inside the final database transaction.

IMPORTANT - failure reasons: the database stores failure_reason as free text and
the only value observed in real data is "insufficient_funds". The other labels in
DEFAULT_REASON_CATEGORIES are SYNTHETIC policy labels, not verified processor
codes. Unrecognised or missing reasons never permit an automated retry.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict

from app.context.recovery_context import RecoveryContext

RETRY = "schedule_retry"
REMINDER = "send_reminder"
PAYMENT_UPDATE = "request_payment_update"
ESCALATE = "escalate_to_human"
NO_ACTION = "no_action"

SAFE_ACTIONS = frozenset({NO_ACTION, ESCALATE})
ORDINARY_ACTIONS = frozenset({RETRY, REMINDER, PAYMENT_UPDATE})
# A retry that is scheduled or has run counts toward the cap; a cancelled one does not.
COUNTED_RETRY_STATUSES = frozenset({"scheduled", "executed_simulated"})


class FailureCategory(str, Enum):
    FUNDS = "insufficient_funds"
    CARD_EXPIRED = "expired_card"
    INVALID_DETAILS = "invalid_payment_details"
    AUTH_REQUIRED = "authentication_required"
    FRAUD_RISK = "suspected_fraud"
    GATEWAY_TIMEOUT = "gateway_timeout"
    PROCESSOR_DECLINE = "processor_decline"
    TRANSACTION_NOT_ALLOWED = "transaction_not_allowed"
    LIMIT_EXCEEDED = "processing_limit_exceeded"
    UNKNOWN = "unknown"


# SYNTHETIC vocabulary (see module docstring). Keys are normalized reasons.
DEFAULT_REASON_CATEGORIES: dict[str, FailureCategory] = {c.value: c for c in FailureCategory
                                                         if c is not FailureCategory.UNKNOWN}

_CONTACT = frozenset({REMINDER, PAYMENT_UPDATE})
# Ordinary actions each category permits. Empty = escalation-only.
DEFAULT_CATEGORY_ACTIONS: dict[FailureCategory, frozenset[str]] = {
    FailureCategory.FUNDS: frozenset({RETRY, REMINDER, PAYMENT_UPDATE}),   # the only retry-eligible category
    FailureCategory.CARD_EXPIRED: _CONTACT,
    FailureCategory.INVALID_DETAILS: _CONTACT,
    FailureCategory.AUTH_REQUIRED: _CONTACT,
    FailureCategory.FRAUD_RISK: frozenset(),            # escalation only
    FailureCategory.GATEWAY_TIMEOUT: frozenset(),       # escalation only until reconciled
    FailureCategory.PROCESSOR_DECLINE: _CONTACT,
    FailureCategory.TRANSACTION_NOT_ALLOWED: _CONTACT,
    FailureCategory.LIMIT_EXCEEDED: _CONTACT,
    FailureCategory.UNKNOWN: _CONTACT,                  # no blind retry for unrecognised reasons
}


@dataclass(frozen=True)
class PolicyConfig:
    recovery_window_days: float = 14
    max_retries: int = 3
    min_retry_interval_hours: float = 24
    action_cooldown_hours: float = 24
    required_payment_status: str = "failed"
    eligible_subscription_statuses: frozenset[str] = frozenset({"active"})
    reason_categories: Mapping[str, FailureCategory] = field(
        default_factory=lambda: dict(DEFAULT_REASON_CATEGORIES))
    category_actions: Mapping[FailureCategory, frozenset[str]] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_ACTIONS))
    # Defensive hint only: an UNRECOGNISED reason containing one of these is treated as
    # fraud risk (escalation-only). It can only make the policy stricter.
    fraud_keywords: tuple[str, ...] = ("fraud", "stolen")

    def __post_init__(self):
        if self.recovery_window_days <= 0 or self.min_retry_interval_hours < 0 \
                or self.action_cooldown_hours < 0 or self.max_retries < 0:
            raise ValueError("policy thresholds must be non-negative (window must be positive)")


DEFAULT_CONFIG = PolicyConfig()


@dataclass(frozen=True)
class PriorAction:
    """A previously recorded simulated action for this payment."""
    action: str
    status: str
    created_at: datetime
    scheduled_for: datetime | None = None


@dataclass(frozen=True)
class PolicySnapshot:
    """Everything the policy may look at. Datetimes are naive UTC."""
    payment_id: str
    payment_status: str
    failure_reason: str | None
    failed_at: datetime            # NOTE: derived from payments.created_at, not a true failure time
    subscription_status: str
    now: datetime
    prior_actions: tuple[PriorAction, ...] = ()


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    action: str
    rule: str                      # the deciding rule: the blocking rule, or "always_allowed"/"all_rules_passed"
    reason: str
    category: str
    rules_evaluated: list[str]
    # Only filled for blocked decisions made through make_policy_check: actions that are allowed
    # right now, as guidance for the model. Advisory only; evaluate() is still the gate.
    alternatives: list[str] = []


def snapshot_from_context(context: RecoveryContext, now: datetime) -> PolicySnapshot:
    """The policy's view of a RecoveryContext, so the policy never needs a second copy of the data.

    The context builder only builds a context for a payment that is still failed, hence the fixed
    payment status. Prior actions come from the payment's recovery history: a retry's attempted_at is
    the time it is planned for; for every other action it is when it was recorded.
    """
    prior = tuple(
        PriorAction(action=item.strategy, status=item.status, created_at=item.attempted_at,
                    scheduled_for=item.attempted_at if item.strategy == RETRY else None)
        for item in context.recovery_history
    )
    return PolicySnapshot(
        payment_id=context.failed_payment.id,
        payment_status="failed",
        failure_reason=context.failed_payment.failure_reason,
        failed_at=context.failed_payment.failed_at,   # NOTE: payments.created_at, not a true failure time
        subscription_status=context.subscription.status,
        now=now,
        prior_actions=prior,
    )


def normalize_reason(reason: str | None) -> str | None:
    """Lower-case, trim, and join words with underscores. None/blank -> None."""
    if reason is None:
        return None
    normalized = re.sub(r"[\s\-]+", "_", reason.strip().lower())
    return normalized or None


def classify(reason: str | None, config: PolicyConfig = DEFAULT_CONFIG) -> FailureCategory:
    normalized = normalize_reason(reason)
    if normalized is None:
        return FailureCategory.UNKNOWN
    exact = config.reason_categories.get(normalized)
    if exact is not None:
        return exact
    if any(keyword in normalized for keyword in config.fraud_keywords):
        return FailureCategory.FRAUD_RISK
    return FailureCategory.UNKNOWN


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def evaluate(
    snapshot: PolicySnapshot,
    action: str,
    parameters: Mapping[str, Any] | None = None,
    config: PolicyConfig = DEFAULT_CONFIG,
) -> PolicyDecision:
    """Decide whether `action` (with already-validated `parameters`) is allowed now."""
    category = classify(snapshot.failure_reason, config)
    evaluated: list[str] = []

    def decide(allowed: bool, rule: str, reason: str) -> PolicyDecision:
        return PolicyDecision(allowed=allowed, action=action, rule=rule, reason=reason,
                              category=category.value, rules_evaluated=list(evaluated))

    def block(rule: str, reason: str) -> PolicyDecision:
        return decide(False, rule, reason)

    # 0. Safe fallbacks are always allowed.
    if action in SAFE_ACTIONS:
        evaluated.append("always_allowed")
        return decide(True, "always_allowed", f"{action} is a safe fallback and is always permitted")

    if action not in ORDINARY_ACTIONS:
        evaluated.append("known_action")
        return block("known_action", f"{action!r} is not a recognised recovery action")

    # 1. Payment must still be failed.
    evaluated.append("payment_must_be_failed")
    if _norm(snapshot.payment_status) != config.required_payment_status:
        return block("payment_must_be_failed",
                     f"Payment status is {snapshot.payment_status!r}, not {config.required_payment_status!r}")

    # 2. Subscription must be eligible.
    evaluated.append("subscription_eligible")
    if _norm(snapshot.subscription_status) not in config.eligible_subscription_statuses:
        return block("subscription_eligible",
                     f"Subscription status {snapshot.subscription_status!r} is not eligible "
                     f"(eligible: {sorted(config.eligible_subscription_statuses)})")

    # 3. Recovery window.
    evaluated.append("recovery_window")
    deadline = snapshot.failed_at + timedelta(days=config.recovery_window_days)
    if snapshot.now > deadline:
        return block("recovery_window",
                     f"Recovery window of {config.recovery_window_days:g} days ended at {deadline.isoformat()}")

    # 4. Failure category must permit the action.
    evaluated.append("failure_category")
    permitted = config.category_actions.get(category, frozenset())
    if action not in permitted:
        if action == RETRY:
            why = ("Automated retries are only permitted when the failure reason is exactly "
                   "'insufficient_funds'")
        else:
            why = f"Failure category {category.value!r} does not permit {action}"
        options = sorted(permitted | SAFE_ACTIONS)
        return block("failure_category", f"{why} (category: {category.value}). Permitted: {options}")

    # 5. Retry-specific rules.
    if action == RETRY:
        hours = (parameters or {}).get("retry_after_hours")
        evaluated.append("retry_parameters")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
            return block("retry_parameters", "retry_after_hours is missing or invalid")
        retry_time = snapshot.now + timedelta(hours=hours)
        retries = [p for p in snapshot.prior_actions
                   if p.action == RETRY and p.status in COUNTED_RETRY_STATUSES]

        evaluated.append("retry_not_already_scheduled")
        if any(p.status == "scheduled" for p in retries):
            return block("retry_not_already_scheduled", "A retry is already scheduled for this payment")

        evaluated.append("retry_cap")
        if len(retries) >= config.max_retries:
            return block("retry_cap",
                         f"{len(retries)} retries already recorded; the maximum is {config.max_retries}")

        evaluated.append("retry_interval")
        minimum = timedelta(hours=config.min_retry_interval_hours)
        for prior in retries:
            prior_time = prior.scheduled_for or prior.created_at
            if abs(retry_time - prior_time) < minimum:
                return block("retry_interval",
                             f"Retry at {retry_time.isoformat()} is within "
                             f"{config.min_retry_interval_hours:g}h of the retry at {prior_time.isoformat()}")

        evaluated.append("retry_within_window")
        if retry_time > deadline:
            return block("retry_within_window",
                         f"Retry at {retry_time.isoformat()} falls after the recovery window ({deadline.isoformat()})")

    # 6. Same-action cooldown.
    evaluated.append("action_cooldown")
    cooldown = timedelta(hours=config.action_cooldown_hours)
    for prior in snapshot.prior_actions:
        if prior.action == action and prior.status != "cancelled" and snapshot.now - prior.created_at < cooldown:
            return block("action_cooldown",
                         f"{action} was already recorded at {prior.created_at.isoformat()}; "
                         f"cooldown is {config.action_cooldown_hours:g}h")

    return decide(True, "all_rules_passed", f"{action} is permitted for category {category.value!r}")


RETRY_PROBE_HOURS = (24, 48, 72, 168)


def suggest_alternatives(snapshot: PolicySnapshot, blocked_action: str,
                         config: PolicyConfig = DEFAULT_CONFIG) -> list[str]:
    """Actions (other than `blocked_action`) that the policy would currently allow.

    Ordinary actions are included only if they pass evaluate(); a retry counts if any of a few
    representative delays passes. The safe fallbacks are always listed last.
    """
    options: list[str] = []
    for action in (RETRY, REMINDER, PAYMENT_UPDATE):
        if action == blocked_action:
            continue
        if action == RETRY:
            allowed = any(evaluate(snapshot, RETRY, {"retry_after_hours": h}, config).allowed
                          for h in RETRY_PROBE_HOURS)
        else:
            allowed = evaluate(snapshot, action, {}, config).allowed
        if allowed:
            options.append(action)
    return options + [ESCALATE, NO_ACTION]


def make_policy_check(
    snapshot: PolicySnapshot, config: PolicyConfig = DEFAULT_CONFIG
) -> Callable[[str, Mapping[str, Any]], PolicyDecision]:
    """Bind a snapshot into the (tool_name, validated_args) hook used before tool dispatch."""
    def check(tool_name: str, arguments: Mapping[str, Any]) -> PolicyDecision:
        decision = evaluate(snapshot, tool_name, arguments, config)
        if not decision.allowed:
            decision = decision.model_copy(
                update={"alternatives": suggest_alternatives(snapshot, tool_name, config)})
        return decision
    return check
