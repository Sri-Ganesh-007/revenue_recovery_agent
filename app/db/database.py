from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

DATABASE_URL = settings.database_url

# pool_pre_ping: drop connections that went stale (for example after the database container restarted).
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(
    autocommit = False,
    autoflush=False,
    bind=engine
)
