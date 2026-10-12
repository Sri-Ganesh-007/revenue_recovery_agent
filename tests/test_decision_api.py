from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Customer, Payment, Subscription
from app.llm.decision_maker import InvalidModelOutputError, OllamaUnavailableError
from app.llm.proposal import RecoveryProposal
from app.main import app, get_db, get_decision_maker

NOW = datetime(2026, 1, 1)


class StubDecisionMaker:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def propose(self, context):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    db = factory()
    db.add(Customer(id="c1", name="Jane", email="j@example.com", created_at=NOW))
    db.add(Subscription(id="s1", customer_id="c1", status="active", plan="pro",
                        amount=10, currency="USD", billing_freq="monthly"))
    db.add(Payment(id="p_failed", customer_id="c1", subscription_id="s1", amount=10,
                   currency="USD", status="failed", failure_reason="insufficient_funds",
                   created_at=NOW))
    db.add(Payment(id="p_ok", customer_id="c1", subscription_id="s1", amount=10,
                   currency="USD", status="success", created_at=NOW))
    db.commit()
    db.close()
    return factory


def make_client(session_factory, stub):
    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_decision_maker] = lambda: stub
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def good_proposal():
    return RecoveryProposal(payment_id="p_failed", action="schedule_retry",
                            parameters={"retry_after_hours": 24}, summary="Retry tomorrow.")


def test_returns_validated_proposal(session_factory):
    stub = StubDecisionMaker(result=good_proposal())
    r = make_client(session_factory, stub).post("/recovery/proposal", json={"payment_id": "p_failed"})
    assert r.status_code == 200
    assert r.json()["action"] == "schedule_retry"
    assert stub.calls == 1


def test_missing_payment_returns_404_without_calling_llm(session_factory):
    stub = StubDecisionMaker(result=good_proposal())
    r = make_client(session_factory, stub).post("/recovery/proposal", json={"payment_id": "nope"})
    assert r.status_code == 404
    assert stub.calls == 0


def test_non_failed_payment_returns_409_without_calling_llm(session_factory):
    stub = StubDecisionMaker(result=good_proposal())
    r = make_client(session_factory, stub).post("/recovery/proposal", json={"payment_id": "p_ok"})
    assert r.status_code == 409
    assert stub.calls == 0


def test_invalid_model_output_returns_502(session_factory):
    stub = StubDecisionMaker(error=InvalidModelOutputError("bad"))
    r = make_client(session_factory, stub).post("/recovery/proposal", json={"payment_id": "p_failed"})
    assert r.status_code == 502


def test_ollama_unavailable_returns_503(session_factory):
    stub = StubDecisionMaker(error=OllamaUnavailableError("down"))
    r = make_client(session_factory, stub).post("/recovery/proposal", json={"payment_id": "p_failed"})
    assert r.status_code == 503


def test_missing_payment_id_field_returns_422(session_factory):
    r = make_client(session_factory, StubDecisionMaker()).post("/recovery/proposal", json={})
    assert r.status_code == 422
