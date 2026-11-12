from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ShipmentKind(StrEnum):
    TRANSFER = "transfer"  # перемещение между ФФ
    FBS = "fbs"  # заказ со склада ФФ к клиенту
    FBO = "fbo"  # поставка на склад маркетплейса


class ShipmentStatus(StrEnum):
    RESERVED = "reserved"  # только FBS: товар заблокирован на ФФ
    IN_TRANSIT = "in_transit"  # товар покинул ФФ и едет
    DONE = "done"  # принят получателем
    CANCELLED = "cancelled"  # только FBS: резерв снят


class OperationKind(StrEnum):
    RECEIPT = "receipt"
    TRANSFER_SHIP = "transfer_ship"
    TRANSFER_RECEIVE = "transfer_receive"
    FBS_RESERVE = "fbs_reserve"
    FBS_CANCEL = "fbs_cancel"
    FBS_SHIP = "fbs_ship"
    FBS_DELIVER = "fbs_deliver"
    FBO_SHIP = "fbo_ship"
    FBO_RECEIVE = "fbo_receive"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))


class Warehouse(Base):
    """Склад фулфилмента (ФФ). Удаление мягкое: остатки и история ссылаются на строку."""

    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Имя уникально только среди действующих ФФ: удалённый не мешает завести новый с тем же именем.
        Index("uq_active_warehouse_name", "name", unique=True, postgresql_where=text("deleted_at IS NULL")),
    )

    @property
    def label(self) -> str:
        return f"«{self.name}»" + (" (удалён)" if self.deleted_at else "")


class Stock(Base):
    """Текущий остаток артикула на ФФ. Доступно = физически − резерв FBS."""

    __tablename__ = "stock"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(default=0)
    reserved: Mapped[int] = mapped_column(default=0)

    # Последний рубеж: даже при ошибке в коде база не даст уйти в минус
    # или зарезервировать больше, чем лежит на складе.
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_stock_on_hand_non_negative"),
        CheckConstraint("reserved >= 0 AND reserved <= on_hand", name="ck_stock_reserved_within_on_hand"),
    )

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class Shipment(Base):
    """Документ движения: перемещение между ФФ, заказ FBS или поставка FBO."""

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int]
    from_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))  # только у перемещений
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped[Product] = relationship()
    from_warehouse: Mapped[Warehouse] = relationship(foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[to_warehouse_id])

    __table_args__ = (CheckConstraint("qty > 0", name="ck_shipment_qty_positive"),)


class Operation(Base):
    """Журнал операций. Строки только добавляются — это и есть история."""

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(30))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int]
    from_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.id"))
    # Ключ идемпотентности формы: повторная отправка той же формы не создаёт вторую операцию.
    request_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    product: Mapped[Product] = relationship()
    from_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[from_warehouse_id])
    to_warehouse: Mapped[Warehouse | None] = relationship(foreign_keys=[to_warehouse_id])

    __table_args__ = (CheckConstraint("qty > 0", name="ck_operation_qty_positive"),)
