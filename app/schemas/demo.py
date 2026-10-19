"""Response/request models for the demo console (dashboard, payments, audit and the mock gateway)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.recovery import ToolCallRecord

EventKind = Literal["insufficient_funds", "gateway_timeout", "success", "retry_limit",
                    "expired_card", "authentication_required", "processor_decline", "suspected_fraud"]


# ------------------------------------------------------------------ payments / audit

class PaymentRow(BaseModel):
    id: str
    customer_id: str
    customer_name: str
    amount: float
    currency: str
    status: str                              # the payment's own status: success / failed
    failure_reason: str | None
    created_at: datetime
    recovery_status: str                     # derived from persisted decisions and attempts (see dashboard.py)
    last_action: str | None
    last_policy_status: str | None


class PaymentPage(BaseModel):
    total: int
    items: list[PaymentRow]


class AttemptRow(BaseModel):
    id: str
    strategy: str
    status: str
    outcome: str | None
    attempted_at: datetime


class AuditRecord(BaseModel):
    decision_id: str
    payment_id: str
    customer_id: str
    customer_name: str
    proposed_action: str
    parameters: dict | None
    policy_status: str | None
    policy_reason: str | None
    explanation: str | None                  # the LLM's explanation (narrative only)
    failure_reason: str | None               # the original reason, as saved in the audit payload
    attempts: list[ToolCallRecord]           # every proposal and the policy's verdict, in order
    simulated_action: AttemptRow | None      # the recorded simulated outcome (only for approved actions)
    created_at: datetime


class PaymentDetail(BaseModel):
    payment: PaymentRow
    subscription_plan: str | None
    recovery_attempts: list[AttemptRow]
    decisions: list[AuditRecord]
    customer_recent_payments: list[PaymentRow]


# ------------------------------------------------------------------ metrics

class CountByAction(BaseModel):
    action: str
    approved: int
    blocked: int


class CountByReason(BaseModel):
    reason: str
    count: int
    amount: float


class DayPoint(BaseModel):
    date: str
    approved: int
    blocked: int
    recovered_amount: float


class RecentActivity(BaseModel):
    decision_id: str
    payment_id: str
    customer_name: str
    action: str
    policy_status: str | None
    summary: str | None
    created_at: datetime


class DashboardMetrics(BaseModel):
    currency: str
    failed_payments: int
    revenue_at_risk: float
    recovered_payments: int
    revenue_recovered: float
    payments_processed: int                  # payments the agent has produced a decision for
    recovery_success_rate: float | None      # recovered_payments / payments_processed
    retries_settled: int
    retries_confirmed: int
    retry_confirmation_rate: float | None
    escalations_open: int
    decisions_total: int
    decisions_approved: int
    decisions_blocked: int
    by_action: list[CountByAction]
    by_failure_reason: list[CountByReason]
    by_day: list[DayPoint]
    recent_activity: list[RecentActivity]
    definitions: dict[str, str]


# ------------------------------------------------------------------ mock gateway

class GatewayStatus(BaseModel):
    demo_mode: bool
    llm_provider: str
    currency: str
    batch_enabled: bool = False
    batch_max: int = 0


class GatewayEventRequest(BaseModel):
    kind: EventKind


class GatewayEvent(BaseModel):
    kind: EventKind
    payment_id: str
    customer_id: str
    customer_name: str
    plan: str
    amount: float
    currency: str
    payment_status: str
    failure_reason: str | None
    received_at: datetime
    prior_retries: int


class PolicyPreview(BaseModel):
    """A dry run of the policy for a hypothetical retry. Nothing is executed or saved."""
    action: str
    allowed: bool
    rule: str
    reason: str
    category: str


class GatewayEventResponse(BaseModel):
    event: GatewayEvent
    eligible_for_recovery: bool
    ineligible_reason: str | None
    context_preview: dict | None             # what the agent will be shown (customer name/email excluded)
    retry_policy_preview: PolicyPreview | None


class SettleRequest(BaseModel):
    outcome: Literal["success", "failure"]


class BatchRequest(BaseModel):
    count: int = Field(ge=1, le=10)


class BatchItem(BaseModel):
    payment_id: str
    kind: EventKind
    result: Literal["completed", "blocked", "rejected", "error"]
    action_taken: str | None
    detail: str | None
