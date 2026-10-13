"""Fixtures for service/API tests: an in-memory SQLite copy of the ORIGINAL five tables, with fabricated data."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Customer, Payment, Subscription
from app.recovery.decision_maker import InvalidModelOutputError
from app.schemas.recovery import DecisionEnvelope

FIXED_NOW = datetime(2026, 10, 12, 12, 0, 0)


def clock():
    return FIXED_NOW


def make_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)


def seed(db, *, payment_id="pay_1", failure_reason="insufficient_funds", subscription_status="active",
         payment_status="failed", extra_payment=None):
    created = FIXED_NOW - timedelta(days=1)
    db.add(Customer(id="c1", name="Jane Doe", email="jane@example.com", created_at=created))
    db.add(Subscription(id="s1", customer_id="c1", status=subscription_status, plan="Premium", amount=499,
                        currency="INR", billing_freq="monthly"))
    db.flush()
    for pid, status in [(payment_id, payment_status)] + ([(extra_payment, "failed")] if extra_payment else []):
        db.add(Payment(id=pid, customer_id="c1", subscription_id="s1", amount=499, currency="INR", status=status,
                       failure_reason=failure_reason, created_at=created))
    db.commit()


def counts(db):
    return {t: db.execute(text(f"select count(*) from {t}")).scalar()
            for t in ("agent_decisions", "recovery_attempts", "payments")}


def payments_rows(db):
    return [tuple(r) for r in db.execute(text("select * from payments order by id"))]


# ---- decisions the fake LLM can return

def _decision(**fields):
    return DecisionEnvelope.model_validate({"decision": {"explanation": "Because.", **fields}}).decision


def retry(hours=24, explanation="Funds were short."):
    return _decision(action="schedule_retry", retry_after_hours=hours, explanation=explanation)


def update():
    return _decision(action="request_payment_update")


def reminder(tone="friendly"):
    return _decision(action="send_reminder", tone=tone)


def escalate(reason="needs a person"):
    return _decision(action="escalate_to_human", reason=reason)


def wait(reason="nothing useful now"):
    return _decision(action="no_action", reason=reason)


class FakeDecisionMaker:
    """Stands in for the LLM: returns scripted decisions (or raises scripted errors) and records every call."""

    def __init__(self, *script, on_call=None):
        self.script, self.on_call = list(script), on_call
        self.contexts, self.feedbacks = [], []

    @property
    def calls(self):
        return len(self.contexts)

    def propose(self, context, feedback=None):
        index = self.calls
        self.contexts.append(context)
        self.feedbacks.append(feedback)
        if self.on_call:
            self.on_call(index)
        if not self.script:
            raise AssertionError("the LLM was called more often than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def invalid(message="schedule_retry needs retry_after_hours"):
    return InvalidModelOutputError(message)
