import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://checkstock:checkstock@localhost:5432/checkstock",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db


@contextmanager
def transaction(db: Session):
    """Одна складская операция = одна транзакция: либо всё, либо ничего."""
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise
