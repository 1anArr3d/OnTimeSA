from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

Base = declarative_base()

# Lazily constructed so importing models (e.g. in tests, against a different
# DB) doesn't force a connection using whatever DATABASE_URL happens to be
# configured - or blow up when it isn't configured at all.
_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def get_db():
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
