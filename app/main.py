from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.context.context_builder import RecoveryContextBuilder
from app.db.database import SessionLocal
from app.llm.decision_maker import (
    InvalidModelOutputError,
    OllamaDecisionMaker,
    OllamaUnavailableError,
)
from app.llm.proposal import RecoveryProposal
from app.llm.graph import run_simulation
from app.llm.tool_runner import SimulationResult
from app.repositories.recovery_repository import RecoveryRepository

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


def get_decision_maker():
    return OllamaDecisionMaker()


def build_context(db: Session, payment_id: str):
    repository = RecoveryRepository(db)

    if repository.get_payment(payment_id) is None:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} not found")

    try:
        return RecoveryContextBuilder(repository).build(payment_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/recovery/proposal", response_model=RecoveryProposal)
def create_recovery_proposal(
    request: ProposalRequest,
    db: Session = Depends(get_db),
    decision_maker=Depends(get_decision_maker),
):
    context = build_context(db, request.payment_id)

    try:
        return decision_maker.propose(context)
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except InvalidModelOutputError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/recovery/simulate", response_model=SimulationResult)
def simulate_recovery(
    request: ProposalRequest,
    db: Session = Depends(get_db),
    decision_maker=Depends(get_decision_maker),
):
    """Tool-calling run with simulated tools only. Reads the database; writes nothing."""
    context = build_context(db, request.payment_id)

    try:
        return run_simulation(context, decision_maker)
    except OllamaUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except InvalidModelOutputError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
