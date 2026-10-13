from sqlalchemy import Column,String,DateTime,Numeric,Text,JSON,ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base,relationship

Base = declarative_base()

# The real column is JSONB; plain JSON elsewhere (unit tests run on SQLite). No schema change.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Customer(Base):
    __tablename__ = "customers"
    id = Column(String(50),primary_key=True)
    name = Column(String(100),nullable=False)
    email = Column(String(255),nullable=False)
    created_at = Column(DateTime,nullable=False)

    subscriptions = relationship("Subscription",back_populates="customer")
    payments = relationship("Payment",back_populates="customer")
    recovery_attempts = relationship("RecoveryAttempt",back_populates="customer")
    agent_decisions = relationship("AgentDecision",back_populates="customer")



class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(String(50),primary_key=True)
    customer_id = Column(String(50),ForeignKey("customers.id"),nullable=False)
    status = Column(String(30),nullable=False)
    plan = Column(String(100),nullable=False)
    amount = Column(Numeric(12,2),nullable=False)
    currency = Column(String(10),nullable=False)
    billing_freq = Column(String(30),nullable=False)

    customer = relationship("Customer", back_populates="subscriptions")
    payments = relationship("Payment",back_populates="subscription")

class Payment(Base):
    __tablename__ = "payments"
    id = Column(String(50),primary_key=True)
    customer_id = Column(String(50),ForeignKey("customers.id"),nullable=False)
    subscription_id = Column(String(50),ForeignKey("subscriptions.id"),nullable=False)
    amount = Column(Numeric(12,2),nullable=False)
    currency = Column(String(10),nullable=False)
    status = Column(String(30),nullable=False)
    failure_reason = Column(String(100),nullable=True)
    created_at = Column(DateTime,nullable=False)

    customer = relationship("Customer",back_populates="payments")
    subscription = relationship("Subscription",back_populates="payments")
    recovery_attempts = relationship("RecoveryAttempt", back_populates="payment")
    agent_decisions = relationship("AgentDecision",back_populates="payment")

class RecoveryAttempt(Base):
    __tablename__ = "recovery_attempts"
    id = Column(String(50),primary_key=True)
    customer_id = Column(String(50),ForeignKey("customers.id"),nullable=False)
    payment_id = Column(String(50),ForeignKey("payments.id"),nullable=False)
    strategy = Column(String(50),nullable=False)
    status = Column(String(30),nullable=False)
    outcome = Column(String(100),nullable=True)
    attempted_at = Column(DateTime,nullable=False)

    customer = relationship("Customer",back_populates="recovery_attempts")
    payment = relationship("Payment",back_populates="recovery_attempts")

class AgentDecision(Base):
    __tablename__ = "agent_decisions"
    id = Column(String(50),primary_key=True)
    customer_id = Column(String(50),ForeignKey("customers.id"),nullable=False)
    payment_id = Column(String(50),ForeignKey("payments.id"),nullable=False)
    proposed_action = Column(String(50),nullable=False)
    action_parameters = Column(JSONType,nullable=True)
    decision_summary = Column(Text,nullable=True)
    policy_status = Column(String(30),nullable=True)
    policy_reason = Column(Text,nullable=True)
    created_at = Column(DateTime,nullable=False)

    customer = relationship("Customer",back_populates="agent_decisions")
    payment = relationship("Payment",back_populates="agent_decisions")
