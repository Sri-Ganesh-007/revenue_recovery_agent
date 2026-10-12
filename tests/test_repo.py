from app.db.database import SessionLocal
from app.repositories.recovery_repository import RecoveryRepository



db = SessionLocal()

repository = RecoveryRepository(db)

payment = repository.get_payment("pay_004")

print("Payment:", payment.id)
print("Customer ID:", payment.customer_id)
print("Sub id",payment.subscription_id)

customer = repository.get_customer(payment.customer_id)

print("Customer:", customer.id)
print("Name:", customer.name)
print("Email:", customer.email)

sub = repository.get_subscription(payment.subscription_id)
print("sub_id", sub.id)
print("sub_plan",sub.plan)
print("sub status",sub.status)
payment_history = repository.get_payment_history(payment.customer_id)

print("\nPayment History:")

for payment in payment_history:
    print(
        payment.id,
        payment.amount,
        payment.status,
        payment.failure_reason
    )
recovery_history = repository.get_recovery_history(payment.customer_id)

print("\nRecovery History:")

for attempt in recovery_history:
    print(
        attempt.id,
        attempt.strategy,
        attempt.status,
        attempt.outcome
    )

db.close()