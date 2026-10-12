from sqlalchemy.orm import Session
from app.db.models import Payment,Customer,Subscription,RecoveryAttempt


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
