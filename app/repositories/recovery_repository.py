from sqlalchemy.orm import Session
from app.db.models import Payment,Customer,Subscription,RecoveryAttempt,AgentDecision


class RecoveryRepository:
    def __init__(self,db:Session):
        self.db = db

    def get_payment(self,payment_id:str):
        return (
            self.db.query(Payment).filter(Payment.id == payment_id).first()
        )
    def get_customer(self,customer_id:str):
        return(
            self.db.query(Customer).filter(Customer.id == customer_id).first()
        )
    def get_subscription(self,subscription_id:str):
        return(
            self.db.query(Subscription).filter(Subscription.id==subscription_id).first()
        )
    
    def get_payment_history(self, customer_id: str):
        return(
        self.db.query(Payment)
        .filter(Payment.customer_id == customer_id).order_by(Payment.created_at.desc()).all()
    )
    def get_recovery_history(self, payment_id: str):
        return (
        self.db.query(RecoveryAttempt)
        .filter(RecoveryAttempt.payment_id == payment_id).order_by(RecoveryAttempt.attempted_at.desc()).all()
    )

    # ---- used by the recovery service ----
    def lock_payment(self, payment_id: str):
        """Lock the payment row (SELECT ... FOR UPDATE) so concurrent recoveries for it run one at a time.
        This only locks; the payments row is never written."""
        return self.db.query(Payment).populate_existing().filter(Payment.id == payment_id).with_for_update().first()

    def get_decision(self, decision_id: str):
        return self.db.query(AgentDecision).populate_existing().filter(AgentDecision.id == decision_id).first()

    def get_attempt(self, attempt_id: str):
        return self.db.query(RecoveryAttempt).populate_existing().filter(RecoveryAttempt.id == attempt_id).first()

    def add_decision(self, **fields):
        row = AgentDecision(**fields)
        self.db.add(row)
        return row

    def add_attempt(self, **fields):
        row = RecoveryAttempt(**fields)
        self.db.add(row)
        return row
