"""SYNTHETIC fixtures for deterministic policy tests.

These scenarios are labels chosen for testing the policy. They are NOT verified
processor or gateway codes and do not describe real gateway behaviour. Only
"insufficient_funds" appears in the development database.

Representation limits (documented, not assumed away):
- failed_at is payments.created_at; the schema has no separate failure timestamp.
- payments.status values other than "failed"/"success" (e.g. an unresolved gateway
  timeout stored as "pending") are not known to exist, so a timeout is modelled only
  as status "failed" with reason "gateway_timeout".
- There is no card-expiry, authentication or fraud-flag field; each of those cases is
  represented only through the free-text failure_reason.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.recovery.policy import (ESCALATE, NO_ACTION, PAYMENT_UPDATE, REMINDER, RETRY,
                                 FailureCategory as C, PolicySnapshot, PriorAction)

NOW = datetime(2026, 10, 12, 12, 0, 0)
RETRY_PARAMS = {"retry_after_hours": 48}
ORDINARY = (RETRY, REMINDER, PAYMENT_UPDATE)
SAFE = (NO_ACTION, ESCALATE)


def params_for(action):
    return dict(RETRY_PARAMS) if action == RETRY else {}


def make_snapshot(**overrides) -> PolicySnapshot:
    values = dict(payment_id="pay_syn", payment_status="failed", failure_reason="insufficient_funds",
                  failed_at=NOW - timedelta(days=1), subscription_status="active", now=NOW,
                  prior_actions=())
    values.update(overrides)
    return PolicySnapshot(**values)


def prior_retry(created_ago_hours, scheduled_offset_hours, status="executed_simulated"):
    return PriorAction(RETRY, status, NOW - timedelta(hours=created_ago_hours),
                       NOW + timedelta(hours=scheduled_offset_hours))


def prior_action(action, created_ago_hours, status="executed_simulated"):
    return PriorAction(action, status, NOW - timedelta(hours=created_ago_hours), None)


@dataclass(frozen=True)
class Scenario:
    id: str
    failure_reason: str | None
    category: C
    allowed_ordinary: frozenset   # ordinary actions allowed in an otherwise clean state


# SYNTHETIC. Expected values are written by hand, independent of the policy's own tables.
CONTACT = frozenset({REMINDER, PAYMENT_UPDATE})
SCENARIOS = [
    Scenario("insufficient_funds", "insufficient_funds", C.FUNDS, frozenset({RETRY, REMINDER, PAYMENT_UPDATE})),
    Scenario("expired_card", "expired_card", C.CARD_EXPIRED, CONTACT),
    Scenario("invalid_payment_details", "invalid_payment_details", C.INVALID_DETAILS, CONTACT),
    Scenario("authentication_required", "authentication_required", C.AUTH_REQUIRED, CONTACT),
    Scenario("suspected_fraud", "suspected_fraud", C.FRAUD_RISK, frozenset()),
    Scenario("gateway_timeout", "gateway_timeout", C.GATEWAY_TIMEOUT, frozenset()),
    Scenario("processor_decline", "processor_decline", C.PROCESSOR_DECLINE, CONTACT),
    Scenario("transaction_not_allowed", "transaction_not_allowed", C.TRANSACTION_NOT_ALLOWED, CONTACT),
    Scenario("processing_limit_exceeded", "processing_limit_exceeded", C.LIMIT_EXCEEDED, CONTACT),
    Scenario("missing_reason", None, C.UNKNOWN, CONTACT),
    Scenario("blank_reason", "   ", C.UNKNOWN, CONTACT),
    Scenario("unrecognised_reason", "some_new_processor_code", C.UNKNOWN, CONTACT),
]
