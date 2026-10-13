"""The recovery service end to end on an in-memory SQLite copy of the original tables (fabricated data).
Concurrency is SIMULATED here by choosing the order of events; real two-connection concurrency is not tested."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db.models import AgentDecision, Payment, RecoveryAttempt
from app.recovery.decision_maker import InvalidModelOutputError, OllamaUnavailableError
from app.recovery.service import RecoveryError, decision_id_for, run_recovery
from app.repositories.recovery_repository import RecoveryRepository
from tests.service_fixtures import (FIXED_NOW, FakeDecisionMaker, clock, counts, escalate, invalid, make_factory,
                                    payments_rows, reminder, retry, seed, update, wait)


@pytest.fixture
def factory():
    return make_factory()


@pytest.fixture
def db(factory):
    session = factory()
    seed(session)
    yield session
    session.close()


def go(db, maker, key="k1", payment="pay_1", **kw):
    return run_recovery(db, payment, key, maker, clock=clock, **kw)


def the_decision(db):
    db.expire_all()
    return db.query(AgentDecision).one()


# ------------------------------------------------------------------ approved: saved and auditable

def test_an_approved_retry_is_saved_as_a_decision_and_a_simulated_action(db):
    before = payments_rows(db)
    result = go(db, FakeDecisionMaker(retry(24, "Funds were short.")))

    assert (result.run_status, result.action_taken, result.stopped_reason) == ("completed", "schedule_retry", "completed")
    assert result.simulated is True and result.idempotent_replay is False and result.final_message == "Funds were short."
    assert counts(db) == {"agent_decisions": 1, "recovery_attempts": 1, "payments": 1}

    decision = the_decision(db)
    assert decision.id == decision_id_for("k1") == result.run_id and decision.id.startswith("dec_") and len(decision.id) <= 50
    assert (decision.proposed_action, decision.policy_status, decision.decision_summary) == ("schedule_retry", "approved", "Funds were short.")
    assert decision.policy_reason.startswith("all_rules_passed:") and decision.created_at == FIXED_NOW
    audit = decision.action_parameters
    assert audit["parameters"] == {"retry_after_hours": 24} and audit["failure_reason"] == "insufficient_funds"
    assert [a["status"] for a in audit["attempts"]] == ["executed_simulated"]

    attempt = db.query(RecoveryAttempt).one()
    assert attempt.id == "att_" + decision.id[4:] and (attempt.strategy, attempt.status) == ("schedule_retry", "scheduled")
    assert attempt.attempted_at == FIXED_NOW + timedelta(hours=24) and attempt.outcome.startswith("SIMULATED")
    assert (attempt.customer_id, attempt.payment_id) == ("c1", "pay_1")
    assert result.simulated_action.id == attempt.id and result.simulated_action.scheduled_for == attempt.attempted_at
    assert payments_rows(db) == before                                        # the payment itself is never written


@pytest.mark.parametrize("decision,status", [(update(), "executed_simulated"), (reminder("urgent"), "executed_simulated"),
                                             (escalate(), "executed_simulated"), (wait(), "noop")])
def test_other_actions_are_saved_with_the_right_status(db, decision, status):
    result = go(db, FakeDecisionMaker(decision))
    attempt = db.query(RecoveryAttempt).one()
    assert (attempt.strategy, attempt.status, result.run_status) == (decision.action, status, "completed")
    assert result.simulated_action.scheduled_for is None


def test_the_original_failure_reason_is_kept_exactly_as_it_was(factory):
    db = factory(); seed(db, failure_reason="  Insufficient Funds ")
    go(db, FakeDecisionMaker(retry()))
    assert the_decision(db).action_parameters["failure_reason"] == "  Insufficient Funds "


def test_the_next_request_sees_earlier_simulated_actions_in_its_context(db):
    go(db, FakeDecisionMaker(retry(24)), key="a")
    maker = FakeDecisionMaker(reminder())
    go(db, maker, key="b")
    history = maker.contexts[0].recovery_history
    assert [(h.strategy, h.status) for h in history] == [("schedule_retry", "scheduled")]


# ------------------------------------------------------------------ blocked: never executed

def test_a_blocked_proposal_is_recorded_as_blocked_and_never_executed(factory):
    db = factory(); seed(db, failure_reason="suspected_fraud")
    maker = FakeDecisionMaker(retry(24), update())
    result = go(db, maker)

    assert (result.run_status, result.action_taken, result.stopped_reason, result.final_message) == ("blocked", None, "rejection_limit", None)
    assert result.simulated_action is None and "No action was executed" in result.message and maker.calls == 2
    assert [t.status for t in result.tool_calls] == ["rejected", "rejected"]
    first = result.tool_calls[0].policy
    assert first["allowed"] is False and first["category"] == "suspected_fraud" and first["alternatives"] == ["escalate_to_human", "no_action"]
    assert counts(db)["recovery_attempts"] == 0                               # a blocked proposal is NOT a simulated action
    decision = the_decision(db)
    assert decision.policy_status == "blocked" and decision.proposed_action == "request_payment_update"
    assert decision.policy_reason.startswith("failure_category:")


def test_a_blocked_try_followed_by_an_allowed_one_records_only_the_allowed_action(factory):
    db = factory(); seed(db, failure_reason="gateway_timeout")
    result = go(db, FakeDecisionMaker(retry(24), escalate("timeout is ambiguous")))
    assert (result.run_status, result.action_taken) == ("completed", "escalate_to_human")
    assert [t.status for t in result.tool_calls] == ["rejected", "executed_simulated"]
    assert [r.strategy for r in db.query(RecoveryAttempt)] == ["escalate_to_human"]
    assert the_decision(db).action_parameters["attempts"][0]["policy"]["allowed"] is False   # the blocked try stays in the audit


def test_a_second_retry_is_blocked_because_one_is_already_scheduled(db):
    go(db, FakeDecisionMaker(retry(24)), key="a")
    result = go(db, FakeDecisionMaker(retry(48), retry(72)), key="b")
    assert result.run_status == "blocked" and result.decision.policy_reason.startswith("retry_not_already_scheduled")
    assert db.query(RecoveryAttempt).filter(RecoveryAttempt.status == "scheduled").count() == 1


def test_the_state_changing_during_the_llm_call_blocks_the_action_at_commit(factory):
    db = factory(); seed(db)

    def customer_pays_meanwhile(call_index):
        other = factory()
        other.query(Payment).filter(Payment.id == "pay_1").update({"status": "success"})
        other.commit(); other.close()

    result = go(db, FakeDecisionMaker(retry(24), wait(), on_call=customer_pays_meanwhile))
    assert result.run_status == "blocked" and counts(db)["recovery_attempts"] == 0
    assert "at commit" in result.tool_calls[-1].error and "payment_must_be_failed" in result.tool_calls[-1].error
    assert the_decision(db).policy_reason.startswith("payment_must_be_failed")


def test_a_retry_saved_by_another_request_meanwhile_blocks_this_one_at_commit(factory):
    """Different keys, same payment. SIMULATED interleaving: request B saves its retry while request A's LLM call runs."""
    db = factory(); seed(db)

    def request_b_saves_first(call_index):
        other = factory()
        go(other, FakeDecisionMaker(retry(48)), key="B")
        other.close()

    result = go(db, FakeDecisionMaker(retry(24), wait(), on_call=request_b_saves_first), key="A")
    assert result.run_status == "blocked" and "at commit" in result.tool_calls[-1].error
    assert result.decision.policy_reason.startswith("retry_not_already_scheduled")
    assert db.query(RecoveryAttempt).filter(RecoveryAttempt.strategy == "schedule_retry").count() == 1     # only B's


# ------------------------------------------------------------------ idempotency

def test_the_same_key_returns_the_saved_result_without_calling_the_llm_again(db):
    maker = FakeDecisionMaker(retry(24))
    first = go(db, maker)
    before = counts(db)
    second = go(db, maker)                                                    # a second LLM call would raise
    assert maker.calls == 1 and counts(db) == before
    assert second.idempotent_replay is True and first.idempotent_replay is False
    assert second.model_dump(exclude={"idempotent_replay"}) == first.model_dump(exclude={"idempotent_replay"})


def test_a_blocked_outcome_is_replayed_too(factory):
    db = factory(); seed(db, failure_reason="suspected_fraud")
    maker = FakeDecisionMaker(retry(24), retry(48))
    first, second = go(db, maker), go(db, maker)
    assert first.run_status == second.run_status == "blocked" and second.idempotent_replay and maker.calls == 2


def test_a_key_used_for_another_payment_is_rejected(factory):
    db = factory(); seed(db, extra_payment="pay_2")
    go(db, FakeDecisionMaker(retry()))
    maker, before = FakeDecisionMaker(retry()), None
    with pytest.raises(RecoveryError) as err:
        go(db, maker, payment="pay_2")
    assert err.value.status_code == 422 and maker.calls == 0 and counts(db)["agent_decisions"] == 1


def test_losing_the_race_for_a_key_replays_the_winners_result(db, monkeypatch):
    """SIMULATED: the first lookup misses (the other request had not committed yet), then the primary key rejects us."""
    winner = go(db, FakeDecisionMaker(retry(24)), key="same")
    real, state = RecoveryRepository.get_decision, {"first": True}

    def stale_first(self, decision_id):
        if state["first"]:
            state["first"] = False
            return None
        return real(self, decision_id)

    monkeypatch.setattr(RecoveryRepository, "get_decision", stale_first)
    loser = go(db, FakeDecisionMaker(wait()), key="same")
    assert loser.idempotent_replay is True and loser.run_id == winner.run_id and counts(db)["agent_decisions"] == 1


def test_without_a_key_one_is_generated_and_can_be_used_to_replay(db):
    first = run_recovery(db, "pay_1", None, FakeDecisionMaker(wait()), clock=clock)
    assert first.idempotency_key.startswith("auto-")
    replay = run_recovery(db, "pay_1", first.idempotency_key, FakeDecisionMaker(), clock=clock)
    assert replay.idempotent_replay is True and replay.run_id == first.run_id


def test_the_primary_key_really_rejects_a_second_decision_for_one_key(db):
    go(db, FakeDecisionMaker(wait()), key="k")
    db.add(AgentDecision(id=decision_id_for("k"), customer_id="c1", payment_id="pay_1", proposed_action="no_action",
                         created_at=FIXED_NOW))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ------------------------------------------------------------------ failures: all or nothing

def test_a_failure_while_saving_leaves_nothing_behind(db, monkeypatch):
    def boom(self, **fields):
        raise RuntimeError("disk exploded")                                    # after the decision row was written

    monkeypatch.setattr(RecoveryRepository, "add_attempt", boom)
    before = payments_rows(db)
    with pytest.raises(RuntimeError):
        go(db, FakeDecisionMaker(retry(24)))
    assert counts(db) == {"agent_decisions": 0, "recovery_attempts": 0, "payments": 1} and payments_rows(db) == before


def test_an_unexpected_database_conflict_is_a_409_and_saves_nothing(db, monkeypatch):
    def conflict(self, **fields):
        raise IntegrityError("insert", {}, Exception("conflict"))

    monkeypatch.setattr(RecoveryRepository, "add_attempt", conflict)
    with pytest.raises(RecoveryError) as err:
        go(db, FakeDecisionMaker(retry(24)))
    assert err.value.status_code == 409 and counts(db)["agent_decisions"] == 0


@pytest.mark.parametrize("method", ["get_decision", "lock_payment"])
def test_database_down_is_a_503_and_saves_nothing(db, monkeypatch, method):
    def down(self, *a, **k):
        raise OperationalError("select", {}, Exception("connection lost"))

    monkeypatch.setattr(RecoveryRepository, method, down)
    with pytest.raises(RecoveryError) as err:
        go(db, FakeDecisionMaker(retry(24), wait()))
    assert err.value.status_code == 503 and counts(db)["agent_decisions"] == 0


def test_an_unreachable_ollama_saves_nothing_and_the_same_key_works_later(db):
    with pytest.raises(RecoveryError) as err:
        go(db, FakeDecisionMaker(OllamaUnavailableError("refused")))
    assert err.value.status_code == 503 and counts(db)["agent_decisions"] == 0
    assert go(db, FakeDecisionMaker(wait())).run_status == "completed"          # the key was not used up


def test_unusable_model_output_twice_is_a_502_and_saves_nothing(db):
    with pytest.raises(RecoveryError) as err:
        go(db, FakeDecisionMaker(invalid("a"), invalid("b")))
    assert err.value.status_code == 502 and counts(db)["agent_decisions"] == 0


def test_a_payment_that_is_not_recoverable_saves_nothing(factory):
    db = factory(); seed(db, payment_status="success")
    maker = FakeDecisionMaker(retry())
    with pytest.raises(RecoveryError) as not_failed:
        go(db, maker)
    with pytest.raises(RecoveryError) as missing:
        go(db, maker, payment="nope")
    assert (not_failed.value.status_code, missing.value.status_code) == (409, 404)
    assert maker.calls == 0 and counts(db)["agent_decisions"] == 0
