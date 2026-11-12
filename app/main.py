import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import queries, services
from app.db import Base, SessionLocal, engine, get_db
from app.models import Product, Warehouse
from app.seed import seed

APP_DIR = Path(__file__).parent
TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
    yield


app = FastAPI(title="CheckStock", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["localtime"] = lambda value: value.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M:%S")
# Каждая форма создания получает свой одноразовый ключ идемпотентности.
templates.env.globals["new_request_id"] = lambda: uuid4().hex

Db = Annotated[Session, Depends(get_db)]
Id = Annotated[int, Form()]
Qty = Annotated[int, Form(gt=0)]
RequestId = Annotated[str, Form(min_length=8, max_length=64)]


def run(action: Callable[[], services.Result], back: str) -> RedirectResponse:
    """Выполняет складское действие и возвращает на страницу с сообщением (Post/Redirect/Get)."""
    try:
        result = action()
        flash = {"ok" if result.changed else "info": result.message}
    except services.StockError as error:
        flash = {"error": str(error)}
    return RedirectResponse(f"{back}?{urlencode(flash)}", status_code=303)


@app.exception_handler(RequestValidationError)
def invalid_form(request: Request, exc: RequestValidationError) -> RedirectResponse:
    back = "/warehouses" if request.url.path.startswith("/warehouses") else "/"
    flash = {"error": "Проверьте форму: выберите товар и ФФ, количество — целое число больше нуля."}
    return RedirectResponse(f"{back}?{urlencode(flash)}", status_code=303)


# ---------- экраны ----------


@app.get("/")
def stock_page(request: Request, db: Db):
    warehouses = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    overview = queries.stock_overview(db)
    return templates.TemplateResponse(
        request,
        "stock.html",
        {
            "overview": overview,
            "products": db.scalars(select(Product).order_by(Product.sku)).all(),
            "active_warehouses": [wh for wh in warehouses if not wh.deleted_at],
            # Источником перемещения может быть и удалённый ФФ с остатком — чтобы вывезти товар.
            "source_warehouses": overview["warehouses"],
            "shipments": queries.open_shipments(db),
        },
    )


@app.get("/warehouses")
def warehouses_page(request: Request, db: Db):
    return templates.TemplateResponse(request, "warehouses.html", {"rows": queries.warehouses_with_stock(db)})


@app.get("/history")
def history_page(request: Request, db: Db):
    return templates.TemplateResponse(request, "history.html", {"rows": queries.history(db)})


# ---------- движения товара ----------


@app.post("/receipts")
def post_receipt(db: Db, product_id: Id, warehouse_id: Id, qty: Qty, request_id: RequestId):
    return run(lambda: services.receive_goods(db, product_id, warehouse_id, qty, request_id), "/")


@app.post("/transfers")
def post_transfer(
    db: Db, product_id: Id, from_warehouse_id: Id, to_warehouse_id: Id, qty: Qty, request_id: RequestId
):
    return run(
        lambda: services.create_transfer(db, product_id, from_warehouse_id, to_warehouse_id, qty, request_id), "/"
    )


@app.post("/fbs")
def post_fbs_reserve(db: Db, product_id: Id, warehouse_id: Id, qty: Qty, request_id: RequestId):
    return run(lambda: services.create_fbs_reserve(db, product_id, warehouse_id, qty, request_id), "/")


@app.post("/fbo")
def post_fbo_shipment(db: Db, product_id: Id, warehouse_id: Id, qty: Qty, request_id: RequestId):
    return run(lambda: services.create_fbo_shipment(db, product_id, warehouse_id, qty, request_id), "/")


@app.post("/shipments/{shipment_id}/ship")
def post_ship(db: Db, shipment_id: int):
    return run(lambda: services.ship_fbs(db, shipment_id), "/")


@app.post("/shipments/{shipment_id}/cancel")
def post_cancel(db: Db, shipment_id: int):
    return run(lambda: services.cancel_fbs(db, shipment_id), "/")


@app.post("/shipments/{shipment_id}/complete")
def post_complete(db: Db, shipment_id: int):
    return run(lambda: services.complete_shipment(db, shipment_id), "/")


# ---------- склады ФФ ----------


@app.post("/warehouses")
def post_warehouse(db: Db, name: Annotated[str, Form()]):
    return run(lambda: services.create_warehouse(db, name), "/warehouses")


@app.post("/warehouses/{warehouse_id}/rename")
def post_rename(db: Db, warehouse_id: int, name: Annotated[str, Form()]):
    return run(lambda: services.rename_warehouse(db, warehouse_id, name), "/warehouses")


@app.post("/warehouses/{warehouse_id}/delete")
def post_delete(db: Db, warehouse_id: int):
    return run(lambda: services.delete_warehouse(db, warehouse_id), "/warehouses")
