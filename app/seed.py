"""Тестовые данные из задания: один магазин, два ФФ, три артикула и тестовый приход."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import services
from app.models import Product, Warehouse

PRODUCTS = [("SKU-101", "Органайзер"), ("SKU-102", "Контейнер"), ("SKU-103", "Корзина")]
WAREHOUSES = ["Север", "Юг"]
RECEIPTS = {
    "SKU-101": {"Север": 10, "Юг": 6},
    "SKU-102": {"Север": 5, "Юг": 0},
    "SKU-103": {"Север": 0, "Юг": 4},
}


def seed(db: Session) -> None:
    """Заполняет пустую базу. На непустой ничего не делает."""
    if db.scalar(select(func.count()).select_from(Product)):
        return

    products = {sku: Product(sku=sku, name=name) for sku, name in PRODUCTS}
    warehouses = {name: Warehouse(name=name) for name in WAREHOUSES}
    db.add_all([*products.values(), *warehouses.values()])
    db.commit()

    # Приход идёт через тот же сервис, что и из интерфейса, поэтому попадает в историю.
    for sku, by_warehouse in RECEIPTS.items():
        for warehouse_name, qty in by_warehouse.items():
            if qty:
                services.receive_goods(
                    db,
                    products[sku].id,
                    warehouses[warehouse_name].id,
                    qty,
                    request_id=f"seed:{sku}:{warehouse_name}",
                )
