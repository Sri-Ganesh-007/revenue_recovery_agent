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


# ---- POST /recovery/simulate ----

class StubToolModel:
    def __init__(self, messages=None, error=None):
        self.messages, self.error = list(messages or []), error

    def chat(self, messages, tools=None):
        if self.error:
            raise self.error
        return self.messages.pop(0)


def test_simulate_returns_labelled_simulated_result(session_factory):
    stub = StubToolModel([
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "schedule_retry", "arguments": {"retry_after_hours": 24}}}]},
        {"role": "assistant", "content": "A retry was scheduled."},
    ])
    r = make_client(session_factory, stub).post("/recovery/simulate", json={"payment_id": "p_failed"})
    assert r.status_code == 200
    body = r.json()
    assert body["simulated"] is True
    assert body["payment_id"] == "p_failed"
    assert body["action_taken"] == "schedule_retry"
    assert body["tool_calls"][0]["status"] == "executed_simulated"
    assert body["final_message_is_narrative_only"] is True


def test_simulate_does_not_write_to_the_database(session_factory):
    stub = StubToolModel([
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "request_payment_update", "arguments": {}}}]},
        {"role": "assistant", "content": "ok"},
    ])
    make_client(session_factory, stub).post("/recovery/simulate", json={"payment_id": "p_failed"})
    db = session_factory()
    from app.db.models import AgentDecision, RecoveryAttempt, Payment
    assert db.query(AgentDecision).count() == 0
    assert db.query(RecoveryAttempt).count() == 0
    assert db.query(Payment).filter(Payment.id == "p_failed").one().status == "failed"
    db.close()


def test_simulate_missing_payment_404_and_non_failed_409(session_factory):
    client = make_client(session_factory, StubToolModel())
    assert client.post("/recovery/simulate", json={"payment_id": "nope"}).status_code == 404
    assert client.post("/recovery/simulate", json={"payment_id": "p_ok"}).status_code == 409


def test_simulate_ollama_down_503(session_factory):
    client = make_client(session_factory, StubToolModel(error=OllamaUnavailableError("down")))
    assert client.post("/recovery/simulate", json={"payment_id": "p_failed"}).status_code == 503


def test_simulate_invalid_model_output_502(session_factory):
    client = make_client(session_factory, StubToolModel([{"role": "assistant", "content": "just text"}]))
    assert client.post("/recovery/simulate", json={"payment_id": "p_failed"}).status_code == 502
