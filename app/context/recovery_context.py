from datetime import datetime
from pydantic import BaseModel


class CustomerContext(BaseModel):
    id: str
    name: str
    email: str


class SubscriptionContext(BaseModel):
    id: str
    status: str
    plan: str
    amount: float
    currency: str
    billing_frequency: str


class FailedPaymentContext(BaseModel):
    id: str
    amount: float
    currency: str
    failure_reason: str | None
    failed_at: datetime

class PaymentHistoryItem(BaseModel):
    id: str
    status: str
    amount: float
    currency: str
    failure_reason: str | None
    created_at: datetime


class PaymentPattern(BaseModel):
    total_payments: int
    successful_payments: int
    failed_payments: int
    success_rate: float
    recent_history: list[PaymentHistoryItem]


class RecoveryHistoryItem(BaseModel):
    strategy: str
    status: str
    outcome: str | None
    attempted_at: datetime


class CurrentRecoveryState(BaseModel):
    attempt_count: int
    last_action: str | None
    last_action_at: datetime | None
    next_scheduled_action: datetime | None


class RecoveryContext(BaseModel):
    customer: CustomerContext
    subscription: SubscriptionContext
    failed_payment: FailedPaymentContext
    payment_pattern: PaymentPattern
    recovery_history: list[RecoveryHistoryItem]
    current_recovery_state: CurrentRecoveryState