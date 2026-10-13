"""PostgreSQL integration tests on the separate *_test database.

SAFETY: every test runs inside ONE outer transaction that is always rolled back, so nothing is ever
committed: no rows, no schema changes. The teardown re-checks, from a fresh connection, that no row
with this test's unique prefix survived. Run with:

    TEST_DATABASE_URL=postgresql+psycopg2://USER:PASSWORD@localhost:5433/revenue_recovery_test pytest tests/test_database.py

What this does NOT test: two connections working at the same moment (that would need committed rows).
The same-key guarantee rests on the primary key, and different-key protection on the payment-row lock plus
the policy; the interleavings are simulated in test_recovery_service.py.
"""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AgentDecision, Customer, Payment, Subscription
from app.recovery.service import decision_id_for, run_recovery, utcnow
from app.repositories.recovery_repository import RecoveryRepository
from tests.service_fixtures import FakeDecisionMaker, escalate, retry, wait


class PgCase:
    def __init__(self, conn, prefix):
        self.conn, self.prefix = conn, prefix
        self.payment_id = f"{prefix}_pay"

    def session(self):
        return Session(self.conn, join_transaction_mode="create_savepoint", autoflush=False)

    def seed(self, failure_reason="insufficient_funds"):
        created = utcnow() - timedelta(days=1)
        db = self.session()
        db.add(Customer(id=f"{self.prefix}_c", name="Test", email=f"{self.prefix}@example.com", created_at=created))
        db.add(Subscription(id=f"{self.prefix}_s", customer_id=f"{self.prefix}_c", status="active", plan="Premium",
                            amount=499, currency="INR", billing_freq="monthly"))
        db.flush()
        db.add(Payment(id=self.payment_id, customer_id=f"{self.prefix}_c", subscription_id=f"{self.prefix}_s", amount=499,
                       currency="INR", status="failed", failure_reason=failure_reason, created_at=created))
        db.commit()
        db.close()

    def count(self, table, column="payment_id"):
        return self.conn.execute(text(f"select count(*) from {table} where {column} like :p"), {"p": self.prefix + "%"}).scalar()


@pytest.fixture
def pg(pg_test_url):
    engine = create_engine(pg_test_url)
    conn = engine.connect()
    outer = conn.begin()
    case = PgCase(conn, "zz" + uuid.uuid4().hex[:8])
    try:
        yield case
    finally:
        outer.rollback()
        conn.close()
        with engine.connect() as fresh:                                       # prove nothing was kept
            kept = sum(fresh.execute(text(f"select count(*) from {t} where {c} like :p"), {"p": case.prefix + "%"}).scalar()
                       for t, c in (("customers", "id"), ("subscriptions", "id"), ("payments", "id"),
                                    ("agent_decisions", "payment_id"), ("recovery_attempts", "payment_id")))
        engine.dispose()
        assert kept == 0, "a test left rows behind"


def test_the_original_tables_and_columns_are_there(pg):
    insp = inspect(pg.conn)
    expected = {"customers": {"id", "name", "email", "created_at"},
                "subscriptions": {"id", "customer_id", "status", "plan", "amount", "currency", "billing_freq"},
                "payments": {"id", "customer_id", "subscription_id", "amount", "currency", "status", "failure_reason", "created_at"},
                "recovery_attempts": {"id", "customer_id", "payment_id", "strategy", "status", "outcome", "attempted_at"},
                "agent_decisions": {"id", "customer_id", "payment_id", "proposed_action", "action_parameters",
                                    "decision_summary", "policy_status", "policy_reason", "created_at"}}
    for table, columns in expected.items():
        assert columns <= {c["name"] for c in insp.get_columns(table)}, table


def test_the_default_isolation_level_is_read_committed(pg):
    # The duplicate-retry protection reads fresh data after taking the payment lock; that relies on this.
    assert pg.conn.execute(text("show transaction_isolation")).scalar() == "read committed"


def test_an_approved_action_is_saved_correctly_on_postgres(pg):
    pg.seed()
    before = pg.conn.execute(text("select xmin::text, * from payments where id = :i"), {"i": pg.payment_id}).all()
    db = pg.session()
    result = run_recovery(db, pg.payment_id, f"{pg.prefix}-k", FakeDecisionMaker(retry(24, "Funds were short.")))
    db.close()

    assert result.run_status == "completed" and pg.count("agent_decisions") == 1 and pg.count("recovery_attempts") == 1
    row = pg.conn.execute(text("select policy_status, action_parameters->'attempts'->0->>'status', "
                               "action_parameters->>'failure_reason', action_parameters->'parameters'->>'retry_after_hours' "
                               "from agent_decisions where payment_id = :i"), {"i": pg.payment_id}).one()
    assert tuple(row) == ("approved", "executed_simulated", "insufficient_funds", "24")      # JSONB round trip
    assert pg.conn.execute(text("select status from recovery_attempts where payment_id = :i"), {"i": pg.payment_id}).scalar() == "scheduled"
    assert pg.conn.execute(text("select xmin::text, * from payments where id = :i"), {"i": pg.payment_id}).all() == before


def test_a_blocked_proposal_creates_no_action_row_on_postgres(pg):
    pg.seed(failure_reason="suspected_fraud")
    db = pg.session()
    result = run_recovery(db, pg.payment_id, f"{pg.prefix}-k", FakeDecisionMaker(retry(24), retry(48)))
    db.close()
    assert result.run_status == "blocked" and pg.count("agent_decisions") == 1 and pg.count("recovery_attempts") == 0


def test_a_repeated_key_is_replayed_from_postgres(pg):
    pg.seed()
    key = f"{pg.prefix}-k"
    db = pg.session()
    first = run_recovery(db, pg.payment_id, key, FakeDecisionMaker(wait("later")))
    replay = run_recovery(db, pg.payment_id, key, FakeDecisionMaker())                        # no LLM call allowed
    db.close()
    assert replay.idempotent_replay is True and replay.run_id == first.run_id and pg.count("agent_decisions") == 1


def test_the_primary_key_rejects_a_second_decision_for_the_same_key(pg):
    pg.seed()
    db = pg.session()
    run_recovery(db, pg.payment_id, f"{pg.prefix}-k", FakeDecisionMaker(wait("later")))
    with pytest.raises(IntegrityError):
        db.add(AgentDecision(id=decision_id_for(f"{pg.prefix}-k"), customer_id=f"{pg.prefix}_c", payment_id=pg.payment_id,
                             proposed_action="no_action", created_at=utcnow()))
        db.commit()
    db.rollback()
    db.close()


def test_the_payment_row_lock_can_be_taken_without_changing_the_payment(pg):
    pg.seed()
    before = pg.conn.execute(text("select xmin::text, * from payments where id = :i"), {"i": pg.payment_id}).all()
    db = pg.session()
    assert RecoveryRepository(db).lock_payment(pg.payment_id).id == pg.payment_id
    db.close()
    assert pg.conn.execute(text("select xmin::text, * from payments where id = :i"), {"i": pg.payment_id}).all() == before


def test_a_failed_write_rolls_the_decision_back_on_postgres(pg, monkeypatch):
    pg.seed()

    def boom(self, **fields):
        raise RuntimeError("simulated crash after the decision was written")

    monkeypatch.setattr(RecoveryRepository, "add_attempt", boom)
    db = pg.session()
    with pytest.raises(RuntimeError):
        run_recovery(db, pg.payment_id, f"{pg.prefix}-k", FakeDecisionMaker(retry(24)))
    db.close()
    assert pg.count("agent_decisions") == 0 and pg.count("recovery_attempts") == 0


def test_saving_a_decision_takes_the_payment_row_lock(pg):
    """The different-key duplicate protection depends on this lock, and SQLite cannot show it, so check the real SQL."""
    pg.seed()
    statements = []
    event.listen(pg.conn, "before_cursor_execute", lambda conn, cur, stmt, params, ctx, many: statements.append(stmt))
    db = pg.session()
    run_recovery(db, pg.payment_id, f"{pg.prefix}-k", FakeDecisionMaker(wait("later")))
    db.close()
    locking = [s for s in statements if "FROM payments" in s and s.rstrip().endswith("FOR UPDATE")]
    assert locking, "the save did not lock the payment row"
    assert not any(s.lstrip().upper().startswith(("UPDATE PAYMENTS", "INSERT INTO PAYMENTS", "DELETE FROM PAYMENTS"))
                   for s in statements)                                   # and it never writes to payments
