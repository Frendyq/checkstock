"""Складские сценарии из задания: учёт, нехватка, удаление ФФ, повторные подтверждения."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import queries, services
from app.db import get_db
from app.main import app
from app.models import Operation, OperationKind, Shipment, ShipmentStatus, Stock
from app.seed import seed
from app.services import StockError


def last_shipment_id(db) -> int:
    return db.scalar(select(func.max(Shipment.id)))


def assert_books_balance(db) -> None:
    """Остатки в таблице stock совпадают с тем, что получается, если проиграть журнал операций с нуля."""
    db.expire_all()
    on_hand: dict[tuple, int] = defaultdict(int)
    reserved: dict[tuple, int] = defaultdict(int)
    for op in db.scalars(select(Operation).order_by(Operation.id)):
        source, target = (op.product_id, op.from_warehouse_id), (op.product_id, op.to_warehouse_id)
        match op.kind:
            case OperationKind.RECEIPT | OperationKind.TRANSFER_RECEIVE:
                on_hand[target] += op.qty
            case OperationKind.TRANSFER_SHIP | OperationKind.FBO_SHIP:
                on_hand[source] -= op.qty
            case OperationKind.FBS_RESERVE:
                reserved[source] += op.qty
            case OperationKind.FBS_CANCEL:
                reserved[source] -= op.qty
            case OperationKind.FBS_SHIP:
                on_hand[source] -= op.qty
                reserved[source] -= op.qty

    for stock in db.scalars(select(Stock)):
        key = (stock.product_id, stock.warehouse_id)
        assert (stock.on_hand, stock.reserved) == (on_hand[key], reserved[key]), key

    assert all(row["balanced"] for row in queries.stock_overview(db)["rows"])


# ---------- 40%: учёт после прихода, резерва, перемещения, отправки и приёмки ----------


def test_seed_matches_task_table_and_is_not_applied_twice(db, ids, stock_of):
    seed(db)  # повторный запуск на непустой базе

    assert stock_of(ids["SKU-101"], ids["Север"]) == (10, 0, 10)
    assert stock_of(ids["SKU-101"], ids["Юг"]) == (6, 0, 6)
    assert stock_of(ids["SKU-102"], ids["Север"]) == (5, 0, 5)
    assert stock_of(ids["SKU-102"], ids["Юг"]) == (0, 0, 0)
    assert stock_of(ids["SKU-103"], ids["Север"]) == (0, 0, 0)
    assert stock_of(ids["SKU-103"], ids["Юг"]) == (4, 0, 4)
    assert_books_balance(db)


def test_receipt_increases_stock(db, ids, stock_of, rid):
    services.receive_goods(db, ids["SKU-102"], ids["Юг"], 7, rid())

    assert stock_of(ids["SKU-102"], ids["Юг"]) == (7, 0, 7)
    assert_books_balance(db)


def test_fbs_reserve_blocks_stock_until_shipped(db, ids, stock_of, rid):
    sku, north = ids["SKU-101"], ids["Север"]

    services.create_fbs_reserve(db, sku, north, 4, rid())
    assert stock_of(sku, north) == (10, 4, 6)  # товар лежит на ФФ, но недоступен

    # Зарезервированное нельзя ни переместить, ни отправить по FBO.
    with pytest.raises(StockError):
        services.create_transfer(db, sku, north, ids["Юг"], 7, rid())
    with pytest.raises(StockError):
        services.create_fbo_shipment(db, sku, north, 7, rid())
    assert stock_of(sku, north) == (10, 4, 6)

    services.ship_fbs(db, last_shipment_id(db))
    assert stock_of(sku, north) == (6, 0, 6)  # товар уехал к клиенту
    row = queries.stock_overview(db)["rows"][0]
    assert row["transit"]["fbs"] == 4
    assert_books_balance(db)


def test_fbs_cancel_returns_stock_to_available(db, ids, stock_of, rid):
    sku, north = ids["SKU-101"], ids["Север"]
    services.create_fbs_reserve(db, sku, north, 4, rid())
    order = last_shipment_id(db)

    services.cancel_fbs(db, order)
    assert stock_of(sku, north) == (10, 0, 10)

    with pytest.raises(StockError):  # отменённый заказ отправить нельзя
        services.ship_fbs(db, order)
    assert stock_of(sku, north) == (10, 0, 10)
    assert_books_balance(db)


def test_shipped_fbs_cannot_be_cancelled(db, ids, stock_of, rid):
    sku, north = ids["SKU-101"], ids["Север"]
    services.create_fbs_reserve(db, sku, north, 4, rid())
    order = last_shipment_id(db)
    services.ship_fbs(db, order)

    with pytest.raises(StockError):
        services.cancel_fbs(db, order)
    assert stock_of(sku, north) == (6, 0, 6)


def test_transfer_is_in_transit_until_received(db, ids, stock_of, rid):
    sku, north, south = ids["SKU-102"], ids["Север"], ids["Юг"]

    services.create_transfer(db, sku, north, south, 3, rid())
    assert stock_of(sku, north) == (2, 0, 2)
    assert stock_of(sku, south) == (0, 0, 0)  # ещё не доехал
    row = next(r for r in queries.stock_overview(db)["rows"] if r["product"].id == sku)
    assert row["transit"]["transfer"] == 3
    assert_books_balance(db)

    services.complete_shipment(db, last_shipment_id(db))
    assert stock_of(sku, south) == (3, 0, 3)
    assert_books_balance(db)


def test_fbo_leaves_warehouse_and_stays_in_transit_until_accepted(db, ids, stock_of, rid):
    sku, south = ids["SKU-103"], ids["Юг"]

    services.create_fbo_shipment(db, sku, south, 3, rid())
    assert stock_of(sku, south) == (1, 0, 1)
    row = next(r for r in queries.stock_overview(db)["rows"] if r["product"].id == sku)
    assert (row["transit"]["fbo"], row["done_fbo"]) == (3, 0)

    services.complete_shipment(db, last_shipment_id(db))
    row = next(r for r in queries.stock_overview(db)["rows"] if r["product"].id == sku)
    assert (row["transit"]["fbo"], row["done_fbo"]) == (0, 3)
    assert stock_of(sku, south) == (1, 0, 1)
    assert_books_balance(db)


def test_history_shows_what_moved_from_where_to_where(db, ids, rid):
    services.create_transfer(db, ids["SKU-101"], ids["Север"], ids["Юг"], 2, rid())

    row = queries.history(db)[0]
    assert (row["title"], row["op"].product.sku, row["op"].qty) == ("Перемещение: отправка", "SKU-101", 2)
    assert (row["source"], row["target"]) == ("ФФ «Север»", "В пути на ФФ «Юг»")


# ---------- 25%: нехватка, удаление ФФ с остатком, повторное подтверждение ----------


def test_shortage_on_selected_warehouse_while_total_is_enough(db, ids, stock_of, rid):
    """SKU-101: на Юге 6, на Севере 10. Просим 8 с Юга — всего хватает, на выбранном ФФ нет."""
    sku, north, south = ids["SKU-101"], ids["Север"], ids["Юг"]

    for action in (services.create_fbs_reserve, services.create_fbo_shipment):
        with pytest.raises(StockError) as error:
            action(db, sku, south, 8, rid())
        message = str(error.value)
        assert "нужно 8, доступно 6" in message
        assert "«Север» — 10" in message  # подсказка, где товар есть
        assert "Выберите другой ФФ" in message

    # Ничего не списано и не зарезервировано, документы не созданы.
    assert stock_of(sku, south) == (6, 0, 6)
    assert stock_of(sku, north) == (10, 0, 10)
    assert last_shipment_id(db) is None
    assert_books_balance(db)


def test_shortage_message_when_total_is_not_enough_either(db, ids, rid):
    with pytest.raises(StockError) as error:
        services.create_fbs_reserve(db, ids["SKU-101"], ids["Юг"], 20, rid())
    assert "всего 16, этого тоже мало" in str(error.value)


def test_reserve_counts_as_shortage(db, ids, rid):
    """Физически товар есть, но он под резервом — для новой отправки его нет."""
    sku, south = ids["SKU-103"], ids["Юг"]
    services.create_fbs_reserve(db, sku, south, 3, rid())

    with pytest.raises(StockError) as error:
        services.create_fbo_shipment(db, sku, south, 2, rid())
    assert "доступно 1 (физически 4, в резерве FBS 3)" in str(error.value)


def test_deleted_warehouse_keeps_stock_and_history(db, ids, stock_of, rid):
    sku, north, south = ids["SKU-101"], ids["Север"], ids["Юг"]
    services.create_fbs_reserve(db, sku, south, 2, rid())
    order = last_shipment_id(db)
    services.create_transfer(db, ids["SKU-102"], north, south, 1, rid())  # уже едет на Юг
    transfer = last_shipment_id(db)
    operations_before = db.scalar(select(func.count()).select_from(Operation))

    result = services.delete_warehouse(db, south)
    assert "числится 10 шт. (в резерве FBS 2)" in result.message

    # Остатки и история на месте, ФФ виден на экране остатков с пометкой.
    assert stock_of(sku, south) == (6, 2, 4)
    assert stock_of(ids["SKU-103"], south) == (4, 0, 4)
    assert db.scalar(select(func.count()).select_from(Operation)) == operations_before
    overview = queries.stock_overview(db)
    assert [(w.name, bool(w.deleted_at)) for w in overview["warehouses"]] == [("Север", False), ("Юг", True)]
    assert queries.history(db)[0]["target"] == "В пути на ФФ «Юг» (удалён)"

    # Новое на удалённом ФФ начать нельзя.
    for action in (services.receive_goods, services.create_fbs_reserve, services.create_fbo_shipment):
        with pytest.raises(StockError, match="удалён"):
            action(db, sku, south, 1, rid())
    with pytest.raises(StockError, match="удалён"):
        services.create_transfer(db, sku, north, south, 1, rid())
    with pytest.raises(StockError, match="удалён"):
        services.rename_warehouse(db, south, "Юг-2")

    # Начатое можно завершить: отправить резерв и принять перемещение, которое уже в пути.
    services.ship_fbs(db, order)
    services.complete_shipment(db, transfer)
    assert stock_of(sku, south) == (4, 0, 4)
    assert stock_of(ids["SKU-102"], south) == (1, 0, 1)

    # Остаток можно вывезти на действующий ФФ.
    services.create_transfer(db, sku, south, north, 4, rid())
    services.complete_shipment(db, last_shipment_id(db))
    assert stock_of(sku, south) == (0, 0, 0)
    assert stock_of(sku, north) == (14, 0, 14)
    assert_books_balance(db)


def test_delete_twice_and_reuse_name(db, ids):
    services.delete_warehouse(db, ids["Юг"])
    assert services.delete_warehouse(db, ids["Юг"]).changed is False

    services.create_warehouse(db, "Юг")  # имя удалённого ФФ свободно
    with pytest.raises(StockError, match="уже есть"):
        services.create_warehouse(db, " Юг ")  # а среди действующих — уникально


def test_rename_warehouse_keeps_stock_and_history(db, ids, stock_of):
    services.rename_warehouse(db, ids["Север"], "Север-1")

    assert stock_of(ids["SKU-101"], ids["Север"]) == (10, 0, 10)
    assert queries.history(db)[-1]["target"] == "ФФ «Север-1»"  # история показывает актуальное имя
    with pytest.raises(StockError, match="уже есть"):
        services.rename_warehouse(db, ids["Север"], "Юг")


def test_repeated_form_submit_is_applied_once(db, ids, stock_of, rid):
    sku, north, south = ids["SKU-101"], ids["Север"], ids["Юг"]

    for action, expected in (
        (lambda key: services.receive_goods(db, sku, north, 5, key), (15, 0, 15)),
        (lambda key: services.create_fbs_reserve(db, sku, north, 2, key), (15, 2, 13)),
        (lambda key: services.create_fbo_shipment(db, sku, north, 3, key), (12, 2, 10)),
        (lambda key: services.create_transfer(db, sku, north, south, 4, key), (8, 2, 6)),
    ):
        key = rid()
        assert action(key).changed is True
        assert action(key).changed is False  # та же форма отправлена второй раз
        assert stock_of(sku, north) == expected

    assert db.scalar(select(func.count()).select_from(Shipment)) == 3
    assert_books_balance(db)


def test_repeated_confirmation_does_not_move_stock_twice(db, ids, stock_of, rid):
    sku, north, south = ids["SKU-101"], ids["Север"], ids["Юг"]

    services.create_fbs_reserve(db, sku, north, 4, rid())
    order = last_shipment_id(db)
    assert services.ship_fbs(db, order).changed is True
    assert services.ship_fbs(db, order).changed is False
    assert stock_of(sku, north) == (6, 0, 6)

    services.create_transfer(db, sku, north, south, 2, rid())
    transfer = last_shipment_id(db)
    assert services.complete_shipment(db, transfer).changed is True
    assert services.complete_shipment(db, transfer).changed is False
    assert stock_of(sku, south) == (8, 0, 8)

    services.create_fbs_reserve(db, sku, south, 1, rid())
    reserve = last_shipment_id(db)
    assert services.cancel_fbs(db, reserve).changed is True
    assert services.cancel_fbs(db, reserve).changed is False
    assert stock_of(sku, south) == (8, 0, 8)
    assert_books_balance(db)


def test_simultaneous_confirmations_ship_once(make_session, db, ids, stock_of, rid):
    """Два менеджера одновременно жмут «Отправить клиенту» на одном заказе."""
    sku, north = ids["SKU-101"], ids["Север"]
    services.create_fbs_reserve(db, sku, north, 4, rid())
    order = last_shipment_id(db)

    def confirm(_):
        with make_session() as session:
            return services.ship_fbs(session, order).changed

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(confirm, range(8)))

    assert results.count(True) == 1
    assert stock_of(sku, north) == (6, 0, 6)
    assert_books_balance(db)


def test_simultaneous_reserves_do_not_oversell(make_session, db, ids, stock_of):
    """На Юге 4 шт. SKU-103; шесть одновременных резервов по 1 шт. — пройти должны ровно четыре."""
    sku, south = ids["SKU-103"], ids["Юг"]

    def reserve(i):
        with make_session() as session:
            try:
                services.create_fbs_reserve(session, sku, south, 1, f"concurrent-{i}")
                return True
            except StockError:
                return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(6)))

    assert results.count(True) == 4
    assert stock_of(sku, south) == (4, 4, 0)
    assert_books_balance(db)


# ---------- сквозной сценарий и веб-слой ----------


def test_end_to_end_scenario_balances_after_every_step(db, ids, stock_of, rid):
    sku, north, south = ids["SKU-101"], ids["Север"], ids["Юг"]
    steps = [
        lambda: services.receive_goods(db, sku, north, 5, rid()),
        lambda: services.create_fbs_reserve(db, sku, north, 3, rid()),
        lambda: services.create_transfer(db, sku, north, south, 6, rid()),
        lambda: services.ship_fbs(db, 1),
        lambda: services.complete_shipment(db, 2),
        lambda: services.create_fbo_shipment(db, sku, south, 10, rid()),
        lambda: services.complete_shipment(db, 3),
        lambda: services.complete_shipment(db, 1),
        lambda: services.delete_warehouse(db, south),
    ]
    for step in steps:
        step()
        assert_books_balance(db)

    assert stock_of(sku, north) == (6, 0, 6)
    assert stock_of(sku, south) == (2, 0, 2)
    row = queries.stock_overview(db)["rows"][0]
    assert (row["received"], row["done_fbo"], row["done_fbs"], row["in_transit"]) == (21, 10, 3, 0)


@pytest.fixture
def client(make_session):
    def override():
        with make_session() as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_pages_render(client):
    for path in ("/", "/warehouses", "/history"):
        response = client.get(path)
        assert response.status_code == 200
        assert "SKU-101" in response.text or path == "/warehouses"


def test_form_resubmit_over_http_is_applied_once(client, db, ids, stock_of):
    form = {"product_id": ids["SKU-102"], "warehouse_id": ids["Север"], "qty": 3, "request_id": "browser-resubmit"}

    first = client.post("/fbo", data=form)
    second = client.post("/fbo", data=form)

    assert first.status_code == second.status_code == 303
    assert "ok=" in first.headers["location"] and "info=" in second.headers["location"]
    assert stock_of(ids["SKU-102"], ids["Север"]) == (2, 0, 2)


def test_shortage_over_http_changes_nothing(client, db, ids, stock_of):
    form = {"product_id": ids["SKU-101"], "warehouse_id": ids["Юг"], "qty": 8, "request_id": "shortage-form"}

    response = client.post("/fbs", data=form)

    assert "error=" in response.headers["location"]
    assert stock_of(ids["SKU-101"], ids["Юг"]) == (6, 0, 6)
    assert db.scalar(select(func.count()).select_from(Shipment).where(Shipment.status == ShipmentStatus.RESERVED)) == 0


def test_invalid_quantity_is_rejected(client):
    response = client.post("/receipts", data={"product_id": 1, "warehouse_id": 1, "qty": 0, "request_id": "bad-qty-1"})
    assert response.status_code == 303 and "error=" in response.headers["location"]
