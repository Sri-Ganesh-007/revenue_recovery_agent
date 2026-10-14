"""Mock payment gateway. SIMULATION ONLY: it never charges a card or sends a message.

It plays two roles for the demo:
  * create_event  - the "gateway" announces a payment outcome by creating a SYNTHETIC payment row;
  * settle_retry  - the "processor" confirms or declines a retry that the agent scheduled.

It only ever creates or changes rows whose ids start with a synthetic prefix, so it cannot touch real data.
The agent itself (app/recovery) is untouched: events are processed by the existing /recovery/simulate flow.
"""
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import Customer, Payment, RecoveryAttempt, Subscription
from app.repositories.recovery_repository import RecoveryRepository

SYNTHETIC_PAYMENT_PREFIXES = ("demo_", "pay_demo_", "gw_")
SYNTHETIC_CUSTOMER_PREFIXES = ("demo_", "cust_demo_", "gw_")

CONFIRMED = "SIMULATED: processor CONFIRMED payment"
DECLINED = "SIMULATED: processor DECLINED retry"

# what each kind of event looks like
KINDS = {
    "insufficient_funds": {"status": "failed", "reason": "insufficient_funds"},
    "gateway_timeout": {"status": "failed", "reason": "gateway_timeout"},
    "expired_card": {"status": "failed", "reason": "expired_card"},
    "authentication_required": {"status": "failed", "reason": "authentication_required"},
    "processor_decline": {"status": "failed", "reason": "processor_decline"},
    "suspected_fraud": {"status": "failed", "reason": "suspected_fraud"},
    "success": {"status": "success", "reason": None},
    "retry_limit": {"status": "failed", "reason": "insufficient_funds", "prior_retries": 3},
}


class GatewayError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(f"{status_code}: {detail}")


@dataclass
class GatewayEventRecord:
    kind: str
    payment_id: str
    customer_id: str
    customer_name: str
    plan: str
    amount: float
    currency: str
    payment_status: str
    failure_reason: str | None
    received_at: datetime
    prior_retries: int


def is_synthetic_payment(payment_id: str) -> bool:
    return payment_id.startswith(SYNTHETIC_PAYMENT_PREFIXES)


def _pick_customer(db: Session, rng: random.Random, now: datetime):
    """A synthetic customer with an active subscription; creates one if the database has none yet."""
    like = [Customer.id.like(p.replace("_", "\\_") + "%", escape="\\") for p in SYNTHETIC_CUSTOMER_PREFIXES]
    rows = (db.query(Customer, Subscription)
            .join(Subscription, Subscription.customer_id == Customer.id)
            .filter(Subscription.status == "active", or_(*like))
            .order_by(Customer.id).all())
    if rows:
        return rng.choice(rows)
    suffix = uuid.uuid4().hex[:8]
    customer = Customer(id=f"gw_cust_{suffix}", name=f"Demo Customer {suffix[:4].upper()}",
                        email=f"gw_cust_{suffix}@demo.example", created_at=now)
    db.add(customer)
    db.flush()
    subscription = Subscription(id=f"gw_sub_{suffix}", customer_id=customer.id, status="active", plan="Demo Monthly",
                                amount=499, currency="INR", billing_freq="monthly")
    db.add(subscription)
    db.flush()
    return customer, subscription


def create_event(db: Session, kind: str, now: datetime, rng: random.Random | None = None) -> GatewayEventRecord:
    """The gateway announces a payment outcome: one synthetic payment row (plus prior retries for retry_limit)."""
    spec = KINDS[kind]
    rng = rng or random.Random()
    customer, subscription = _pick_customer(db, rng, now)
    prior = spec.get("prior_retries", 0)
    created_at = now - timedelta(days=6) if prior else now
    payment_id = f"gw_pay_{uuid.uuid4().hex[:10]}"

    db.add(Payment(id=payment_id, customer_id=customer.id, subscription_id=subscription.id,
                   amount=subscription.amount, currency=subscription.currency, status=spec["status"],
                   failure_reason=spec["reason"], created_at=created_at))
    db.flush()
    for i, days_ago in enumerate((5.0, 3.5, 2.0)[:prior], start=1):       # earlier retries the processor declined
        db.add(RecoveryAttempt(id=f"{payment_id}_r{i}", customer_id=customer.id, payment_id=payment_id,
                               strategy="schedule_retry", status="executed_simulated", outcome=DECLINED,
                               attempted_at=now - timedelta(days=days_ago)))
    db.commit()
    return GatewayEventRecord(kind=kind, payment_id=payment_id, customer_id=customer.id, customer_name=customer.name,
                              plan=subscription.plan, amount=float(subscription.amount), currency=subscription.currency,
                              payment_status=spec["status"], failure_reason=spec["reason"], received_at=created_at,
                              prior_retries=prior)


def settle_retry(db: Session, payment_id: str, outcome: str, now: datetime) -> None:
    """The mock processor settles the agent's scheduled retry: 'success' confirms it, 'failure' declines it.

    Revenue counts as recovered only here, when the (simulated) processor confirms. Only synthetic payments."""
    if not is_synthetic_payment(payment_id):
        raise GatewayError(403, "The mock processor only settles synthetic payments")
    repo = RecoveryRepository(db)
    payment = repo.lock_payment(payment_id)
    if payment is None:
        raise GatewayError(404, f"Payment {payment_id} not found")
    if payment.status != "failed":
        raise GatewayError(409, f"Payment {payment_id} is not failed (current status: {payment.status})")
    attempt = (db.query(RecoveryAttempt)
               .filter(RecoveryAttempt.payment_id == payment_id, RecoveryAttempt.strategy == "schedule_retry",
                       RecoveryAttempt.status == "scheduled")
               .order_by(RecoveryAttempt.attempted_at.desc()).first())
    if attempt is None:
        raise GatewayError(409, "There is no scheduled retry to settle for this payment")
    attempt.status = "executed_simulated"
    attempt.attempted_at = now                      # the retry has now (virtually) happened
    if outcome == "success":
        payment.status = "success"
        attempt.outcome = CONFIRMED
    else:
        attempt.outcome = DECLINED
    db.commit()
