"""Read-only queries behind the dashboard. Every number is computed from persisted rows; nothing is hard-coded.

Business definitions (also returned by GET /dashboard/metrics so the UI can show them):
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import AgentDecision, Customer, Payment, RecoveryAttempt, Subscription
from app.gateway import CONFIRMED, DECLINED
from app.schemas.demo import (AttemptRow, AuditRecord, CountByAction, CountByReason, DashboardMetrics, DayPoint,
                              PaymentDetail, PaymentRow, RecentActivity)
from app.schemas.recovery import ToolCallRecord

DEFINITIONS = {
    "failed_payments": "Payments whose status is 'failed' (in the dashboard currency).",
    "revenue_at_risk": "The summed amount of those failed payments.",
    "revenue_recovered": ("The summed amount of payments that are now 'success' because the agent scheduled a retry and "
                          "the simulated processor CONFIRMED it. A scheduled or attempted retry alone is not revenue."),
    "recovery_success_rate": ("Recovered payments divided by payments the agent has produced a decision for. "
                              "Escalations, reminders and blocked decisions count in the denominator."),
    "retry_confirmation_rate": "Confirmed retries divided by retries the simulated processor has settled (confirmed or declined).",
    "escalations_open": "Failed payments whose latest recorded action is a human escalation.",
    "approved / blocked": "Decisions where the deterministic policy approved or blocked what the LLM proposed. Blocked proposals are never executed.",
}
__doc__ += "\n".join(f"  {k}: {v}" for k, v in DEFINITIONS.items())

_STATUS_BY_STRATEGY = {"schedule_retry": "retry_declined", "send_reminder": "reminder_sent",
                       "request_payment_update": "update_requested", "escalate_to_human": "escalated",
                       "no_action": "no_action"}
RECOVERY_STATUSES = ["unprocessed", "retry_scheduled", "retry_declined", "reminder_sent", "update_requested",
                     "escalated", "no_action", "blocked", "recovered", "paid"]


@dataclass
class State:
    rows: list[PaymentRow]
    attempts: dict[str, list[RecoveryAttempt]]
    decisions: list[AgentDecision]
    names: dict[str, str]


def _recovery_status(payment: Payment, attempts: list[RecoveryAttempt], decisions: list[AgentDecision]) -> str:
    confirmed = any(a.strategy == "schedule_retry" and CONFIRMED in (a.outcome or "") for a in attempts)
    if payment.status == "success":
        return "recovered" if confirmed else "paid"
    if any(a.strategy == "schedule_retry" and a.status == "scheduled" for a in attempts):
        return "retry_scheduled"
    latest = max(attempts, key=lambda a: (a.attempted_at, a.id)) if attempts else None
    if decisions and decisions[-1].policy_status == "blocked" and (latest is None or decisions[-1].created_at >= latest.attempted_at):
        return "blocked"                       # the agent's latest word was a block and nothing was done after it
    if latest is not None:
        return _STATUS_BY_STRATEGY.get(latest.strategy, "unprocessed")
    return "unprocessed"


def load_state(db: Session) -> State:
    attempts: dict[str, list[RecoveryAttempt]] = defaultdict(list)
    for a in db.query(RecoveryAttempt).all():
        attempts[a.payment_id].append(a)
    decisions_by: dict[str, list[AgentDecision]] = defaultdict(list)
    decisions = db.query(AgentDecision).order_by(AgentDecision.created_at, AgentDecision.id).all()
    for d in decisions:
        decisions_by[d.payment_id].append(d)
    rows, names = [], {}
    for payment, name in db.query(Payment, Customer.name).join(Customer, Customer.id == Payment.customer_id).all():
        names[payment.customer_id] = name
        mine = decisions_by.get(payment.id, [])
        last_attempt = max(attempts.get(payment.id, []), key=lambda a: (a.attempted_at, a.id), default=None)
        rows.append(PaymentRow(
            id=payment.id, customer_id=payment.customer_id, customer_name=name, amount=float(payment.amount),
            currency=payment.currency, status=payment.status, failure_reason=payment.failure_reason,
            created_at=payment.created_at, recovery_status=_recovery_status(payment, attempts.get(payment.id, []), mine),
            last_action=mine[-1].proposed_action if mine else (last_attempt.strategy if last_attempt else None),
            last_policy_status=mine[-1].policy_status if mine else None))
    rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return State(rows, attempts, decisions, names)


def list_payments(db: Session, *, status=None, recovery_status=None, failure_reason=None, search=None,
                  limit=50, offset=0) -> tuple[int, list[PaymentRow]]:
    rows = load_state(db).rows
    if status:
        rows = [r for r in rows if r.status == status]
    if recovery_status:
        rows = [r for r in rows if r.recovery_status == recovery_status]
    if failure_reason:
        rows = [r for r in rows if r.failure_reason == failure_reason]
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in r.id.lower() or needle in r.customer_name.lower() or needle in r.customer_id.lower()]
    return len(rows), rows[offset:offset + limit]


def _attempt_row(a: RecoveryAttempt) -> AttemptRow:
    return AttemptRow(id=a.id, strategy=a.strategy, status=a.status, outcome=a.outcome, attempted_at=a.attempted_at)


def _audit_record(d: AgentDecision, names: dict[str, str], attempts: dict[str, list[RecoveryAttempt]]) -> AuditRecord:
    audit = d.action_parameters if isinstance(d.action_parameters, dict) else {}
    linked = None
    if d.id.startswith("dec_"):
        linked = next((a for a in attempts.get(d.payment_id, []) if a.id == "att_" + d.id[4:]), None)
    return AuditRecord(
        decision_id=d.id, payment_id=d.payment_id, customer_id=d.customer_id,
        customer_name=names.get(d.customer_id, d.customer_id), proposed_action=d.proposed_action,
        parameters=audit.get("parameters"), policy_status=d.policy_status, policy_reason=d.policy_reason,
        explanation=d.decision_summary, failure_reason=audit.get("failure_reason"),
        attempts=[ToolCallRecord.model_validate(r) for r in audit.get("attempts", [])],
        simulated_action=_attempt_row(linked) if linked else None, created_at=d.created_at)


def audit_records(db: Session, payment_id: str | None = None, limit: int = 50) -> list[AuditRecord]:
    state = load_state(db)
    decisions = [d for d in state.decisions if payment_id is None or d.payment_id == payment_id]
    decisions.sort(key=lambda d: (d.created_at, d.id), reverse=True)
    return [_audit_record(d, state.names, state.attempts) for d in decisions[:limit]]


def payment_detail(db: Session, payment_id: str) -> PaymentDetail | None:
    state = load_state(db)
    row = next((r for r in state.rows if r.id == payment_id), None)
    if row is None:
        return None
    subscription = (db.query(Subscription).join(Payment, Payment.subscription_id == Subscription.id)
                    .filter(Payment.id == payment_id).first())
    decisions = sorted((d for d in state.decisions if d.payment_id == payment_id), key=lambda d: (d.created_at, d.id), reverse=True)
    return PaymentDetail(
        payment=row, subscription_plan=subscription.plan if subscription else None,
        recovery_attempts=[_attempt_row(a) for a in sorted(state.attempts.get(payment_id, []), key=lambda a: (a.attempted_at, a.id), reverse=True)],
        decisions=[_audit_record(d, state.names, state.attempts) for d in decisions],
        customer_recent_payments=[r for r in state.rows if r.customer_id == row.customer_id][:6])


def metrics(db: Session, now: datetime, currency: str, days: int = 14) -> DashboardMetrics:
    state = load_state(db)
    rows = [r for r in state.rows if r.currency == currency]
    ids = {r.id for r in rows}
    amount = {r.id: r.amount for r in rows}
    failed = [r for r in rows if r.status == "failed"]
    recovered = [r for r in rows if r.recovery_status == "recovered"]
    decisions = [d for d in state.decisions if d.payment_id in ids]
    processed = {d.payment_id for d in decisions}
    settled = [a for lst in state.attempts.values() for a in lst
               if a.payment_id in ids and a.strategy == "schedule_retry" and (CONFIRMED in (a.outcome or "") or DECLINED in (a.outcome or ""))]
    confirmed = [a for a in settled if CONFIRMED in (a.outcome or "")]

    by_action: dict[str, dict[str, int]] = defaultdict(lambda: {"approved": 0, "blocked": 0})
    for d in decisions:
        by_action[d.proposed_action]["blocked" if d.policy_status == "blocked" else "approved"] += 1
    reasons: dict[str, list[float]] = defaultdict(list)
    for r in failed:
        reasons[r.failure_reason or "unknown"].append(r.amount)

    today = now.date()
    buckets = {(today - timedelta(days=i)).isoformat(): [0, 0, 0.0] for i in range(days - 1, -1, -1)}
    for d in decisions:
        key = d.created_at.date().isoformat()
        if key in buckets:
            buckets[key][1 if d.policy_status == "blocked" else 0] += 1
    for a in confirmed:
        key = a.attempted_at.date().isoformat()
        if key in buckets and a.payment_id in amount:
            buckets[key][2] += amount[a.payment_id]

    recent = sorted(decisions, key=lambda d: (d.created_at, d.id), reverse=True)[:10]
    return DashboardMetrics(
        currency=currency, failed_payments=len(failed), revenue_at_risk=round(sum(r.amount for r in failed), 2),
        recovered_payments=len(recovered), revenue_recovered=round(sum(r.amount for r in recovered), 2),
        payments_processed=len(processed),
        recovery_success_rate=(len(recovered) / len(processed)) if processed else None,
        retries_settled=len(settled), retries_confirmed=len(confirmed),
        retry_confirmation_rate=(len(confirmed) / len(settled)) if settled else None,
        escalations_open=sum(1 for r in failed if r.recovery_status == "escalated"),
        decisions_total=len(decisions),
        decisions_approved=sum(1 for d in decisions if d.policy_status != "blocked"),
        decisions_blocked=sum(1 for d in decisions if d.policy_status == "blocked"),
        by_action=[CountByAction(action=k, **v) for k, v in sorted(by_action.items())],
        by_failure_reason=[CountByReason(reason=k, count=len(v), amount=round(sum(v), 2))
                           for k, v in sorted(reasons.items(), key=lambda kv: -len(kv[1]))],
        by_day=[DayPoint(date=k, approved=v[0], blocked=v[1], recovered_amount=round(v[2], 2)) for k, v in buckets.items()],
        recent_activity=[RecentActivity(decision_id=d.id, payment_id=d.payment_id,
                                        customer_name=state.names.get(d.customer_id, d.customer_id), action=d.proposed_action,
                                        policy_status=d.policy_status, summary=d.decision_summary, created_at=d.created_at)
                         for d in recent],
        definitions=DEFINITIONS)
