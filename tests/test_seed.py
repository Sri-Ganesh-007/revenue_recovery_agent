"""The demo seed script: deterministic, idempotent, referentially valid, non-destructive, and consistent with the policy."""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.context.context_builder import RecoveryContextBuilder
from app.db.models import AgentDecision, Customer, Payment, RecoveryAttempt, Subscription
from app.recovery.policy import evaluate, snapshot_from_context
from app.repositories.recovery_repository import RecoveryRepository
from tests.service_fixtures import make_factory

spec = importlib.util.spec_from_file_location("seed_demo_data", Path(__file__).resolve().parent.parent / "scripts" / "seed_demo_data.py")
seed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed)

NOW = datetime(2026, 10, 14, 9, 0, 0)


@pytest.fixture
def db():
    session = make_factory()()
    yield session
    session.close()


def table_counts(db):
    return {n: db.scalar(select(func.count()).select_from(m)) for n, m in
            [("customers", Customer), ("subscriptions", Subscription), ("payments", Payment), ("attempts", RecoveryAttempt)]}


def test_dataset_is_deterministic_and_about_a_hundred_customers():
    a, b = seed.build_dataset(NOW), seed.build_dataset(NOW)
    assert [c.id for c in a["customers"]] == [c.id for c in b["customers"]]
    assert [(p.id, p.status, p.failure_reason) for p in a["payments"]] == [(p.id, p.status, p.failure_reason) for p in b["payments"]]
    assert len(a["customers"]) == 105 and len(a["subscriptions"]) == 105          # 100 + 5 deterministic scenarios
    assert len(a["payments"]) > 400


def test_every_record_is_marked_synthetic_and_references_are_valid():
    d = seed.build_dataset(NOW)
    assert all(c.id.startswith("demo_") and c.email.endswith("@demo.example") for c in d["customers"])
    assert all(r.id.startswith("demo_") for k in d for r in d[k])
    ids = {c.id for c in d["customers"]}
    subs = {s.id: s for s in d["subscriptions"]}
    pays = {p.id for p in d["payments"]}
    assert len({c.email for c in d["customers"]}) == len(d["customers"]) and len({p.id for p in d["payments"]}) == len(d["payments"])
    assert all(s.customer_id in ids for s in subs.values())
    assert all(p.customer_id in ids and subs[p.subscription_id].customer_id == p.customer_id for p in d["payments"])
    assert all(a.payment_id in pays and a.customer_id in ids for a in d["attempts"])


def test_the_dataset_covers_the_requested_payment_situations():
    d = seed.build_dataset(NOW)
    assert any(p.status == "success" for p in d["payments"])
    reasons = {p.failure_reason for p in d["payments"] if p.status == "failed"}
    assert {"insufficient_funds", "gateway_timeout"} <= reasons and len(reasons) >= 5
    failed = [p for p in d["payments"] if p.status == "failed"]
    assert 30 <= len(failed) <= 45 and all(p.created_at >= NOW - timedelta(days=10) for p in failed)   # recent: still inside the window
    assert all(c.status == "active" for c in d["subscriptions"])


def test_applying_twice_inserts_nothing_the_second_time(db):
    first = seed.plan_and_apply(db, seed.build_dataset(NOW), apply=True)
    counts = table_counts(db)
    second = seed.plan_and_apply(db, seed.build_dataset(NOW), apply=True)
    assert all(new == 0 for _, new in second.values()) and table_counts(db) == counts
    assert sum(new for _, new in first.values()) == sum(counts.values())


def test_it_even_survives_a_later_day_without_duplicates_or_changes(db):
    seed.plan_and_apply(db, seed.build_dataset(NOW), apply=True)
    before = {r[0]: r[1:] for r in db.execute(text("select id, status, created_at from payments"))}
    later = seed.plan_and_apply(db, seed.build_dataset(NOW + timedelta(days=3)), apply=True)
    assert all(new == 0 for _, new in later.values())
    assert {r[0]: r[1:] for r in db.execute(text("select id, status, created_at from payments"))} == before     # nothing overwritten


def test_dry_run_writes_nothing(db):
    summary = seed.plan_and_apply(db, seed.build_dataset(NOW), apply=False)
    assert all(v == 0 for v in table_counts(db).values()) and sum(new for _, new in summary.values()) > 500


def test_unrelated_existing_data_is_left_alone(db):
    db.add(Customer(id="real_1", name="Real Person", email="real@example.com", created_at=NOW))
    db.flush()
    db.add(Subscription(id="real_s1", customer_id="real_1", status="active", plan="Pro", amount=10, currency="USD", billing_freq="monthly"))
    db.flush()
    db.add(Payment(id="real_p1", customer_id="real_1", subscription_id="real_s1", amount=10, currency="USD", status="failed",
                   failure_reason="insufficient_funds", created_at=NOW))
    db.commit()
    seed.plan_and_apply(db, seed.build_dataset(NOW), apply=True)
    assert db.get(Customer, "real_1").name == "Real Person" and db.get(Payment, "real_p1").status == "failed"
    assert db.scalar(select(func.count()).select_from(AgentDecision)) == 0


def test_the_deterministic_scenarios_behave_as_labelled_under_the_real_policy(db):
    seed.plan_and_apply(db, seed.build_dataset(NOW), apply=True)
    repo = RecoveryRepository(db)

    def retry_verdict(pid):
        ctx = RecoveryContextBuilder(repo).build(pid)
        return evaluate(snapshot_from_context(ctx, NOW), "schedule_retry", {"retry_after_hours": 24})

    funds = retry_verdict("demo_pay_scn_funds")
    assert funds.allowed and funds.rule == "all_rules_passed"
    assert not retry_verdict("demo_pay_scn_timeout").allowed
    assert retry_verdict("demo_pay_scn_retrylimit").rule == "retry_cap"
    assert retry_verdict("demo_pay_scn_expired").rule == "failure_category"
    with pytest.raises(ValueError, match="not failed"):
        RecoveryContextBuilder(repo).build("demo_pay_scn_success")


def test_the_script_refuses_to_run_without_an_explicit_database_url(capsys):
    assert seed.main([], env={}) == 2
    assert "DATABASE_URL" in capsys.readouterr().out


def test_the_script_never_prints_the_password_and_never_creates_tables(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'empty.db'}"                               # a database file with no tables
    assert seed.main(["--apply"], env={"DATABASE_URL": url}) == 3
    out = capsys.readouterr().out
    assert "do not exist" in out
    pg = "postgresql+psycopg2://someuser:SuperSecretPw@127.0.0.1:1/x_db"        # unreachable: only the printed target matters
    with pytest.raises(Exception):
        seed.main([], env={"DATABASE_URL": pg})
    assert "SuperSecretPw" not in capsys.readouterr().out


def test_end_to_end_via_main_on_a_prepared_sqlite_file(tmp_path, capsys):
    from sqlalchemy import create_engine
    from app.db.models import Base
    path = tmp_path / "demo.db"
    Base.metadata.create_all(create_engine(f"sqlite:///{path}"))
    env = {"DATABASE_URL": f"sqlite:///{path}"}
    assert seed.main([], env=env) == 0 and "DRY RUN" in capsys.readouterr().out
    assert seed.main(["--apply"], env=env) == 0
    first = capsys.readouterr().out
    assert "customers" in first and "     0 inserted" not in first.split("customers")[1].split("\n")[0]       # real inserts
    assert seed.main(["--apply"], env=env) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if line.strip().split(" ")[0] in {"customers", "subscriptions", "payments", "attempts"}]
    assert len(rows) == 4 and all(line.rstrip().endswith(" 0 inserted") for line in rows), rows            # second run: nothing new
