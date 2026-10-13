"""Pydantic models for the recovery workflow.

1. RecoveryProposal  - the /recovery/proposal response contract (unchanged).
2. Decision forms    - what the LLM returns (LangChain structured output).
3. Response models   - what /recovery/simulate returns.
"""
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ------------------------------------------------------------------ 1. /recovery/proposal contract

class ProposedAction(str, Enum):
    SCHEDULE_RETRY = "schedule_retry"
    REQUEST_PAYMENT_UPDATE = "request_payment_update"
    SEND_REMINDER = "send_reminder"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    NO_ACTION = "no_action"


REMINDER_TONES = {"friendly", "urgent"}
MAX_RETRY_DELAY_HOURS = 168

# Allowed parameter names per action. Anything else is rejected.
ALLOWED_PARAMETERS: dict[ProposedAction, set[str]] = {
    ProposedAction.SCHEDULE_RETRY: {"retry_after_hours"},
    ProposedAction.REQUEST_PAYMENT_UPDATE: set(),
    ProposedAction.SEND_REMINDER: {"tone"},
    ProposedAction.ESCALATE_TO_HUMAN: {"reason"},
    ProposedAction.NO_ACTION: {"reason"},
}


class RecoveryProposal(BaseModel):
    """An untrusted proposal from the LLM. Not an approved or executed action."""

    model_config = ConfigDict(extra="forbid")

    payment_id: str
    action: ProposedAction
    parameters: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def check_parameters(self):
        allowed = ALLOWED_PARAMETERS[self.action]
        params = self.parameters

        unknown = set(params) - allowed
        if unknown:
            raise ValueError(
                f"unexpected parameters for {self.action.value}: {sorted(unknown)}"
            )

        if self.action == ProposedAction.SCHEDULE_RETRY:
            hours = params.get("retry_after_hours")
            if isinstance(hours, bool) or not isinstance(hours, int):
                raise ValueError("schedule_retry requires integer retry_after_hours")
            if not 1 <= hours <= MAX_RETRY_DELAY_HOURS:
                raise ValueError(
                    f"retry_after_hours must be between 1 and {MAX_RETRY_DELAY_HOURS}"
                )

        elif self.action == ProposedAction.SEND_REMINDER:
            if params.get("tone") not in REMINDER_TONES:
                raise ValueError(f"send_reminder requires tone in {sorted(REMINDER_TONES)}")

        elif self.action in (ProposedAction.ESCALATE_TO_HUMAN, ProposedAction.NO_ACTION):
            reason = params.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"{self.action.value} requires a non-empty reason")

        return self


# ------------------------------------------------------------------ 2. what the LLM returns
# One small form per action, so each action's required details cannot be left out. Pydantic re-checks
# the ranges; the deterministic policy decides whether an action is permitted.

class _Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(min_length=1, description="One or two sentences explaining the choice")
    uncertainty: str | None = Field(default=None, description="Anything unclear or missing that affects the choice")


class RetryDecision(_Decision):
    action: Literal["schedule_retry"]
    retry_after_hours: int = Field(ge=1, le=MAX_RETRY_DELAY_HOURS, description="Hours from now, 1 to 168")


class PaymentUpdateDecision(_Decision):
    action: Literal["request_payment_update"]


class ReminderDecision(_Decision):
    action: Literal["send_reminder"]
    tone: Literal["friendly", "urgent"]


class EscalateDecision(_Decision):
    action: Literal["escalate_to_human"]
    reason: str = Field(min_length=3, description="Short reason a person should look at this")


class NoActionDecision(_Decision):
    action: Literal["no_action"]
    reason: str = Field(min_length=3, description="Short reason for waiting")


RecoveryDecision = Annotated[
    Union[RetryDecision, PaymentUpdateDecision, ReminderDecision, EscalateDecision, NoActionDecision],
    Field(discriminator="action"),
]


class DecisionEnvelope(BaseModel):
    """LangChain structured output needs one top-level object; the decision is its only field."""
    decision: RecoveryDecision


def decision_parameters(decision) -> dict:
    """The action's parameters in the shape the policy, actions and API use."""
    if decision.action == "schedule_retry":
        return {"retry_after_hours": decision.retry_after_hours}
    if decision.action == "send_reminder":
        return {"tone": decision.tone}
    if decision.action in ("escalate_to_human", "no_action"):
        return {"reason": decision.reason[:200]}
    return {}


def to_proposal(decision, payment_id: str) -> RecoveryProposal:
    """The /recovery/proposal response, built from the LLM's decision."""
    return RecoveryProposal(payment_id=payment_id, action=decision.action,
                            parameters=decision_parameters(decision), summary=decision.explanation[:300])


# ------------------------------------------------------------------ 3. /recovery/simulate response

class ToolCallRecord(BaseModel):
    """One action the LLM proposed, and what happened to it. (Name kept for API compatibility.)"""
    name: str | None
    arguments: dict[str, Any]
    status: Literal["executed_simulated", "rejected", "error"]
    result: dict | None = None
    error: str | None = None
    policy: dict | None = None            # the policy's verdict, if the proposal got that far


class DecisionSummary(BaseModel):
    id: str
    proposed_action: str
    parameters: dict | None
    policy_status: Literal["approved", "blocked"]
    policy_reason: str
    summary: str


class SimulatedActionSummary(BaseModel):
    id: str
    action: str
    status: str
    scheduled_for: datetime | None = None


class RecoveryRunResponse(BaseModel):
    """What POST /recovery/simulate returns. Everything in it is simulated."""
    simulated: Literal[True] = True
    payment_id: str
    action_taken: str | None                      # only an approved action; None when everything was blocked
    stopped_reason: Literal["completed", "turn_limit", "rejection_limit"]
    tool_calls: list[ToolCallRecord]
    final_message: str | None                     # the LLM's explanation (narrative only)
    final_message_is_narrative_only: Literal[True] = True
    run_id: str                                   # the decision id
    idempotency_key: str
    run_status: Literal["completed", "blocked"]
    idempotent_replay: bool = False
    decision: DecisionSummary | None
    simulated_action: SimulatedActionSummary | None
    message: str
