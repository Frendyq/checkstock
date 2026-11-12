"""Складские операции. Все правила учёта живут здесь.

Каждая функция — одна транзакция: блокирует нужные строки (SELECT ... FOR UPDATE),
проверяет правило, меняет остаток и пишет строку в журнал операций.

Защита от повторов двух видов:
* создание (приход, перемещение, резерв, FBO) — по request_id формы;
* подтверждение (отправить, принять, отменить) — по статусу документа под блокировкой.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import transaction
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


class StockError(Exception):
    """Нарушено складское правило. Текст показывается менеджеру как есть."""


@dataclass
class Result:
    message: str
    changed: bool = True  # False — повтор: остатки не тронуты


REPEAT = Result("Эта операция уже проведена — повторная отправка формы ничего не изменила.", changed=False)


# ---------- вспомогательное ----------


def _is_repeat(db: Session, request_id: str) -> bool:
    # Advisory-блокировка на ключ: два одновременных запроса с одним request_id
    # выстраиваются в очередь, и второй уже видит операцию первого.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtext(request_id))))
    return db.scalar(select(Operation.id).where(Operation.request_id == request_id)) is not None


def _check_qty(qty: int) -> None:
    if qty <= 0:
        raise StockError("Количество должно быть больше нуля.")


def _get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise StockError("Товар не найден.")
    return product


def _get_warehouse(db: Session, warehouse_id: int, *, active_for: str | None = None) -> Warehouse:
    """active_for — для чего нужен склад; если задано, удалённый ФФ не подойдёт."""
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise StockError("ФФ не найден.")
    if active_for and warehouse.deleted_at:
        raise StockError(
            f"ФФ «{warehouse.name}» удалён: {active_for} нельзя. "
            "С удалённого ФФ можно только вывезти остаток перемещением и завершить уже начатые документы."
        )
    return warehouse


def _lock_stock(db: Session, product_id: int, warehouse_id: int) -> Stock:
    db.execute(
        pg_insert(Stock)
        .values(product_id=product_id, warehouse_id=warehouse_id, on_hand=0, reserved=0)
        .on_conflict_do_nothing()
    )
    return db.execute(
        select(Stock)
        .where(Stock.product_id == product_id, Stock.warehouse_id == warehouse_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def _lock_available(db: Session, product: Product, warehouse: Warehouse, qty: int) -> Stock:
    """Блокирует остаток и проверяет, что на выбранном ФФ хватает доступного товара."""
    stock = _lock_stock(db, product.id, warehouse.id)
    if stock.available >= qty:
        return stock

    message = (
        f"Не хватает {product.sku} на ФФ «{warehouse.name}»: "
        f"нужно {qty}, доступно {stock.available} "
        f"(физически {stock.on_hand}, в резерве FBS {stock.reserved})."
    )
    elsewhere = db.execute(
        select(Warehouse, Stock.on_hand - Stock.reserved)
        .join(Stock, Stock.warehouse_id == Warehouse.id)
        .where(
            Stock.product_id == product.id,
            Stock.warehouse_id != warehouse.id,
            Stock.on_hand - Stock.reserved > 0,
        )
        .order_by(Warehouse.id)
    ).all()
    if not elsewhere:
        raise StockError(message + " На других ФФ доступного остатка нет.")

    places = ", ".join(f"{wh.label} — {available}" for wh, available in elsewhere)
    total = stock.available + sum(available for _, available in elsewhere)
    if total >= qty:
        message += f" На других ФФ доступно: {places}. Выберите другой ФФ или сначала оформите перемещение."
    else:
        message += f" На других ФФ доступно: {places} — всего {total}, этого тоже мало."
    raise StockError(message)


def _lock_shipment(db: Session, shipment_id: int) -> Shipment:
    shipment = db.execute(
        select(Shipment)
        .where(Shipment.id == shipment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if shipment is None:
        raise StockError("Документ не найден.")
    return shipment


def _log(
    db: Session,
    kind: OperationKind,
    product_id: int,
    qty: int,
    *,
    from_id: int | None = None,
    to_id: int | None = None,
    shipment: Shipment | None = None,
    request_id: str | None = None,
) -> None:
    db.add(
        Operation(
            kind=kind,
            product_id=product_id,
            qty=qty,
            from_warehouse_id=from_id,
            to_warehouse_id=to_id,
            shipment_id=shipment.id if shipment else None,
            request_id=request_id,
        )
    )


# ---------- создание: приход, перемещение, резерв FBS, отправка FBO ----------


def receive_goods(db: Session, product_id: int, warehouse_id: int, qty: int, request_id: str) -> Result:
    """Приход: товар появился на ФФ."""
    _check_qty(qty)
    with transaction(db):
        if _is_repeat(db, request_id):
            return REPEAT
        product = _get_product(db, product_id)
        warehouse = _get_warehouse(db, warehouse_id, active_for="оформить приход")
        stock = _lock_stock(db, product.id, warehouse.id)
        stock.on_hand += qty
        _log(db, OperationKind.RECEIPT, product.id, qty, to_id=warehouse.id, request_id=request_id)
        return Result(f"Приход: {product.sku} × {qty} на ФФ «{warehouse.name}».")


def create_transfer(
    db: Session, product_id: int, from_id: int, to_id: int, qty: int, request_id: str
) -> Result:
    """Отправка перемещения: товар сразу покидает ФФ-отправитель и числится в пути."""
    _check_qty(qty)
    if from_id == to_id:
        raise StockError("Выберите разные ФФ для отправки и получения.")
    with transaction(db):
        if _is_repeat(db, request_id):
            return REPEAT
        product = _get_product(db, product_id)
        source = _get_warehouse(db, from_id)  # с удалённого ФФ вывозить можно
        target = _get_warehouse(db, to_id, active_for="принимать на него перемещения")
        stock = _lock_available(db, product, source, qty)
        stock.on_hand -= qty
        shipment = Shipment(
            kind=ShipmentKind.TRANSFER,
            status=ShipmentStatus.IN_TRANSIT,
            product_id=product.id,
            qty=qty,
            from_warehouse_id=source.id,
            to_warehouse_id=target.id,
        )
        db.add(shipment)
        db.flush()
        _log(
            db,
            OperationKind.TRANSFER_SHIP,
            product.id,
            qty,
            from_id=source.id,
            to_id=target.id,
            shipment=shipment,
            request_id=request_id,
        )
        return Result(
            f"Перемещение №{shipment.id}: {product.sku} × {qty} отправлено "
            f"с «{source.name}» на «{target.name}» — в пути до приёмки."
        )


def create_fbs_reserve(db: Session, product_id: int, warehouse_id: int, qty: int, request_id: str) -> Result:
    """Резерв под заказ FBS: товар остаётся на ФФ, но перестаёт быть доступным."""
    _check_qty(qty)
    with transaction(db):
        if _is_repeat(db, request_id):
            return REPEAT
        product = _get_product(db, product_id)
        warehouse = _get_warehouse(db, warehouse_id, active_for="резервировать на нём товар")
        stock = _lock_available(db, product, warehouse, qty)
        stock.reserved += qty
        shipment = Shipment(
            kind=ShipmentKind.FBS,
            status=ShipmentStatus.RESERVED,
            product_id=product.id,
            qty=qty,
            from_warehouse_id=warehouse.id,
        )
        db.add(shipment)
        db.flush()
        _log(
            db,
            OperationKind.FBS_RESERVE,
            product.id,
            qty,
            from_id=warehouse.id,
            shipment=shipment,
            request_id=request_id,
        )
        return Result(f"Заказ FBS №{shipment.id}: {product.sku} × {qty} зарезервировано на «{warehouse.name}».")


def create_fbo_shipment(db: Session, product_id: int, warehouse_id: int, qty: int, request_id: str) -> Result:
    """Отправка FBO: товар покидает ФФ и едет на склад маркетплейса."""
    _check_qty(qty)
    with transaction(db):
        if _is_repeat(db, request_id):
            return REPEAT
        product = _get_product(db, product_id)
        warehouse = _get_warehouse(db, warehouse_id, active_for="отправлять с него поставки FBO")
        stock = _lock_available(db, product, warehouse, qty)
        stock.on_hand -= qty
        shipment = Shipment(
            kind=ShipmentKind.FBO,
            status=ShipmentStatus.IN_TRANSIT,
            product_id=product.id,
            qty=qty,
            from_warehouse_id=warehouse.id,
        )
        db.add(shipment)
        db.flush()
        _log(
            db,
            OperationKind.FBO_SHIP,
            product.id,
            qty,
            from_id=warehouse.id,
            shipment=shipment,
            request_id=request_id,
        )
        return Result(
            f"Поставка FBO №{shipment.id}: {product.sku} × {qty} ушло с «{warehouse.name}» "
            "на склад маркетплейса — в пути до приёмки."
        )


# ---------- подтверждения: меняют статус документа ровно один раз ----------


def ship_fbs(db: Session, shipment_id: int) -> Result:
    """Отправка заказа FBS: резерв превращается в списание, товар едет к клиенту."""
    with transaction(db):
        shipment = _lock_shipment(db, shipment_id)
        if shipment.kind != ShipmentKind.FBS:
            raise StockError("Отправить клиенту можно только заказ FBS.")
        if shipment.status in (ShipmentStatus.IN_TRANSIT, ShipmentStatus.DONE):
            return Result(f"Заказ FBS №{shipment.id} уже отправлен — повторно товар не списан.", changed=False)
        if shipment.status == ShipmentStatus.CANCELLED:
            raise StockError(f"Заказ FBS №{shipment.id} отменён, отправить его нельзя.")

        stock = _lock_stock(db, shipment.product_id, shipment.from_warehouse_id)
        stock.on_hand -= shipment.qty
        stock.reserved -= shipment.qty
        shipment.status = ShipmentStatus.IN_TRANSIT
        _log(
            db,
            OperationKind.FBS_SHIP,
            shipment.product_id,
            shipment.qty,
            from_id=shipment.from_warehouse_id,
            shipment=shipment,
        )
        return Result(f"Заказ FBS №{shipment.id} отправлен: товар едет к клиенту.")


def cancel_fbs(db: Session, shipment_id: int) -> Result:
    """Отмена резерва FBS: товар снова доступен на ФФ."""
    with transaction(db):
        shipment = _lock_shipment(db, shipment_id)
        if shipment.kind != ShipmentKind.FBS:
            raise StockError("Отменить можно только резерв FBS.")
        if shipment.status == ShipmentStatus.CANCELLED:
            return Result(f"Резерв FBS №{shipment.id} уже отменён.", changed=False)
        if shipment.status != ShipmentStatus.RESERVED:
            raise StockError(f"Заказ FBS №{shipment.id} уже отправлен клиенту, отменить резерв нельзя.")

        stock = _lock_stock(db, shipment.product_id, shipment.from_warehouse_id)
        stock.reserved -= shipment.qty
        shipment.status = ShipmentStatus.CANCELLED
        _log(
            db,
            OperationKind.FBS_CANCEL,
            shipment.product_id,
            shipment.qty,
            from_id=shipment.from_warehouse_id,
            shipment=shipment,
        )
        return Result(f"Резерв FBS №{shipment.id} отменён: товар снова доступен.")


def complete_shipment(db: Session, shipment_id: int) -> Result:
    """Приёмка: перемещение — на ФФ-получателе, FBO — складом маркетплейса, FBS — клиентом."""
    with transaction(db):
        shipment = _lock_shipment(db, shipment_id)
        if shipment.status == ShipmentStatus.DONE:
            return Result(f"Документ №{shipment.id} уже принят — повторно товар не зачислен.", changed=False)
        if shipment.status == ShipmentStatus.RESERVED:
            raise StockError(f"Заказ FBS №{shipment.id} ещё не отправлен.")
        if shipment.status == ShipmentStatus.CANCELLED:
            raise StockError(f"Заказ FBS №{shipment.id} отменён.")

        shipment.status = ShipmentStatus.DONE
        if shipment.kind == ShipmentKind.TRANSFER:
            # Принимаем даже на удалённый ФФ: товар физически приехал и должен где-то числиться.
            stock = _lock_stock(db, shipment.product_id, shipment.to_warehouse_id)
            stock.on_hand += shipment.qty
            kind, message = OperationKind.TRANSFER_RECEIVE, "принято на ФФ-получателе"
        elif shipment.kind == ShipmentKind.FBO:
            kind, message = OperationKind.FBO_RECEIVE, "принято складом маркетплейса"
        else:
            kind, message = OperationKind.FBS_DELIVER, "доставлено клиенту"
        _log(
            db,
            kind,
            shipment.product_id,
            shipment.qty,
            from_id=shipment.from_warehouse_id,
            to_id=shipment.to_warehouse_id,
            shipment=shipment,
        )
        return Result(f"Документ №{shipment.id}: {message}.")


# ---------- склады ФФ ----------


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise StockError("Укажите название ФФ.")
    if len(name) > 100:
        raise StockError("Название ФФ длиннее 100 символов.")
    return name


def create_warehouse(db: Session, name: str) -> Result:
    name = _clean_name(name)
    try:
        with transaction(db):
            db.add(Warehouse(name=name))
    except IntegrityError:
        raise StockError(f"ФФ с названием «{name}» уже есть.") from None
    return Result(f"ФФ «{name}» добавлен.")


def rename_warehouse(db: Session, warehouse_id: int, name: str) -> Result:
    name = _clean_name(name)
    try:
        with transaction(db):
            warehouse = _get_warehouse(db, warehouse_id, active_for="переименовать его")
            old_name = warehouse.name
            warehouse.name = name
    except IntegrityError:
        raise StockError(f"ФФ с названием «{name}» уже есть.") from None
    return Result(f"ФФ «{old_name}» переименован в «{name}». В истории он теперь под новым именем.")


def delete_warehouse(db: Session, warehouse_id: int) -> Result:
    """Мягкое удаление: остатки, резервы и история остаются привязанными к ФФ."""
    with transaction(db):
        warehouse = _get_warehouse(db, warehouse_id)
        if warehouse.deleted_at:
            return Result(f"ФФ «{warehouse.name}» уже удалён.", changed=False)
        warehouse.deleted_at = datetime.now(timezone.utc)

        on_hand, reserved = db.execute(
            select(func.coalesce(func.sum(Stock.on_hand), 0), func.coalesce(func.sum(Stock.reserved), 0)).where(
                Stock.warehouse_id == warehouse.id
            )
        ).one()
        message = f"ФФ «{warehouse.name}» удалён."
        if on_hand:
            message += (
                f" На нём по-прежнему числится {on_hand} шт. (в резерве FBS {reserved}): "
                "остатки и история сохранены. Вывезите товар перемещением на действующий ФФ."
            )
        return Result(message)
