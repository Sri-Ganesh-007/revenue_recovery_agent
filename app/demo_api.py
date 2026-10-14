"""Demo console API: dashboard reads, payment explorer, audit trail, and the mock gateway.

* Reads are open (they return what is in the connected database).
* The mock-gateway WRITE endpoints (/gateway/events, /gateway/batch, /gateway/payments/{id}/settle) work only when
  DEMO_MODE=true, and only ever create/change synthetic payments. Recovery itself is the existing
  POST /recovery/simulate flow; nothing here is a second workflow.
"""
import random

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import dashboard, gateway
from app.config import settings
from app.demo_access import access_required, limit_events, require_access, spend
from app.context.context_builder import RecoveryContextBuilder
from app.deps import get_db, get_decision_maker
from app.recovery.policy import evaluate, snapshot_from_context
from app.recovery.service import RecoveryError, run_recovery, utcnow
from app.repositories.recovery_repository import RecoveryRepository
from app.schemas.demo import (AuditRecord, BatchItem, BatchRequest, DashboardMetrics, GatewayEvent, GatewayEventRequest,
                              GatewayEventResponse, GatewayStatus, PaymentDetail, PaymentPage, PolicyPreview, SettleRequest)

router = APIRouter()
ACCESS = [Depends(require_access)]          # backend-enforced access code (see app/demo_access.py)


def require_demo_mode():
    if not settings.demo_mode:
        raise HTTPException(status_code=403, detail="The mock gateway is disabled. Set DEMO_MODE=true on the backend to enable it.")


# ------------------------------------------------------------------ reads

@router.get("/gateway/status", response_model=GatewayStatus)
def gateway_status():
    return GatewayStatus(demo_mode=settings.demo_mode, llm_provider=settings.llm_provider, currency=settings.demo_currency,
                         access_required=access_required(), batch_enabled=settings.demo_batch_enabled,
                         batch_max=settings.demo_batch_max)


@router.get("/dashboard/metrics", response_model=DashboardMetrics, dependencies=ACCESS)
def dashboard_metrics(db: Session = Depends(get_db)):
    return dashboard.metrics(db, utcnow(), settings.demo_currency)


@router.get("/payments", response_model=PaymentPage, dependencies=ACCESS)
def payments(status: str | None = None, recovery_status: str | None = None, failure_reason: str | None = None,
             search: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
             db: Session = Depends(get_db)):
    if recovery_status and recovery_status not in dashboard.RECOVERY_STATUSES:
        raise HTTPException(status_code=422, detail=f"recovery_status must be one of {dashboard.RECOVERY_STATUSES}")
    total, items = dashboard.list_payments(db, status=status, recovery_status=recovery_status,
                                           failure_reason=failure_reason, search=search, limit=limit, offset=offset)
    return PaymentPage(total=total, items=items)


@router.get("/payments/{payment_id}", response_model=PaymentDetail, dependencies=ACCESS)
def payment(payment_id: str, db: Session = Depends(get_db)):
    detail = dashboard.payment_detail(db, payment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} not found")
    return detail


@router.get("/audit", response_model=list[AuditRecord], dependencies=ACCESS)
def audit(payment_id: str | None = None, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return dashboard.audit_records(db, payment_id, limit)


# ------------------------------------------------------------------ mock gateway (writes)

def _preview(db: Session, payment_id: str):
    """What the agent will be shown, and a dry run of the policy for a retry. Nothing is executed or saved."""
    repo = RecoveryRepository(db)
    try:
        context = RecoveryContextBuilder(repo).build(payment_id)
    except ValueError as exc:
        return False, str(exc), None, None
    data = context.model_dump(mode="json")
    data["customer"] = {"id": context.customer.id}                       # the model is never shown name or email
    verdict = evaluate(snapshot_from_context(context, utcnow()), "schedule_retry", {"retry_after_hours": 24})
    return True, None, data, PolicyPreview(action=verdict.action, allowed=verdict.allowed, rule=verdict.rule,
                                           reason=verdict.reason, category=verdict.category)


@router.post("/gateway/events", response_model=GatewayEventResponse,
             dependencies=[Depends(require_demo_mode), Depends(require_access), Depends(limit_events)])
def create_gateway_event(request: GatewayEventRequest, db: Session = Depends(get_db)):
    """The mock gateway announces a payment outcome (a new synthetic payment). It does NOT run the agent: the
    client then calls the existing POST /recovery/simulate for the new payment_id."""
    record = gateway.create_event(db, request.kind, utcnow())
    eligible, reason, context, preview = _preview(db, record.payment_id)
    return GatewayEventResponse(
        event=GatewayEvent(kind=record.kind, payment_id=record.payment_id, customer_id=record.customer_id,
                           customer_name=record.customer_name, plan=record.plan, amount=record.amount,
                           currency=record.currency, payment_status=record.payment_status,
                           failure_reason=record.failure_reason, received_at=record.received_at,
                           prior_retries=record.prior_retries),
        eligible_for_recovery=eligible, ineligible_reason=reason, context_preview=context, retry_policy_preview=preview)


@router.post("/gateway/payments/{payment_id}/settle", response_model=PaymentDetail,
             dependencies=[Depends(require_demo_mode), Depends(require_access)])
def settle(payment_id: str, request: SettleRequest, db: Session = Depends(get_db)):
    """The mock processor confirms or declines the retry the agent scheduled. Revenue is recovered only on 'success'."""
    try:
        gateway.settle_retry(db, payment_id, request.outcome, utcnow())
    except gateway.GatewayError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return dashboard.payment_detail(db, payment_id)


BATCH_KINDS = ["insufficient_funds", "gateway_timeout", "insufficient_funds", "expired_card", "success",
               "processor_decline", "insufficient_funds", "authentication_required", "gateway_timeout", "suspected_fraud"]


@router.post("/gateway/batch", response_model=list[BatchItem], dependencies=[Depends(require_demo_mode), Depends(require_access)])
def batch(request: BatchRequest, db: Session = Depends(get_db), decision_maker=Depends(get_decision_maker)):
    """Generate several events and run each through the same recovery flow, one after another (each is a real LLM call).

    Off unless DEMO_BATCH_ENABLED=true, and limited to DEMO_BATCH_MAX events, so one request stays short."""
    if not settings.demo_batch_enabled:
        raise HTTPException(status_code=403, detail="Batch runs are disabled. Set DEMO_BATCH_ENABLED=true to allow small batches.")
    if request.count > settings.demo_batch_max:
        raise HTTPException(status_code=422, detail=f"A batch may contain at most {settings.demo_batch_max} events.")
    spend("gateway events", settings.demo_max_events_per_hour, request.count)
    spend("model runs", settings.demo_max_llm_runs_per_hour, request.count)
    rng, items = random.Random(), []
    for kind in rng.sample(BATCH_KINDS, k=request.count):          # count <= 10 == len(BATCH_KINDS) (schema limit)
        record = gateway.create_event(db, kind, utcnow(), rng)
        try:
            result = run_recovery(db, record.payment_id, f"gw-{record.payment_id}", decision_maker)
            items.append(BatchItem(payment_id=record.payment_id, kind=kind, result=result.run_status,
                                   action_taken=result.action_taken, detail=result.decision.policy_reason if result.decision else None))
        except RecoveryError as exc:
            items.append(BatchItem(payment_id=record.payment_id, kind=kind, result="rejected" if exc.status_code == 409 else "error",
                                   action_taken=None, detail=exc.detail))
    return items
