"""Тесты идут на настоящем PostgreSQL: проверяем блокировки, CHECK-ограничения и частичный индекс.

Используется отдельная база checkstock_test на том же сервере — данные приложения не затрагиваются.
"""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, make_url, select, text
from sqlalchemy.orm import sessionmaker

from app.db import DATABASE_URL, Base
from app.models import Product, Stock, Warehouse
from app.seed import seed

TEST_DB = "checkstock_test"


@pytest.fixture(scope="session")
def engine():
    url = make_url(DATABASE_URL)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB}):
            conn.execute(text(f"CREATE DATABASE {TEST_DB}"))
    admin.dispose()

    engine = create_engine(url.set(database=TEST_DB))
    yield engine
    engine.dispose()


@pytest.fixture
def make_session(engine):
    """Чистая схема с тестовыми данными из задания; возвращает фабрику сессий."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        seed(db)
    return factory


@pytest.fixture
def db(make_session):
    with make_session() as session:
        yield session


@pytest.fixture
def ids(db):
    """Идентификаторы по понятным именам: ids['SKU-101'], ids['Север']."""
    result = {p.sku: p.id for p in db.scalars(select(Product))}
    result.update({w.name: w.id for w in db.scalars(select(Warehouse))})
    return result


@pytest.fixture
def stock_of(db):
    """(физически, резерв, доступно) артикула на ФФ."""

    def _stock_of(product_id: int, warehouse_id: int) -> tuple[int, int, int]:
        db.expire_all()
        stock = db.get(Stock, (product_id, warehouse_id))
        if stock is None:
            return 0, 0, 0
        return stock.on_hand, stock.reserved, stock.available

    return _stock_of


@pytest.fixture
def rid():
    """Новый ключ идемпотентности, как у свежеоткрытой формы."""
    return lambda: uuid4().hex
