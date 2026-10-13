"""POST /recovery/simulate, POST /recovery/proposal and GET /health over HTTP (fake LLM, in-memory SQLite)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.models import AgentDecision, RecoveryAttempt
from app.main import app, get_db, get_decision_maker
from app.recovery.decision_maker import InvalidModelOutputError, OllamaUnavailableError
from tests.service_fixtures import FakeDecisionMaker, escalate, make_factory, retry, seed, wait

RESPONSE_FIELDS = {"simulated", "payment_id", "action_taken", "stopped_reason", "tool_calls", "final_message",
                   "final_message_is_narrative_only", "run_id", "idempotency_key", "run_status", "idempotent_replay",
                   "decision", "simulated_action", "message"}


@pytest.fixture
def factory():
    f = make_factory()
    db = f(); seed(db, extra_payment="pay_2"); db.close()
    return f


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def client(factory, maker):
    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_decision_maker] = lambda: maker
    return TestClient(app)


def simulate(c, payment="pay_1", key=None):
    headers = {} if key is None else {"Idempotency-Key": key}
    return c.post("/recovery/simulate", json={"payment_id": payment}, headers=headers)


def rows(factory):
    db = factory()
    out = (db.query(AgentDecision).count(), db.query(RecoveryAttempt).count())
    db.close()
    return out


# ------------------------------------------------------------------ /recovery/simulate

def test_simulate_returns_every_contract_field(factory):
    r = simulate(client(factory, FakeDecisionMaker(retry(24))), key="k1")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == RESPONSE_FIELDS and body["simulated"] is True and body["final_message_is_narrative_only"] is True
    assert (body["run_status"], body["action_taken"], body["stopped_reason"]) == ("completed", "schedule_retry", "completed")
    assert body["idempotency_key"] == "k1" and body["run_id"] == body["decision"]["id"] and body["idempotent_replay"] is False
    assert body["decision"]["policy_status"] == "approved" and body["simulated_action"]["status"] == "scheduled"
    assert body["tool_calls"][0].keys() == {"name", "arguments", "status", "result", "error", "policy"}
    assert body["tool_calls"][0]["policy"].keys() >= {"allowed", "rule", "reason", "category", "rules_evaluated", "alternatives"}
    assert r.headers["Idempotent-Replayed"] == "false"


def test_a_repeated_request_returns_the_saved_result(factory):
    c = client(factory, FakeDecisionMaker(retry(24)))                  # a second LLM call would fail
    first, second = simulate(c, key="k1"), simulate(c, key="k1")
    assert second.headers["Idempotent-Replayed"] == "true" and second.json()["idempotent_replay"] is True
    assert {**second.json(), "idempotent_replay": False} == first.json() and rows(factory) == (1, 1)


def test_a_key_for_a_different_payment_is_422(factory):
    c = client(factory, FakeDecisionMaker(retry(24)))
    simulate(c, key="k1")
    r = simulate(c, payment="pay_2", key="k1")
    assert r.status_code == 422 and "different request" in r.json()["detail"] and rows(factory) == (1, 1)


@pytest.mark.parametrize("key", ["has space", "x" * 201, "a/b", "semi;colon", "a,b"])
def test_invalid_idempotency_keys_are_rejected(factory, key):
    assert simulate(client(factory, FakeDecisionMaker()), key=key).status_code == 422


def test_valid_key_characters_are_accepted(factory):
    assert simulate(client(factory, FakeDecisionMaker(wait())), key="A1_b-2.c:3").status_code == 200


def test_everything_blocked_is_still_http_200_with_nothing_executed(factory):
    db = factory(); db.execute(__import__("sqlalchemy").text("update payments set failure_reason='suspected_fraud'")); db.commit(); db.close()
    r = simulate(client(factory, FakeDecisionMaker(retry(24), retry(48))), key="k1")
    body = r.json()
    assert r.status_code == 200 and (body["run_status"], body["action_taken"], body["simulated_action"]) == ("blocked", None, None)
    assert rows(factory) == (1, 0)


def test_a_fallback_after_a_block_is_recorded_as_the_action(factory):
    db = factory(); db.execute(__import__("sqlalchemy").text("update payments set failure_reason='suspected_fraud'")); db.commit(); db.close()
    body = simulate(client(factory, FakeDecisionMaker(retry(24), escalate("fraud"))), key="k1").json()
    assert body["action_taken"] == "escalate_to_human" and [t["status"] for t in body["tool_calls"]] == ["rejected", "executed_simulated"]


@pytest.mark.parametrize("maker,payment,status", [
    (FakeDecisionMaker(), "nope", 404),
    (FakeDecisionMaker(OllamaUnavailableError("down")), "pay_1", 503),
    (FakeDecisionMaker(InvalidModelOutputError("a"), InvalidModelOutputError("b")), "pay_1", 502),
])
def test_errors_map_to_http_codes_and_save_nothing(factory, maker, payment, status):
    r = simulate(client(factory, maker), payment=payment, key="k1")
    assert r.status_code == status and rows(factory) == (0, 0)


def test_a_non_failed_payment_is_409(factory):
    db = factory(); db.execute(__import__("sqlalchemy").text("update payments set status='success' where id='pay_2'")); db.commit(); db.close()
    assert simulate(client(factory, FakeDecisionMaker()), payment="pay_2").status_code == 409


def test_a_missing_payment_id_is_422(factory):
    assert client(factory, FakeDecisionMaker()).post("/recovery/simulate", json={}).status_code == 422


# ------------------------------------------------------------------ /recovery/proposal (contract unchanged)

def test_proposal_contract_and_nothing_is_saved(factory):
    r = client(factory, FakeDecisionMaker(retry(24, "Funds were short."))).post("/recovery/proposal", json={"payment_id": "pay_1"})
    assert r.status_code == 200
    assert r.json() == {"payment_id": "pay_1", "action": "schedule_retry", "parameters": {"retry_after_hours": 24},
                        "summary": "Funds were short."}
    assert rows(factory) == (0, 0)


@pytest.mark.parametrize("maker,payment,status", [
    (FakeDecisionMaker(), "nope", 404),
    (FakeDecisionMaker(OllamaUnavailableError("down")), "pay_1", 503),
    (FakeDecisionMaker(InvalidModelOutputError("bad")), "pay_1", 502),
])
def test_proposal_errors(factory, maker, payment, status):
    assert client(factory, maker).post("/recovery/proposal", json={"payment_id": payment}).status_code == status


def test_proposal_for_a_non_failed_payment_is_409(factory):
    db = factory(); db.execute(__import__("sqlalchemy").text("update payments set status='success'")); db.commit(); db.close()
    assert client(factory, FakeDecisionMaker()).post("/recovery/proposal", json={"payment_id": "pay_1"}).status_code == 409


# ------------------------------------------------------------------ misc

def test_root_and_health(factory, monkeypatch):
    c = client(factory, FakeDecisionMaker())
    assert c.get("/").json() == {"msg": "app is on air"}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, request=httpx.Request("GET", "http://x")))
    assert c.get("/health").json() == {"status": "ok", "database": "ok", "ollama": "ok"}


def test_health_is_degraded_not_down_when_only_ollama_is_unreachable(factory, monkeypatch):
    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refuse)
    r = client(factory, FakeDecisionMaker()).get("/health")
    assert r.status_code == 200 and r.json() == {"status": "degraded", "database": "ok", "ollama": "unavailable"}


def test_health_is_503_when_the_database_is_unreachable(monkeypatch):
    class BrokenSession:
        def execute(self, *a, **k):
            raise RuntimeError("no database")

        def close(self):
            pass

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, request=httpx.Request("GET", "http://x")))
    r = TestClient(app).get("/health")
    assert r.status_code == 503 and r.json()["database"] == "unavailable"
