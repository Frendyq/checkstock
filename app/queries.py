"""Чтение для экранов: остатки, документы в работе, история."""

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Operation,
    OperationKind,
    Product,
    Shipment,
    ShipmentKind,
    ShipmentStatus,
    Stock,
    Warehouse,
)

OPEN_STATUSES = (ShipmentStatus.RESERVED, ShipmentStatus.IN_TRANSIT)


def stock_overview(db: Session) -> dict:
    """Матрица «артикул × ФФ» плюс товар в пути и сверка по каждому артикулу."""
    products = db.scalars(select(Product).order_by(Product.sku)).all()
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    stocks = {(s.product_id, s.warehouse_id): s for s in db.scalars(select(Stock))}

    # (товар, тип документа, статус) -> штук
    shipped: dict[tuple, int] = defaultdict(int)
    for product_id, kind, status, qty in db.execute(
        select(Shipment.product_id, Shipment.kind, Shipment.status, func.sum(Shipment.qty)).group_by(
            Shipment.product_id, Shipment.kind, Shipment.status
        )
    ):
        shipped[product_id, kind, status] = qty

    received = dict(
        db.execute(
            select(Operation.product_id, func.sum(Operation.qty))
            .where(Operation.kind == OperationKind.RECEIPT)
            .group_by(Operation.product_id)
        ).all()
    )

    # Удалённый ФФ остаётся на экране, пока на нём что-то числится или к нему едет перемещение.
    busy = {wh_id for (_, wh_id), s in stocks.items() if s.on_hand}
    busy |= set(
        db.scalars(
            select(Shipment.to_warehouse_id).where(
                Shipment.kind == ShipmentKind.TRANSFER, Shipment.status == ShipmentStatus.IN_TRANSIT
            )
        )
    )
    visible = [wh for wh in warehouses if not wh.deleted_at or wh.id in busy]

    rows = []
    for product in products:
        cells = []
        for wh in visible:
            stock = stocks.get((product.id, wh.id))
            on_hand = stock.on_hand if stock else 0
            reserved = stock.reserved if stock else 0
            cells.append({"on_hand": on_hand, "reserved": reserved, "available": on_hand - reserved})

        # Итог считаем по всем ФФ, а не только по показанным: ни одна штука не должна потеряться.
        on_hand = sum(s.on_hand for (pid, _), s in stocks.items() if pid == product.id)
        reserved = sum(s.reserved for (pid, _), s in stocks.items() if pid == product.id)

        transit = {kind: shipped[product.id, kind, ShipmentStatus.IN_TRANSIT] for kind in ShipmentKind}
        done_fbo = shipped[product.id, ShipmentKind.FBO, ShipmentStatus.DONE]
        done_fbs = shipped[product.id, ShipmentKind.FBS, ShipmentStatus.DONE]
        received_total = received.get(product.id, 0)

        rows.append(
            {
                "product": product,
                "cells": cells,
                "total": {"on_hand": on_hand, "reserved": reserved, "available": on_hand - reserved},
                "transit": transit,
                "in_transit": sum(transit.values()),
                "done_fbo": done_fbo,
                "done_fbs": done_fbs,
                "received": received_total,
                # Сверка: всё принятое где-то числится, а резерв равен открытым заказам FBS.
                "balanced": (
                    received_total == on_hand + sum(transit.values()) + done_fbo + done_fbs
                    and reserved == shipped[product.id, ShipmentKind.FBS, ShipmentStatus.RESERVED]
                ),
            }
        )

    return {"warehouses": visible, "rows": rows}


def open_shipments(db: Session) -> list[Shipment]:
    """Документы в работе: резервы FBS и всё, что в пути."""
    return db.scalars(
        select(Shipment)
        .where(Shipment.status.in_(OPEN_STATUSES))
        .options(
            selectinload(Shipment.product),
            selectinload(Shipment.from_warehouse),
            selectinload(Shipment.to_warehouse),
        )
        .order_by(Shipment.id.desc())
    ).all()


def warehouses_with_stock(db: Session) -> list[dict]:
    """Все ФФ, включая удалённые, и что на каждом числится."""
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.deleted_at.isnot(None), Warehouse.id)).all()
    items: dict[int, list] = defaultdict(list)
    for stock, product in db.execute(
        select(Stock, Product).join(Product, Product.id == Stock.product_id).where(Stock.on_hand > 0).order_by(Product.sku)
    ):
        items[stock.warehouse_id].append({"product": product, "on_hand": stock.on_hand, "reserved": stock.reserved})

    inbound = dict(
        db.execute(
            select(Shipment.to_warehouse_id, func.sum(Shipment.qty))
            .where(Shipment.kind == ShipmentKind.TRANSFER, Shipment.status == ShipmentStatus.IN_TRANSIT)
            .group_by(Shipment.to_warehouse_id)
        ).all()
    )
    return [
        {
            "warehouse": wh,
            "items": items[wh.id],
            "on_hand": sum(i["on_hand"] for i in items[wh.id]),
            "reserved": sum(i["reserved"] for i in items[wh.id]),
            "inbound": inbound.get(wh.id, 0),
        }
        for wh in warehouses
    ]


# Название операции и «откуда → куда» для истории.
# {from} и {to} — ФФ операции; имена берутся актуальные, с пометкой «удалён».
_ROUTES = {
    OperationKind.RECEIPT: ("Приход", "Поставщик", "ФФ {to}"),
    OperationKind.TRANSFER_SHIP: ("Перемещение: отправка", "ФФ {from}", "В пути на ФФ {to}"),
    OperationKind.TRANSFER_RECEIVE: ("Перемещение: приёмка", "В пути с ФФ {from}", "ФФ {to}"),
    OperationKind.FBS_RESERVE: ("FBS: резерв", "ФФ {from}: доступно", "ФФ {from}: резерв FBS"),
    OperationKind.FBS_CANCEL: ("FBS: отмена резерва", "ФФ {from}: резерв FBS", "ФФ {from}: доступно"),
    OperationKind.FBS_SHIP: ("FBS: отправка", "ФФ {from}: резерв FBS", "В пути к клиенту"),
    OperationKind.FBS_DELIVER: ("FBS: доставка", "В пути к клиенту", "Клиент"),
    OperationKind.FBO_SHIP: ("FBO: отправка", "ФФ {from}", "В пути на склад маркетплейса"),
    OperationKind.FBO_RECEIVE: ("FBO: приёмка", "В пути на склад маркетплейса", "Склад маркетплейса"),
}


def history(db: Session, limit: int = 300) -> list[dict]:
    operations = db.scalars(
        select(Operation)
        .options(
            selectinload(Operation.product),
            selectinload(Operation.from_warehouse),
            selectinload(Operation.to_warehouse),
        )
        .order_by(Operation.id.desc())
        .limit(limit)
    ).all()

    rows = []
    for op in operations:
        title, source, target = _ROUTES[OperationKind(op.kind)]
        names = {
            "from": op.from_warehouse.label if op.from_warehouse else "",
            "to": op.to_warehouse.label if op.to_warehouse else "",
        }
        rows.append(
            {
                "op": op,
                "title": title,
                "source": source.format(**names),
                "target": target.format(**names),
            }
        )
    return rows
