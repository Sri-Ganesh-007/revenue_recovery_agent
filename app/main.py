import re
from functools import lru_cache

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import settings
from app.context.context_builder import RecoveryContextBuilder
from app.db.database import SessionLocal
from app.recovery.decision_maker import DecisionMaker, InvalidModelOutputError, OllamaUnavailableError
from app.recovery.service import RecoveryError, run_recovery
from app.repositories.recovery_repository import RecoveryRepository
from app.schemas.recovery import RecoveryProposal, RecoveryRunResponse, to_proposal

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,200}$")

app = FastAPI(title="Revenue recovery agent")


@app.get("/")
def root():
    return {"msg": "app is on air"}


class ProposalRequest(BaseModel):
    payment_id: str


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@lru_cache
def get_decision_maker():
    return DecisionMaker()


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Is the API up, and can it reach PostgreSQL and Ollama? Ollama being down is 'degraded', not an outage."""
    checks = {"database": "ok", "ollama": "ok"}
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "unavailable"
    try:
        httpx.get(f"{settings.ollama_url}/api/tags", timeout=2).raise_for_status()
    except Exception:
        checks["ollama"] = "unavailable"
    healthy = checks["database"] == "ok"
    status = "ok" if healthy and checks["ollama"] == "ok" else ("degraded" if healthy else "unavailable")
    return JSONResponse({"status": status, **checks}, status_code=200 if healthy else 503)


@app.post("/recovery/proposal", response_model=RecoveryProposal)
def create_recovery_proposal(
    request: ProposalRequest,
    db: Session = Depends(get_db),
    decision_maker=Depends(get_decision_maker),
):
    """Preview only: what would the LLM propose for this failed payment? Nothing is saved or executed."""
    repository = RecoveryRepository(db)
    try:
        if repository.get_payment(request.payment_id) is None:
            raise HTTPException(status_code=404, detail=f"Payment {request.payment_id} not found")
        try:
            context = RecoveryContextBuilder(repository).build(request.payment_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    except OperationalError:
        raise HTTPException(status_code=503, detail="Database unavailable")
    db.rollback()                                       # release the read transaction before the slow LLM call

    try:
        return to_proposal(decision_maker.propose(context), request.payment_id)
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except InvalidModelOutputError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/recovery/simulate", response_model=RecoveryRunResponse)
def simulate_recovery(
    request: ProposalRequest,
    response: Response,
    db: Session = Depends(get_db),
    decision_maker=Depends(get_decision_maker),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Run the recovery workflow for a failed payment and save the decision. Everything is SIMULATED.

    The LLM proposes an action; deterministic Python rules approve or block it; only an approved action is
    recorded as a simulated action. Send an Idempotency-Key header so a repeated request returns the same result.
    """
    if idempotency_key is not None and not IDEMPOTENCY_KEY_PATTERN.match(idempotency_key):
        raise HTTPException(status_code=422,
                            detail="Idempotency-Key must be 1-200 characters of letters, digits, _ . : -")
    try:
        result = run_recovery(db, request.payment_id, idempotency_key, decision_maker)
    except RecoveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    response.headers["Idempotent-Replayed"] = "true" if result.idempotent_replay else "false"
    return result
