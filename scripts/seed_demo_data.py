#!/usr/bin/env python
"""Seed SYNTHETIC demo data: about 100 fictional customers with subscriptions and payment histories.

Safe to run repeatedly: every record has a stable id, and only records that do not exist yet are inserted.
It never updates, deletes or truncates anything and never creates tables (use setup_neon_demo.py or your normal
setup for the schema). Synthetic records are recognisable by their ids (demo_*) and e-mails (@demo.example).

  # 1) DRY RUN (default): connects read-only, prints the target and what WOULD be inserted
  DATABASE_URL='postgresql+psycopg2://USER:PASSWORD@HOST/DBNAME?sslmode=require' python scripts/seed_demo_data.py

  # 2) APPLY: inserts only the missing rows
  DATABASE_URL='postgresql+psycopg2://USER:PASSWORD@HOST/DBNAME?sslmode=require' python scripts/seed_demo_data.py --apply

The database is taken ONLY from the DATABASE_URL environment variable (never from .env), so it can never seed a
database by accident. The password is never printed.
"""
import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, inspect, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.models import Customer, Payment, RecoveryAttempt, Subscription  # noqa: E402

N_CUSTOMERS = 100
N_CURRENT_FAILURES = 36
SEED = 20260101
PLANS = [("Starter", 499), ("Growth", 999), ("Scale", 2499)]
FIRST = ["Aarav", "Diya", "Vihaan", "Anaya", "Arjun", "Ishita", "Kabir", "Meera", "Rohan", "Saanvi", "Aditya", "Kavya",
         "Nikhil", "Tara", "Rahul", "Neha", "Karan", "Pooja", "Siddharth", "Riya"]
LAST = ["Sharma", "Iyer", "Reddy", "Patel", "Nair", "Gupta", "Mehta", "Rao", "Das", "Kapoor", "Menon", "Joshi", "Bose", "Shetty"]
# current failures: (reason, weight). All are labels the policy knows; only insufficient_funds is retryable.
FAILURES = [("insufficient_funds", 40), ("gateway_timeout", 18), ("expired_card", 10), ("processor_decline", 8),
            ("authentication_required", 7), ("invalid_payment_details", 5), ("suspected_fraud", 3), ("processing_limit_exceeded", 3),
            ("transaction_not_allowed", 2)]
SCENARIOS = {   # deterministic demo cases: key -> (reason, status, days ago, prior declined retries)
    "funds": ("insufficient_funds", "failed", 1.0, 0),
    "timeout": ("gateway_timeout", "failed", 1.0, 0),
    "success": (None, "success", 1.0, 0),
    "retrylimit": ("insufficient_funds", "failed", 6.0, 3),
    "expired": ("expired_card", "failed", 2.0, 0),
}
DECLINED = "SIMULATED: processor DECLINED retry"


def build_dataset(now: datetime) -> dict[str, list]:
    """Pure and deterministic: the same `now` always gives the same records."""
    rng = random.Random(SEED)
    customers, subscriptions, payments, attempts = [], [], [], []

    def add_customer(cid, sid, name, plan, amount, started):
        customers.append(Customer(id=cid, name=name, email=f"{cid}@demo.example", created_at=started))
        subscriptions.append(Subscription(id=sid, customer_id=cid, status="active", plan=plan, amount=amount,
                                          currency="INR", billing_freq="monthly"))

    failing = set(rng.sample(range(1, N_CUSTOMERS + 1), N_CURRENT_FAILURES))
    reasons = [r for r, w in FAILURES for _ in range(w)]
    for n in range(1, N_CUSTOMERS + 1):
        cid, sid = f"demo_cust_{n:03d}", f"demo_sub_{n:03d}"
        plan, amount = rng.choices(PLANS, weights=[5, 3, 1])[0]
        months = rng.randint(3, 9)
        add_customer(cid, sid, f"{rng.choice(FIRST)} {rng.choice(LAST)}", plan, amount, now - timedelta(days=30 * months + rng.randint(1, 20)))
        for m in range(1, months + 1):                          # monthly history, all paid
            payments.append(Payment(id=f"demo_pay_{n:03d}_{m:02d}", customer_id=cid, subscription_id=sid, amount=amount,
                                    currency="INR", status="success", failure_reason=None,
                                    created_at=now - timedelta(days=30 * m + rng.randint(0, 3))))
        if n in failing:                                        # this month's payment failed
            payments.append(Payment(id=f"demo_pay_{n:03d}_cur", customer_id=cid, subscription_id=sid, amount=amount, currency="INR",
                                    status="failed", failure_reason=rng.choice(reasons),
                                    created_at=now - timedelta(days=round(rng.uniform(0.3, 9.0), 2))))
        else:
            payments.append(Payment(id=f"demo_pay_{n:03d}_cur", customer_id=cid, subscription_id=sid, amount=amount, currency="INR",
                                    status="success", failure_reason=None, created_at=now - timedelta(days=rng.randint(0, 5))))

    for i, (key, (reason, status, days_ago, prior)) in enumerate(SCENARIOS.items(), start=1):
        cid, sid = f"demo_cust_scn_{i:02d}", f"demo_sub_scn_{i:02d}"
        add_customer(cid, sid, f"Scenario {key.title()} (demo)", "Growth", 999, now - timedelta(days=200))
        pid = f"demo_pay_scn_{key}"
        payments.append(Payment(id=pid, customer_id=cid, subscription_id=sid, amount=999, currency="INR", status=status,
                                failure_reason=reason, created_at=now - timedelta(days=days_ago)))
        for k, ago in enumerate((5.0, 3.5, 2.0)[:prior], start=1):
            attempts.append(RecoveryAttempt(id=f"demo_att_scn_{key}_{k}", customer_id=cid, payment_id=pid, strategy="schedule_retry",
                                            status="executed_simulated", outcome=DECLINED, attempted_at=now - timedelta(days=ago)))
    return {"customers": customers, "subscriptions": subscriptions, "payments": payments, "attempts": attempts}


TABLES = [("customers", Customer), ("subscriptions", Subscription), ("payments", Payment), ("attempts", RecoveryAttempt)]


def plan_and_apply(session: Session, dataset: dict[str, list], apply: bool) -> dict[str, tuple[int, int]]:
    """Insert only the rows whose id is not there yet. Returns {table: (already_there, inserted_or_to_insert)}."""
    summary = {}
    for name, model in TABLES:
        wanted = dataset[name]
        existing = set(session.scalars(select(model.id).where(model.id.like("demo\\_%", escape="\\"))))
        missing = [row for row in wanted if row.id not in existing]
        if apply and missing:
            session.add_all(missing)
            session.flush()                                      # parents first (TABLES is in foreign-key order)
        summary[name] = (len(wanted) - len(missing), len(missing))
    if apply:
        session.commit()
    else:
        session.rollback()
    return summary


def main(argv=None, env=None) -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic demo data (dry run unless --apply).")
    parser.add_argument("--apply", action="store_true", help="actually insert the missing rows")
    args = parser.parse_args(argv)
    url = (os.environ if env is None else env).get("DATABASE_URL", "").strip()
    if not url:
        print("ERROR: set the DATABASE_URL environment variable to the database you want to seed. (.env is deliberately not used.)")
        return 2
    target = make_url(url)
    print(f"Target database : {target.database!r} on host {target.host or '(local file/memory)'} (driver {target.drivername})")
    print(f"Mode            : {'APPLY - inserting missing rows' if args.apply else 'DRY RUN - nothing will be written'}")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        missing_tables = {"customers", "subscriptions", "payments", "recovery_attempts"} - set(inspect(engine).get_table_names())
        if missing_tables:
            print(f"ERROR: these tables do not exist: {sorted(missing_tables)}. This script never creates tables.")
            return 3
        now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        with Session(engine) as session:
            summary = plan_and_apply(session, build_dataset(now), args.apply)
    finally:
        engine.dispose()
    print(f"{'Inserted' if args.apply else 'Would insert'} (already there -> {'inserted' if args.apply else 'to insert'}):")
    for name, (there, new) in summary.items():
        print(f"  {name:14} {there:5} already there, {new:5} {'inserted' if args.apply else 'to insert'}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
