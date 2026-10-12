from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
