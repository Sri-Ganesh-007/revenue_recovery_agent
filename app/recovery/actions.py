"""Simulated actions. Nothing here charges a card, sends an email or contacts anyone.

simulate_action() only builds a description of what WOULD happen. The service records it in
PostgreSQL after the deterministic policy has approved the action.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

NOTE = "No real payment, email, ticket or database change was made."


@dataclass(frozen=True)
class SimulatedOutcome:
    action: str
    status: str                       # "scheduled" (a planned retry), "executed_simulated", or "noop"
    scheduled_for: datetime | None    # only for a planned retry
    outcome: str                      # short text for recovery_attempts.outcome (column limit: 100 characters)
    result: dict                      # full simulated result, kept in the decision's audit record


def simulate_action(action: str, parameters: dict, payment_id: str, now: datetime) -> SimulatedOutcome:
    scheduled_for = None
    status = "executed_simulated"
    if action == "schedule_retry":
        hours = parameters["retry_after_hours"]
        scheduled_for, status = now + timedelta(hours=hours), "scheduled"
        detail = f"Retry would be scheduled in {hours} hours (not scheduled for real)."
        outcome = f"SIMULATED: retry planned in {hours}h (no real charge)"
    elif action == "request_payment_update":
        detail = "A request for new payment details would be sent (not sent)."
        outcome = "SIMULATED: payment-details request recorded (no email sent)"
    elif action == "send_reminder":
        tone = parameters["tone"]
        detail = f"A {tone} reminder would be sent (not sent)."
        outcome = f"SIMULATED: {tone} reminder recorded (no email sent)"
    elif action == "escalate_to_human":
        detail = f"A human review would be requested: {parameters['reason']} (no ticket created)."
        outcome = "SIMULATED: human review requested (no ticket created)"
    elif action == "no_action":
        status = "noop"
        detail = f"No action taken: {parameters['reason']}"
        outcome = "SIMULATED: waiting, no action taken"
    else:
        raise ValueError(f"Unknown recovery action {action!r}")

    result = {"simulated": True, "status": "simulated_ok", "tool": action, "payment_id": payment_id,
              "detail": detail, "note": NOTE}
    return SimulatedOutcome(action, status, scheduled_for, outcome, result)
