"""门店跨区调整服务：登记（推导 from_region、回写门店当前归属）、查询。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.store import Store
from src.models.target import StoreRegionTransfer
from src.services import target_split
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc, local_now, parse_date


def serialize(transfer: StoreRegionTransfer, store: Store | None = None) -> dict:
    return {
        "id": transfer.id,
        "store_id": transfer.store_id,
        "store_code": store.code if store else None,
        "store_name": store.name if store else None,
        "from_region": transfer.from_region,
        "to_region": transfer.to_region,
        "effective_date": iso_date(transfer.effective_date),
        "reason": transfer.reason,
        "created_by": transfer.created_by,
        "created_at": iso_utc(transfer.created_at),
    }


def transfers_of(db: Session, store_id: int) -> list[StoreRegionTransfer]:
    return db.scalars(
        select(StoreRegionTransfer)
        .where(StoreRegionTransfer.store_id == store_id)
        .order_by(StoreRegionTransfer.effective_date, StoreRegionTransfer.id)
    ).all()


def list_transfers(db: Session, *, store_id: int | None = None, month: str | None = None) -> dict:
    stmt = select(StoreRegionTransfer)
    if store_id:
        stmt = stmt.where(StoreRegionTransfer.store_id == store_id)
    if month:
        try:
            month_start, month_end, _ = target_split.month_bounds(month)
        except ValueError as exc:
            raise AppError("VALIDATION", 422, {"field": "month", "message": str(exc)}) from exc
        stmt = stmt.where(StoreRegionTransfer.effective_date >= month_start)
        stmt = stmt.where(StoreRegionTransfer.effective_date <= month_end)
    rows = db.scalars(stmt.order_by(StoreRegionTransfer.effective_date, StoreRegionTransfer.id)).all()
    stores = {store.id: store for store in db.scalars(select(Store)).all()}
    return {"items": [serialize(row, stores.get(row.store_id)) for row in rows], "total": len(rows)}


def record_transfer(
    db: Session, store: Store, payload: dict, operator: str, today: date | None = None
) -> StoreRegionTransfer:
    effective = parse_date(payload.get("effective_date"))
    if effective is None:
        raise AppError("VALIDATION", 422, {"field": "effective_date", "message": "生效日期应为 YYYY-MM-DD"})
    to_region = (payload.get("to_region") or "").strip()
    if not to_region:
        raise AppError("VALIDATION", 422, {"field": "to_region", "message": "调入区域不能为空"})
    history = transfers_of(db, store.id)
    if any(item.effective_date == effective for item in history):
        raise AppError("CONFLICT", 409, {"store": store.code, "effective_date": iso_date(effective)})
    from_region = target_split.region_at(store.region, _as_dicts(history), effective)
    if to_region == from_region:
        raise AppError(
            "VALIDATION",
            422,
            {"field": "to_region", "message": f"生效日当时归属已是「{to_region}」，无需调整"},
        )
    transfer = StoreRegionTransfer(
        store_id=store.id,
        from_region=from_region,
        to_region=to_region,
        effective_date=effective,
        reason=(payload.get("reason") or "").strip() or None,
        created_by=operator,
        seed_key=payload.get("seed_key"),
    )
    db.add(transfer)
    db.flush()
    # 回写门店当前归属：已生效的最新一笔决定 stores.region；未生效的不动
    store.region = target_split.region_at(
        store.region, _as_dicts([*history, transfer]), today or local_now().date()
    )
    db.flush()
    return transfer


def _as_dicts(transfers: list[StoreRegionTransfer]) -> list[dict]:
    return [
        {
            "id": item.id,
            "effective_date": item.effective_date,
            "from_region": item.from_region,
            "to_region": item.to_region,
            "reason": item.reason or "",
        }
        for item in transfers
    ]


def transfers_by_store(db: Session, store_ids: set[int] | None = None) -> dict[int, list[dict]]:
    stmt = select(StoreRegionTransfer).order_by(StoreRegionTransfer.effective_date, StoreRegionTransfer.id)
    if store_ids:
        stmt = stmt.where(StoreRegionTransfer.store_id.in_(store_ids))
    grouped: dict[int, list[dict]] = {}
    for row in db.scalars(stmt).all():
        grouped.setdefault(row.store_id, []).append(
            {
                "id": row.id,
                "effective_date": row.effective_date,
                "from_region": row.from_region,
                "to_region": row.to_region,
                "reason": row.reason or "",
            }
        )
    return grouped
