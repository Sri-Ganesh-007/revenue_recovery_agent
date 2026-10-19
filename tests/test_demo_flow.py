"""The demo console end to end, on in-memory SQLite: mock gateway -> existing /recovery/simulate -> policy ->
simulated action -> persisted rows -> dashboard numbers. The LLM is scripted; everything else is the real code."""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.db.models import AgentDecision, Customer, Payment, RecoveryAttempt, Subscription
from app.demo_access import reset_budgets
from app.deps import get_db, get_decision_maker
from app.main import app
from tests.service_fixtures import FakeDecisionMaker, escalate, make_factory, retry, wait


class AlwaysWait(FakeDecisionMaker):
    """An LLM that always proposes no_action (the policy always allows it)."""

    def propose(self, context, feedback=None):
        self.contexts.append(context)
        self.feedbacks.append(feedback)
        return wait("demo")


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "demo_batch_enabled", True)
    monkeypatch.setattr(settings, "demo_batch_max", 5)
    reset_budgets()
    return make_factory()


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()
    reset_budgets()


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


def event(c, kind):
    r = c.post("/gateway/events", json={"kind": kind})
    assert r.status_code == 200, r.text
    return r.json()


def simulate(c, payment_id, key=None):
    return c.post("/recovery/simulate", json={"payment_id": payment_id}, headers={"Idempotency-Key": key or f"gw-{payment_id}"})


def counts(factory):
    db = factory()
    out = (db.query(AgentDecision).count(), db.query(RecoveryAttempt).count())
    db.close()
    return out


# ============================================================ the five priority scenarios

def test_eligible_insufficient_funds_retry_with_a_successful_mock_outcome(factory):
    c = client(factory, FakeDecisionMaker(retry(24, "Funds were short.")))
    ev = event(c, "insufficient_funds")
    pid = ev["event"]["payment_id"]
    assert pid.startswith("gw_pay_") and ev["event"]["payment_status"] == "failed" and ev["eligible_for_recovery"] is True
    assert ev["context_preview"]["failed_payment"]["failure_reason"] == "insufficient_funds"
    assert "name" not in ev["context_preview"]["customer"] and "email" not in ev["context_preview"]["customer"]
    assert ev["retry_policy_preview"]["allowed"] is True

    result = simulate(c, pid).json()                              # the EXISTING endpoint runs the agent
    assert (result["run_status"], result["action_taken"]) == ("completed", "schedule_retry")

    m = c.get("/dashboard/metrics").json()                        # scheduled is NOT recovered revenue
    assert (m["failed_payments"], m["revenue_at_risk"], m["revenue_recovered"], m["recovered_payments"]) == (1, 499.0, 0.0, 0)
    assert c.get(f"/payments/{pid}").json()["payment"]["recovery_status"] == "retry_scheduled"

    settled = c.post(f"/gateway/payments/{pid}/settle", json={"outcome": "success"}).json()
    assert settled["payment"]["status"] == "success" and settled["payment"]["recovery_status"] == "recovered"
    assert settled["recovery_attempts"][0]["outcome"] == "SIMULATED: processor CONFIRMED payment"

    m = c.get("/dashboard/metrics").json()                        # now, and only now, it is recovered revenue
    assert (m["failed_payments"], m["revenue_at_risk"], m["revenue_recovered"], m["recovered_payments"]) == (0, 0.0, 499.0, 1)
    assert (m["recovery_success_rate"], m["retry_confirmation_rate"], m["retries_confirmed"]) == (1.0, 1.0, 1)
    audit = c.get("/audit", params={"payment_id": pid}).json()
    assert audit[0]["simulated_action"]["outcome"].endswith("CONFIRMED payment") and audit[0]["policy_status"] == "approved"


def test_a_declined_retry_keeps_the_payment_failed_and_recovers_nothing(factory):
    c = client(factory, FakeDecisionMaker(retry(24)))
    pid = event(c, "insufficient_funds")["event"]["payment_id"]
    simulate(c, pid)
    detail = c.post(f"/gateway/payments/{pid}/settle", json={"outcome": "failure"}).json()
    assert detail["payment"]["status"] == "failed" and detail["payment"]["recovery_status"] == "retry_declined"
    m = c.get("/dashboard/metrics").json()
    assert (m["failed_payments"], m["revenue_recovered"], m["retries_settled"], m["retries_confirmed"]) == (1, 0.0, 1, 0)
    assert m["retry_confirmation_rate"] == 0.0


def test_gateway_timeout_requires_escalation_and_leaves_a_visible_record(factory):
    maker = FakeDecisionMaker(retry(24), escalate("A timeout does not prove the payment failed."))
    c = client(factory, maker)
    ev = event(c, "gateway_timeout")
    pid = ev["event"]["payment_id"]
    assert ev["retry_policy_preview"]["allowed"] is False and ev["retry_policy_preview"]["rule"] == "failure_category"

    result = simulate(c, pid).json()
    assert result["action_taken"] == "escalate_to_human" and maker.calls == 2           # one block, one re-ask
    assert [t["status"] for t in result["tool_calls"]] == ["rejected", "executed_simulated"]

    db = factory()
    rows = db.query(RecoveryAttempt).filter(RecoveryAttempt.payment_id == pid).all()
    assert [(r.strategy, r.status) for r in rows] == [("escalate_to_human", "executed_simulated")]     # persisted escalation record
    assert db.query(RecoveryAttempt).filter(RecoveryAttempt.strategy == "schedule_retry").count() == 0  # the retry never ran
    db.close()
    queue = c.get("/payments", params={"recovery_status": "escalated"}).json()
    assert [p["id"] for p in queue["items"]] == [pid]
    assert c.get("/dashboard/metrics").json()["escalations_open"] == 1


def test_an_already_successful_payment_is_rejected_for_recovery_before_the_llm(factory):
    maker = FakeDecisionMaker(retry(24))
    c = client(factory, maker)
    ev = event(c, "success")
    assert ev["eligible_for_recovery"] is False and "not failed" in ev["ineligible_reason"]
    assert ev["context_preview"] is None and ev["retry_policy_preview"] is None
    r = simulate(c, ev["event"]["payment_id"])
    assert r.status_code == 409 and "not failed" in r.json()["detail"]
    assert maker.calls == 0 and counts(factory) == (0, 0)


def test_the_retry_limit_blocks_a_fourth_retry(factory):
    maker = FakeDecisionMaker(retry(24), retry(48))
    c = client(factory, maker)
    ev = event(c, "retry_limit")
    pid = ev["event"]["payment_id"]
    assert ev["event"]["prior_retries"] == 3
    assert ev["retry_policy_preview"]["allowed"] is False and ev["retry_policy_preview"]["rule"] == "retry_cap"
    assert len(ev["context_preview"]["recovery_history"]) == 3                       # the agent can see the earlier retries

    result = simulate(c, pid).json()
    assert (result["run_status"], result["action_taken"], result["simulated_action"]) == ("blocked", None, None)
    assert {t["policy"]["rule"] for t in result["tool_calls"]} == {"retry_cap"} and maker.calls == 2
    db = factory()
    assert db.query(RecoveryAttempt).filter(RecoveryAttempt.payment_id == pid).count() == 3     # still only the 3 earlier ones
    db.close()
    assert c.get(f"/payments/{pid}").json()["payment"]["recovery_status"] == "blocked"


def test_idempotency_prevents_duplicate_processing(factory):
    maker = FakeDecisionMaker(retry(24))
    c = client(factory, maker)
    pid = event(c, "insufficient_funds")["event"]["payment_id"]
    first, second = simulate(c, pid), simulate(c, pid)
    assert second.headers["Idempotent-Replayed"] == "true" and second.json()["run_id"] == first.json()["run_id"]
    assert maker.calls == 1 and counts(factory) == (1, 1)


# ============================================================ safety guards

def test_the_gateway_write_endpoints_are_off_unless_demo_mode_is_on(factory, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    c = client(factory, FakeDecisionMaker())
    assert c.post("/gateway/events", json={"kind": "success"}).status_code == 403
    assert c.post("/gateway/payments/gw_pay_x/settle", json={"outcome": "success"}).status_code == 403
    assert c.post("/gateway/batch", json={"count": 1}).status_code == 403
    assert c.get("/gateway/status").json()["demo_mode"] is False
    assert c.get("/dashboard/metrics").status_code == 200 and c.get("/payments").status_code == 200       # reads still work


def test_the_mock_processor_refuses_real_payments_and_unscheduled_retries(factory):
    db = factory()
    db.add(Customer(id="real_c1", name="Real", email="r@example.com", created_at=__import__("datetime").datetime(2026, 1, 1)))
    db.add(Subscription(id="real_s1", customer_id="real_c1", status="active", plan="p", amount=10, currency="INR", billing_freq="monthly"))
    db.flush()
    db.add(Payment(id="real_pay_1", customer_id="real_c1", subscription_id="real_s1", amount=10, currency="INR", status="failed",
                   failure_reason="insufficient_funds", created_at=__import__("datetime").datetime(2026, 1, 1)))
    db.commit(); db.close()
    c = client(factory, FakeDecisionMaker())
    assert c.post("/gateway/payments/real_pay_1/settle", json={"outcome": "success"}).status_code == 403     # not synthetic
    pid = event(c, "insufficient_funds")["event"]["payment_id"]
    assert c.post(f"/gateway/payments/{pid}/settle", json={"outcome": "success"}).status_code == 409         # nothing scheduled
    assert c.post("/gateway/payments/gw_pay_missing/settle", json={"outcome": "success"}).status_code == 404
    db = factory()
    assert db.get(Payment, "real_pay_1").status == "failed"
    db.close()


def test_events_reuse_synthetic_customers_and_create_one_when_none_exist(factory):
    c = client(factory, FakeDecisionMaker())
    first = event(c, "success")["event"]
    assert first["customer_id"].startswith("gw_cust_")                      # none existed: a synthetic one was created
    second = event(c, "insufficient_funds")["event"]
    assert second["customer_id"] == first["customer_id"]                    # and is reused afterwards
    db = factory()
    assert db.query(Customer).count() == 1 and db.query(Subscription).count() == 1
    db.close()


def test_the_batch_runs_each_event_through_the_same_recovery_flow(factory):
    maker = AlwaysWait()
    items = client(factory, maker).post("/gateway/batch", json={"count": 4}).json()
    assert len(items) == 4 and {i["result"] for i in items} <= {"completed", "rejected"}
    assert all(i["payment_id"].startswith("gw_pay_") for i in items)
    ok = [i for i in items if i["result"] == "completed"]
    assert all(i["action_taken"] == "no_action" for i in ok) and maker.calls == len(ok)
    assert counts(factory)[0] == len(ok)


# ============================================================ reads: explorer, audit, metrics definitions, CORS

def test_explorer_filters_search_and_detail(factory):
    c = client(factory, FakeDecisionMaker(retry(24)))
    ok = event(c, "success")["event"]["payment_id"]
    bad = event(c, "insufficient_funds")["event"]["payment_id"]
    simulate(c, bad)
    assert {p["id"] for p in c.get("/payments").json()["items"]} == {ok, bad}
    assert [p["id"] for p in c.get("/payments", params={"status": "success"}).json()["items"]] == [ok]
    assert [p["id"] for p in c.get("/payments", params={"recovery_status": "retry_scheduled"}).json()["items"]] == [bad]
    assert c.get("/payments", params={"failure_reason": "insufficient_funds"}).json()["total"] == 1
    assert c.get("/payments", params={"search": bad[-6:]}).json()["total"] == 1
    assert c.get("/payments", params={"recovery_status": "nonsense"}).status_code == 422
    detail = c.get(f"/payments/{bad}").json()
    assert detail["subscription_plan"] and len(detail["decisions"]) == 1 and len(detail["recovery_attempts"]) == 1
    assert c.get("/payments/nope").status_code == 404


def test_metrics_come_from_persisted_data_and_carry_their_definitions(factory):
    c = client(factory, FakeDecisionMaker())
    empty = c.get("/dashboard/metrics").json()
    assert (empty["failed_payments"], empty["revenue_at_risk"], empty["revenue_recovered"], empty["recovery_success_rate"]) == (0, 0.0, 0.0, None)
    assert {"revenue_recovered", "recovery_success_rate", "revenue_at_risk"} <= set(empty["definitions"])
    assert len(empty["by_day"]) == 14 and empty["recent_activity"] == []


def test_cors_allows_only_the_configured_origins(factory):
    c = client(factory, FakeDecisionMaker())
    allowed = c.get("/gateway/status", headers={"Origin": "http://localhost:5173"})
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in c.get("/gateway/status", headers={"Origin": "https://evil.example"}).headers
    pre = c.options("/recovery/simulate", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                                     "Access-Control-Request-Headers": "content-type,idempotency-key"})
    assert pre.status_code == 200 and "idempotency-key" in pre.headers["access-control-allow-headers"].lower()


# ============================================================ open dashboard, budgets, batch limits, timeout fallback

def test_the_dashboard_needs_no_access_code_in_demo_mode(factory):
    maker = FakeDecisionMaker(retry(24))
    c = client(factory, maker)                                      # no credentials of any kind are sent
    assert c.get("/gateway/status").json()["demo_mode"] is True
    for path in ("/dashboard/metrics", "/payments", "/audit"):
        assert c.get(path).status_code == 200, path
    pid = event(c, "insufficient_funds")["event"]["payment_id"]
    assert simulate(c, pid).status_code == 200 and maker.calls == 1


def test_the_model_budget_is_enforced_in_demo_mode(factory, monkeypatch):
    monkeypatch.setattr(settings, "demo_max_llm_runs_per_hour", 2)
    maker = FakeDecisionMaker(retry(24), retry(24), retry(24))
    c = client(factory, maker)
    pids = [event(c, "insufficient_funds")["event"]["payment_id"] for _ in range(3)]
    codes = [simulate(c, pid).status_code for pid in pids]
    assert codes == [200, 200, 429] and maker.calls == 2


def test_the_event_budget_is_enforced_in_demo_mode(factory, monkeypatch):
    monkeypatch.setattr(settings, "demo_max_events_per_hour", 2)
    c = client(factory, FakeDecisionMaker())
    assert [c.post("/gateway/events", json={"kind": "success"}).status_code for _ in range(3)] == [200, 200, 429]
    db = factory()
    assert db.query(Payment).count() == 2                                   # the refused request created nothing
    db.close()


def test_batch_is_disabled_by_default_and_capped_when_enabled(factory, monkeypatch):
    monkeypatch.setattr(settings, "demo_batch_enabled", False)
    maker = AlwaysWait()
    c = client(factory, maker)
    assert c.get("/gateway/status").json()["batch_enabled"] is False
    assert c.post("/gateway/batch", json={"count": 1}).status_code == 403
    monkeypatch.setattr(settings, "demo_batch_enabled", True)
    monkeypatch.setattr(settings, "demo_batch_max", 2)
    assert c.post("/gateway/batch", json={"count": 3}).status_code == 422
    assert maker.calls == 0 and counts(factory) == (0, 0)
    assert len(c.post("/gateway/batch", json={"count": 2}).json()) == 2
    assert settings.demo_batch_enabled and Settings(_env_file=None).demo_batch_enabled is False and Settings(_env_file=None).demo_batch_max == 3   # defaults


def test_gateway_timeout_gets_a_deterministic_escalation_when_every_ai_proposal_is_blocked(factory):
    maker = FakeDecisionMaker(retry(24), retry(48))                          # the model insists on retrying
    c = client(factory, maker)
    pid = event(c, "gateway_timeout")["event"]["payment_id"]
    result = simulate(c, pid).json()
    assert maker.calls == 2                                                  # the fallback is NOT a third model call
    assert (result["run_status"], result["action_taken"]) == ("completed", "escalate_to_human")
    assert [t["status"] for t in result["tool_calls"]] == ["rejected", "rejected", "executed_simulated"]
    assert result["tool_calls"][2]["policy"]["allowed"] is True               # judged by the same policy as any proposal
    assert result["decision"]["summary"].startswith("Safety fallback")
    db = factory()
    rows = db.query(RecoveryAttempt).filter(RecoveryAttempt.payment_id == pid).all()
    assert [(r.strategy, r.status) for r in rows] == [("escalate_to_human", "executed_simulated")]
    assert db.query(RecoveryAttempt).filter(RecoveryAttempt.strategy == "schedule_retry").count() == 0
    db.close()
    assert c.get(f"/payments/{pid}").json()["payment"]["recovery_status"] == "escalated"
    replay = simulate(c, pid)
    assert replay.headers["Idempotent-Replayed"] == "true" and maker.calls == 2


def test_the_fallback_applies_only_to_gateway_timeouts(factory):
    c = client(factory, FakeDecisionMaker(retry(24), retry(48)))
    pid = event(c, "expired_card")["event"]["payment_id"]
    result = simulate(c, pid).json()
    assert (result["run_status"], result["action_taken"]) == ("blocked", None) and len(result["tool_calls"]) == 2


def test_settling_a_retry_moves_attempted_at_from_the_planned_time_to_the_execution_time(factory):
    c = client(factory, FakeDecisionMaker(retry(48)))
    pid = event(c, "insufficient_funds")["event"]["payment_id"]
    simulate(c, pid)
    db = factory()
    planned = db.query(RecoveryAttempt).filter(RecoveryAttempt.payment_id == pid).one().attempted_at
    db.close()
    c.post(f"/gateway/payments/{pid}/settle", json={"outcome": "success"})
    db = factory()
    executed = db.query(RecoveryAttempt).filter(RecoveryAttempt.payment_id == pid).one().attempted_at
    db.close()
    assert executed < planned                                               # planned for +48h; executed "now" in the demo
