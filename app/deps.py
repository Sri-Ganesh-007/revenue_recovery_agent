"""FastAPI dependencies shared by main.py and the routers (tests override these)."""
from functools import lru_cache

from app.db.database import SessionLocal
from app.recovery.decision_maker import DecisionMaker


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@lru_cache
def get_decision_maker():
    return DecisionMaker()
