"""Builds a fabricated RecoveryContext for tests (no database)."""
from datetime import datetime, timedelta

from app.context.recovery_context import (CurrentRecoveryState, CustomerContext, FailedPaymentContext,
                                          PaymentPattern, RecoveryContext, RecoveryHistoryItem,
                                          SubscriptionContext)

FAILED_AT = datetime(2026, 10, 11, 10, 0, 0)


def make_context(payment_id="pay_1", failure_reason="insufficient_funds", subscription_status="active",
                 history=(), failed_at=FAILED_AT):
    history = list(history)
    return RecoveryContext(
        customer=CustomerContext(id="c1", name="Jane Doe", email="jane@example.com"),
        subscription=SubscriptionContext(id="s1", status=subscription_status, plan="Premium", amount=499.0,
                                         currency="INR", billing_frequency="monthly"),
        failed_payment=FailedPaymentContext(id=payment_id, amount=499.0, currency="INR",
                                            failure_reason=failure_reason, failed_at=failed_at),
        payment_pattern=PaymentPattern(total_payments=1, successful_payments=0, failed_payments=1,
                                       success_rate=0.0, recent_history=[]),
        recovery_history=history,
        current_recovery_state=CurrentRecoveryState(
            attempt_count=len(history),
            last_action=history[0].strategy if history else None,
            last_action_at=history[0].attempted_at if history else None,
            next_scheduled_action=None),
    )


def history_item(strategy, status, attempted_at):
    return RecoveryHistoryItem(strategy=strategy, status=status, outcome=None, attempted_at=attempted_at)
