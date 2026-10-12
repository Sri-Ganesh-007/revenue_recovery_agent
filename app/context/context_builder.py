from app.context.recovery_context import (
    RecoveryContext,
    CustomerContext,
    SubscriptionContext,
    FailedPaymentContext,
    PaymentPattern,
    PaymentHistoryItem,
    RecoveryHistoryItem,
    CurrentRecoveryState,
)


class RecoveryContextBuilder:
    def __init__(self, repository):
        self.repository = repository

    def build(self, payment_id: str) -> RecoveryContext:

        # 1. Retrieve the failed payment
        payment = self.repository.get_payment(payment_id)

        if payment is None:
            raise ValueError(f"Payment {payment_id} not found")
        if payment.status != "failed":
            raise ValueError(
                    f"Payment {payment_id} is not failed "
                    f"(current status: {payment.status})"
                )
        

        # 2. Retrieve customer and subscription
        customer = self.repository.get_customer(payment.customer_id)
        if customer is None:
            raise ValueError(f"Customer {payment.customer_id} not found")

        subscription = self.repository.get_subscription(payment.subscription_id)
        if subscription is None:
            raise ValueError(
                f"Subscription {payment.subscription_id} not found"
            )

        # 3. Retrieve payment and recovery histories
        payment_history = self.repository.get_payment_history(
            payment.customer_id
        )
        recovery_history = self.repository.get_recovery_history(
            payment_id
        )

        # 4. Calculate payment statistics
        total_payments = len(payment_history)

        successful_payments = sum(
            1 for p in payment_history if p.status == "success"
        )
        failed_payments = sum(
            1 for p in payment_history if p.status == "failed"
        )

        success_rate = (
            successful_payments / total_payments
            if total_payments > 0
            else 0.0
        )

        # 5. Convert the five most recent payments
        recent_history = [
            PaymentHistoryItem(
                id=p.id,
                status=p.status,
                amount=float(p.amount),
                currency=p.currency,
                failure_reason=p.failure_reason,
                created_at=p.created_at,
            )
            for p in payment_history[:5]
        ]

        payment_pattern = PaymentPattern(
            total_payments=total_payments,
            successful_payments=successful_payments,
            failed_payments=failed_payments,
            success_rate=success_rate,
            recent_history=recent_history,
        )

        # 6. Convert recovery history
        recovery_history_context = [
            RecoveryHistoryItem(
                strategy=r.strategy,
                status=r.status,
                outcome=r.outcome,
                attempted_at=r.attempted_at,
            )
            for r in recovery_history
        ]

        # 7. Derive current recovery state
        attempt_count = len(recovery_history)
        last_action = None
        last_action_at = None

        if recovery_history:
            latest_recovery = max(
                recovery_history,
                key=lambda r: r.attempted_at,
            )
            last_action = latest_recovery.strategy
            last_action_at = latest_recovery.attempted_at

        current_recovery_state = CurrentRecoveryState(
            attempt_count=attempt_count,
            last_action=last_action,
            last_action_at=last_action_at,
            next_scheduled_action=None,
        )

        # 8. Assemble and validate the final context
        return RecoveryContext(
            customer=CustomerContext(
                id=customer.id,
                name=customer.name,
                email=customer.email,
            ),
            subscription=SubscriptionContext(
                id=subscription.id,
                status=subscription.status,
                plan=subscription.plan,
                amount=float(subscription.amount),
                currency=subscription.currency,
                billing_frequency=subscription.billing_freq,
            ),
            failed_payment=FailedPaymentContext(
                id=payment.id,
                amount=float(payment.amount),
                currency=payment.currency,
                failure_reason=payment.failure_reason,
                failed_at=payment.created_at,
            ),
            payment_pattern=payment_pattern,
            recovery_history=recovery_history_context,
            current_recovery_state=current_recovery_state,
        )