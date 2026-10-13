"""The recovery service: one request, as a readable sequence. Everything it does is simulated.

  1. Replay      a request with an Idempotency-Key we have already answered returns the saved result.
  2. Context     payment, customer, subscription and this payment's recovery history, via the
                 existing RecoveryRepository and RecoveryContextBuilder (404 / 409 if not recoverable).
  3. Decide      the LangGraph workflow: the LLM proposes, the policy approves or blocks; at most 2 tries.
  4. Save        ONE transaction: lock the payment row, rebuild the context under the lock, re-run the
                 policy, then insert the decision (and, only if approved, the simulated action) and commit.

Duplicate protection, and what it does NOT cover (no schema changes were made to get it):
  * Same Idempotency-Key: the decision's primary key is derived from the key (dec_<sha256>), so the
    DATABASE rejects a second decision for it. The loser rolls back and returns the winner's result.
    Two truly simultaneous requests with one key may both call the LLM; only one result is saved.
  * Different keys / no key for the same payment: protected by the payment-row lock plus the policy
    (open retry, retry cap, 24h spacing, cooldown) re-run on freshly read data. This is APPLICATION
    logic, not a database constraint: any writer that skips the lock could still create a duplicate.
  * Nothing is saved when the LLM or database fails, so retrying the same key simply runs again.
"""
import hashlib
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.context.context_builder import RecoveryContextBuilder
from app.recovery.actions import simulate_action
from app.recovery.decision_maker import InvalidModelOutputError, OllamaUnavailableError
from app.recovery.policy import DEFAULT_CONFIG, evaluate, snapshot_from_context
from app.recovery.workflow import run_workflow
from app.repositories.recovery_repository import RecoveryRepository
from app.schemas.recovery import (DecisionSummary, RecoveryRunResponse, SimulatedActionSummary, ToolCallRecord)


class RecoveryError(Exception):
    """An expected failure with the HTTP status the API should return."""

    def __init__(self, status_code: int, detail: str):
        self.status_code, self.detail = status_code, detail
        super().__init__(f"{status_code}: {detail}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)      # the database stores naive UTC


def decision_id_for(key: str) -> str:
    return "dec_" + hashlib.sha256(key.encode()).hexdigest()[:40]       # 44 characters; column limit is 50


def attempt_id_for(decision_id: str) -> str:
    return "att_" + decision_id[4:]


# ---------------------------------------------------------------------------------- the request

def run_recovery(db: Session, payment_id: str, idempotency_key: str | None, decision_maker, *,
                 config=DEFAULT_CONFIG, clock=utcnow) -> RecoveryRunResponse:
    key = idempotency_key or f"auto-{uuid.uuid4().hex}"
    decision_id = decision_id_for(key)
    repo = RecoveryRepository(db)

    try:
        existing = repo.get_decision(decision_id)                               # 1. replay?
        if existing is not None:
            return _replay(repo, existing, payment_id, key)

        payment = repo.get_payment(payment_id)                                  # 2. context
        if payment is None:
            raise RecoveryError(404, f"Payment {payment_id} not found")
        try:
            context = RecoveryContextBuilder(repo).build(payment_id)
        except ValueError as exc:
            raise RecoveryError(409, str(exc)) from exc
        customer_id = payment.customer_id
        db.rollback()                                   # end the read-only transaction before the slow LLM call
    except OperationalError as exc:
        db.rollback()
        raise RecoveryError(503, "Database unavailable") from exc

    try:                                                                        # 3. the LLM decides
        attempts = run_workflow(decision_maker, context, clock(), config)
    except OllamaUnavailableError as exc:
        raise RecoveryError(503, str(exc)) from exc
    except InvalidModelOutputError as exc:
        raise RecoveryError(502, str(exc)) from exc

    return _save(db, repo, decision_id, key, payment_id, customer_id, context, attempts, config, clock)


# ---------------------------------------------------------------------------------- saving

def _save(db, repo, decision_id, key, payment_id, customer_id, context, attempts, config, clock):
    now = clock()
    try:
        payment = repo.lock_payment(payment_id)                                 # only one save per payment at a time
        if payment is None:
            raise RecoveryError(409, f"Payment {payment_id} no longer exists")
        snapshot = _fresh_snapshot(repo, payment, context, now)                 # data as it is NOW, under the lock

        selected = next(a for a in reversed(attempts) if a.decision is not None)
        outcome = None                    # becomes the simulated action only if the policy still approves
        blocked_at_commit = False
        if selected.approved:
            recheck = evaluate(snapshot, selected.action, selected.parameters, config)
            if recheck.allowed:
                outcome = simulate_action(selected.action, selected.parameters, payment_id, now)
            else:                         # the situation changed since the proposal: do NOT execute it
                selected.verdict, blocked_at_commit = recheck, True

        records = _records(attempts, selected, outcome, blocked_at_commit)
        verdict = selected.verdict
        repo.add_decision(
            id=decision_id, customer_id=customer_id, payment_id=payment_id,
            proposed_action=selected.action,
            action_parameters={"parameters": selected.parameters,
                               "failure_reason": context.failed_payment.failure_reason,   # original, for the audit trail
                               "attempts": [r.model_dump(mode="json") for r in records]},
            decision_summary=selected.decision.explanation,
            policy_status="approved" if outcome else "blocked",
            policy_reason=f"{verdict.rule}: {verdict.reason}",
            created_at=now)
        db.flush()                                                              # the decision exists before its action
        if outcome:
            repo.add_attempt(id=attempt_id_for(decision_id), customer_id=customer_id, payment_id=payment_id,
                             strategy=outcome.action, status=outcome.status, outcome=outcome.outcome[:100],
                             attempted_at=outcome.scheduled_for or now)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = repo.get_decision(decision_id)                               # lost the race for this key?
        if existing is None:
            raise RecoveryError(409, "A conflicting record was rejected by the database") from exc
        return _replay(repo, existing, payment_id, key)
    except OperationalError as exc:
        db.rollback()
        raise RecoveryError(503, "Database unavailable") from exc
    except Exception:
        db.rollback()                                                           # all or nothing
        raise

    return _response(repo.get_decision(decision_id), repo.get_attempt(attempt_id_for(decision_id)), key, replayed=False)


def _fresh_snapshot(repo, payment, context, now):
    try:
        return snapshot_from_context(RecoveryContextBuilder(repo).build(payment.id), now)
    except ValueError:                                  # no longer failed (or data missing)
        if payment.status == "failed":
            raise RecoveryError(409, "Customer or subscription data is no longer available")
        return replace(snapshot_from_context(context, now), payment_status=payment.status)


def _records(attempts, selected, outcome, blocked_at_commit) -> list[ToolCallRecord]:
    """One entry per proposal, in order: what the LLM asked for and what happened to it."""
    records = []
    for attempt in attempts:
        if attempt.decision is None:                                            # the LLM output was unusable
            records.append(ToolCallRecord(name=None, arguments={}, status="rejected", error=attempt.error))
            continue
        policy = attempt.verdict.model_dump(mode="json")
        if attempt is selected and outcome is not None:
            records.append(ToolCallRecord(name=attempt.action, arguments=attempt.parameters,
                                          status="executed_simulated", result=outcome.result, policy=policy))
        else:
            when = " at commit" if (attempt is selected and blocked_at_commit) else ""
            records.append(ToolCallRecord(name=attempt.action, arguments=attempt.parameters, status="rejected",
                                          error=f"Blocked by policy{when} ({attempt.verdict.rule}): {attempt.verdict.reason}",
                                          policy=policy))
    return records


# ---------------------------------------------------------------------------------- answering

MESSAGE_APPROVED = ("{action} was approved by policy and recorded as a SIMULATED action. "
                    "Nothing real was charged, sent or changed.")
MESSAGE_BLOCKED = ("No action was executed: every proposed action was blocked by policy. "
                   "Human review is recommended.")


def _replay(repo, decision, payment_id, key) -> RecoveryRunResponse:
    if decision.payment_id != payment_id:
        raise RecoveryError(422, "Idempotency-Key was already used with a different request "
                                 "(different payment_id). Use a new key.")
    return _response(decision, repo.get_attempt(attempt_id_for(decision.id)), key, replayed=True)


def _response(decision, attempt, key, replayed) -> RecoveryRunResponse:
    audit = decision.action_parameters or {}
    approved = decision.policy_status == "approved"
    action = attempt.strategy if (approved and attempt is not None) else None
    return RecoveryRunResponse(
        payment_id=decision.payment_id,
        action_taken=action,
        stopped_reason="completed" if approved else "rejection_limit",
        tool_calls=[ToolCallRecord.model_validate(r) for r in audit.get("attempts", [])],
        final_message=decision.decision_summary if approved else None,
        run_id=decision.id,
        idempotency_key=key,
        run_status="completed" if approved else "blocked",
        idempotent_replay=replayed,
        decision=DecisionSummary(id=decision.id, proposed_action=decision.proposed_action,
                                 parameters=audit.get("parameters"), policy_status=decision.policy_status,
                                 policy_reason=decision.policy_reason or "", summary=decision.decision_summary or ""),
        simulated_action=None if attempt is None else SimulatedActionSummary(
            id=attempt.id, action=attempt.strategy, status=attempt.status,
            scheduled_for=attempt.attempted_at if attempt.status == "scheduled" else None),
        message=MESSAGE_APPROVED.format(action=action) if approved else MESSAGE_BLOCKED,
    )
