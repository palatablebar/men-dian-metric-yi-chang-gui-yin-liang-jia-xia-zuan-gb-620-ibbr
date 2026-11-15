"""区域月目标服务：目标下达、批次生成（幂等）、发布、中断恢复、三层对平查询。

事务与恢复约定（对应「重跑不重复、不改已发布版、中断先恢复旧版再续跑」）：
- 生成：先把批次置 running 并提交（prev_status 记录原状态），再在**单个事务**内
  删旧行 + 写新行 + 更新批次。中断 → 事务回滚，旧行原样保留，批次停在 running；
- 发布：先置 publishing 并提交，再在单个事务内归档旧已发布版 + 发布新版。
  中断 → 旧已发布版不受影响，批次停在 publishing；
- 恢复：每次生成/发布前 recover_interrupted 把停留在中间态的批次恢复旧版
  （全新批次没有已提交内容，直接删除），然后才续跑 —— 不会数据丢失；
- 幂等：重跑同一个月的草稿批次 = 原地替换分期行（自然键唯一约束兜底）；
  最新版已发布时重跑 = 新开版本号，已发布版内容不被改换。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.constants.enums import TARGET_BATCH_STATUS_LABELS
from src.models.store import Store
from src.models.target import StoreMonthTarget, TargetBatch, TargetLine
from src.services import audit_service, store_service, target_split, transfer_service
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_date, iso_utc, utcnow

INTERRUPTED_STATUSES = ("running", "publishing")


def _fmt_wan(cents: int) -> str:
    yuan = cents / 100
    if abs(yuan) >= 10000:
        return f"{yuan / 10000:.2f} 万"
    return f"{yuan:.0f} 元"


def _bounds_or_422(month: str) -> tuple[date, date, int]:
    try:
        return target_split.month_bounds(month)
    except ValueError as exc:
        raise AppError("VALIDATION", 422, {"field": "month", "message": str(exc)}) from exc


# ---- 序列化 ----

def serialize_batch(batch: TargetBatch) -> dict:
    return {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "month": batch.month.isoformat()[:7],
        "version": batch.version,
        "status": batch.status,
        "status_label": TARGET_BATCH_STATUS_LABELS.get(batch.status, batch.status),
        "attempt": batch.attempt,
        "operator_email": batch.operator_email,
        "store_count": batch.store_count,
        "line_count": batch.line_count,
        "total_target": round(batch.total_target, 2),
        "metrics": batch.metrics or {},
        "started_at": iso_utc(batch.started_at),
        "finished_at": iso_utc(batch.finished_at),
        "created_at": iso_utc(batch.created_at),
    }


def serialize_line(line: TargetLine) -> dict:
    return {
        "id": line.id,
        "store_id": line.store_id,
        "store_code": line.store_code,
        "store_name": line.store_name,
        "segment_no": line.segment_no,
        "region": line.region,
        "period_start": iso_date(line.period_start),
        "period_end": iso_date(line.period_end),
        "days": line.days,
        "target": round(line.target_cents / 100, 2),
        "transfer_id": line.transfer_id,
    }


def serialize_store_target(row: StoreMonthTarget, store: Store | None) -> dict:
    return {
        "id": row.id,
        "store_id": row.store_id,
        "store_code": store.code if store else None,
        "store_name": store.name if store else None,
        "region": store.region if store else None,
        "month": row.month.isoformat()[:7],
        "target_amount": round(row.target_amount, 2),
        "updated_by": row.updated_by,
        "updated_at": iso_utc(row.updated_at),
    }


# ---- 门店月目标（输入） ----

def upsert_store_target(db: Session, payload: dict, operator: str) -> tuple[StoreMonthTarget, Store]:
    store = store_service.get_by_code(db, (payload.get("store_code") or "").strip())
    if store is None:
        raise not_found("store", payload.get("store_code"))
    month_start, _, _ = _bounds_or_422(payload.get("month") or "")
    amount = round(float(payload.get("target_amount") or 0), 2)
    if amount <= 0:
        raise AppError("VALIDATION", 422, {"field": "target_amount", "message": "目标金额必须大于 0"})
    row = db.scalar(
        select(StoreMonthTarget).where(StoreMonthTarget.store_id == store.id, StoreMonthTarget.month == month_start)
    )
    if row is None:
        row = StoreMonthTarget(store_id=store.id, month=month_start, target_amount=amount, updated_by=operator)
        db.add(row)
    else:
        row.target_amount = amount
        row.updated_by = operator
    db.flush()
    return row, store


def list_store_targets(db: Session, *, month: str | None = None, region: str | None = None) -> dict:
    if month:
        month_start, _, _ = _bounds_or_422(month)
    else:
        month_start = db.scalar(select(func.max(StoreMonthTarget.month)))
    if month_start is None:
        return {"items": [], "total": 0, "month": None}
    stmt = select(StoreMonthTarget).where(StoreMonthTarget.month == month_start)
    rows = db.scalars(stmt).all()
    stores = {store.id: store for store in db.scalars(select(Store)).all()}
    items = [serialize_store_target(row, stores.get(row.store_id)) for row in rows]
    if region:
        items = [item for item in items if item["region"] == region]
    items.sort(key=lambda item: item["store_code"] or "")
    return {"items": items, "total": len(items), "month": month_start.isoformat()[:7]}


# ---- 中断恢复 ----

def _restore_batch(db: Session, batch: TargetBatch) -> None:
    """恢复旧版：重跑中断时旧分期行随事务回滚原样保留，只需把状态退回；
    全新批次没有任何已提交内容，连同行一起清掉。"""
    if batch.prev_status is None:
        db.execute(delete(TargetLine).where(TargetLine.batch_id == batch.id))
        db.delete(batch)
    else:
        batch.status = batch.prev_status
        batch.prev_status = None
    db.flush()


def recover_interrupted(db: Session, month_start: date, operator: str = "system") -> list[str]:
    """把该月停留在 running/publishing 的批次恢复旧版（先恢复，再续跑）。"""
    stuck = db.scalars(
        select(TargetBatch).where(
            TargetBatch.month == month_start,
            TargetBatch.status.in_(INTERRUPTED_STATUSES),
        )
    ).all()
    recovered: list[str] = []
    for batch in stuck:
        recovered.append(batch.batch_no)
        _restore_batch(db, batch)
    if recovered:
        audit_service.record(
            db,
            actor_email=operator,
            actor_role="system",
            action="target.recover",
            entity="target_batch",
            entity_id=None,
            detail={"month": month_start.isoformat()[:7], "recovered": recovered},
        )
        db.commit()
    return recovered


# ---- 批次生成与发布 ----

def build_plan(db: Session, month: str) -> dict:
    month_start, _, _ = _bounds_or_422(month)
    stores = db.scalars(select(Store).order_by(Store.code)).all()
    store_dicts = [{"id": store.id, "code": store.code, "name": store.name, "region": store.region} for store in stores]
    targets = db.scalars(select(StoreMonthTarget).where(StoreMonthTarget.month == month_start)).all()
    targets_cents = {row.store_id: int(round(row.target_amount * 100)) for row in targets}
    if not targets_cents:
        raise AppError("VALIDATION", 422, {"field": "month", "message": f"{month} 还没有下达任何门店月目标"})
    transfers = transfer_service.transfers_by_store(db)
    return target_split.build_batch_plan(month, store_dicts, targets_cents, transfers)


def generate(db: Session, month: str, operator: str, seed_key: str | None = None) -> TargetBatch:
    month_start, _, _ = _bounds_or_422(month)
    if seed_key:
        existing = db.scalar(select(TargetBatch).where(TargetBatch.seed_key == seed_key))
        if existing is not None:
            return existing
    recovered = recover_interrupted(db, month_start, operator)
    latest = db.scalar(
        select(TargetBatch)
        .where(TargetBatch.month == month_start)
        .order_by(TargetBatch.version.desc())
        .limit(1)
    )
    if latest is not None and latest.status == "draft":
        # 重跑同一目标批次：原地替换，不新增版本、不产生重复行
        batch = latest
        batch.prev_status = batch.status
    else:
        # 首次生成，或最新版已发布/归档：已发布版本不可改换 → 新开版本
        version = 1 if latest is None else latest.version + 1
        batch = TargetBatch(
            batch_no=f"TB{month_start:%Y%m}-{version:04d}",
            month=month_start,
            version=version,
            status="running",
            prev_status=None,
            operator_email=operator,
            seed_key=seed_key,
        )
        db.add(batch)
        db.flush()
    batch.status = "running"
    batch.attempt = (batch.attempt or 0) + 1
    batch.started_at = utcnow()
    db.commit()  # 中间态落库：进程中断后可被 recover_interrupted 发现并恢复
    try:
        plan = build_plan(db, month)
        reconcile = target_split.reconcile_plan(plan)
        # 单事务：删旧行 + 写新行 + 更新批次，要么全部生效要么全部回滚
        db.execute(delete(TargetLine).where(TargetLine.batch_id == batch.id))
        db.add_all(
            [
                TargetLine(
                    batch_id=batch.id,
                    store_id=line["store_id"],
                    store_code=line["store_code"],
                    store_name=line["store_name"],
                    segment_no=line["segment_no"],
                    region=line["region"],
                    period_start=line["period_start"],
                    period_end=line["period_end"],
                    days=line["days"],
                    target_cents=line["target_cents"],
                    target_amount=round(line["target_cents"] / 100, 2),
                    transfer_id=line["transfer_id"],
                )
                for line in plan["lines"]
            ]
        )
        batch.status = "draft"
        batch.prev_status = None
        batch.store_count = len(plan["store_totals"])
        batch.line_count = len(plan["lines"])
        batch.total_target = round(plan["group_cents"] / 100, 2)
        batch.metrics = {
            "month_days": plan["month_days"],
            "regions": len(plan["region_totals"]),
            "transfer_stores": len(target_split.transfer_impacts(plan, transfer_service.transfers_by_store(db))),
            "reconcile": reconcile,
            "recovered": recovered,
        }
        batch.finished_at = utcnow()
        db.commit()
    except Exception:
        db.rollback()
        _restore_batch(db, batch)  # 生成中断：先恢复旧版，不丢数据
        db.commit()
        raise
    return batch


def publish(db: Session, batch: TargetBatch, operator: str) -> TargetBatch:
    recover_interrupted(db, batch.month, operator)
    if batch.status == "published":
        return batch  # 幂等：重复发布不改动任何内容
    if batch.status != "draft":
        raise AppError("CONFLICT", 409, {"status": batch.status, "message": "只有草稿状态的批次可以发布"})
    batch.prev_status = batch.status
    batch.status = "publishing"
    db.commit()
    try:
        current = db.scalar(
            select(TargetBatch).where(
                TargetBatch.month == batch.month,
                TargetBatch.status == "published",
                TargetBatch.id != batch.id,
            )
        )
        if current is not None:
            current.status = "archived"  # 旧版保留可回查，内容不被改换
        batch.status = "published"
        batch.prev_status = None
        batch.finished_at = utcnow()
        db.commit()
    except Exception:
        db.rollback()
        _restore_batch(db, batch)  # 发布中断：恢复旧版（旧已发布版仍在）
        db.commit()
        raise
    return batch


# ---- 查询 ----

def list_batches(db: Session, *, month: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(TargetBatch)
    count_stmt = select(func.count()).select_from(TargetBatch)
    if month:
        month_start, _, _ = _bounds_or_422(month)
        stmt = stmt.where(TargetBatch.month == month_start)
        count_stmt = count_stmt.where(TargetBatch.month == month_start)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(TargetBatch.month.desc(), TargetBatch.version.desc()).limit(limit).offset(offset)
    ).all()
    return {"items": [serialize_batch(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_batch(db: Session, batch_id: int) -> TargetBatch:
    batch = db.get(TargetBatch, batch_id)
    if batch is None:
        raise not_found("target_batch", batch_id)
    return batch


def _lines_of(db: Session, batch_id: int) -> list[TargetLine]:
    return db.scalars(
        select(TargetLine)
        .where(TargetLine.batch_id == batch_id)
        .order_by(TargetLine.store_code, TargetLine.segment_no)
    ).all()


def _line_dicts(lines: list[TargetLine]) -> list[dict]:
    return [
        {
            "store_id": line.store_id,
            "store_code": line.store_code,
            "store_name": line.store_name,
            "segment_no": line.segment_no,
            "region": line.region,
            "period_start": line.period_start,
            "period_end": line.period_end,
            "days": line.days,
            "target_cents": line.target_cents,
            "transfer_id": line.transfer_id,
        }
        for line in lines
    ]


def _aggregate(lines: list[TargetLine]) -> dict:
    regions: dict[str, dict] = {}
    stores: dict[int, dict] = {}
    for line in lines:
        region = regions.setdefault(line.region, {"cents": 0, "store_ids": set(), "segments": 0})
        region["cents"] += line.target_cents
        region["store_ids"].add(line.store_id)
        region["segments"] += 1
        store = stores.setdefault(
            line.store_id,
            {"cents": 0, "segments": 0, "store_code": line.store_code, "store_name": line.store_name},
        )
        store["cents"] += line.target_cents
        store["segments"] += 1
    group_cents = sum(line.target_cents for line in lines)
    return {"regions": regions, "stores": stores, "group_cents": group_cents}


def _impact_notes(impacts: list[dict]) -> list[dict]:
    noted = []
    for impact in impacts:
        transitions = "；".join(
            f"{item['effective_date']:%m-%d} 起由「{item['from_region']}」调入「{item['to_region']}」"
            for item in impact["transitions"]
        )
        segments = "；".join(
            f"{seg['region']}段 {seg['period_start']:%m-%d}~{seg['period_end']:%m-%d}（{seg['days']} 天）{_fmt_wan(seg['target_cents'])}"
            for seg in impact["segments"]
        )
        noted.append(
            {
                **impact,
                "note": (
                    f"{impact['store_name']}（{impact['store_code']}）：{transitions}。"
                    f"{segments}。分段合计 {_fmt_wan(impact['total_cents'])}，仍等于该店月目标，不重复计。"
                ),
            }
        )
    return noted


def batch_detail(db: Session, batch: TargetBatch) -> dict:
    lines = _lines_of(db, batch.id)
    agg = _aggregate(lines)
    transfers = transfer_service.transfers_by_store(db, {line.store_id for line in lines})
    impacts = _impact_notes(target_split.transfer_impacts({"lines": _line_dicts(lines)}, transfers))
    region_cents = sum(item["cents"] for item in agg["regions"].values())
    store_cents = sum(item["cents"] for item in agg["stores"].values())
    group_cents = agg["group_cents"]
    # 区域调入/调出统计：只计生效日落本月的调整（更早的调整在影响说明里展示，不进本月计数）
    month_start, month_end, _ = target_split.month_bounds(batch.month.isoformat()[:7])
    move: dict[str, dict[str, int]] = {}
    for impact in impacts:
        for transition in impact["transitions"]:
            effective = transition["effective_date"]
            if not (month_start <= effective <= month_end):
                continue
            if transition["to_region"]:
                move.setdefault(transition["to_region"], {"in": 0, "out": 0})["in"] += 1
            if transition["from_region"]:
                move.setdefault(transition["from_region"], {"in": 0, "out": 0})["out"] += 1
    regions = []
    ranked = sorted(agg["regions"].items(), key=lambda item: (-item[1]["cents"], item[0]))
    for rank, (region, item) in enumerate(ranked, start=1):
        regions.append(
            {
                "region": region,
                "rank": rank,
                "target": round(item["cents"] / 100, 2),
                "share": round(item["cents"] / group_cents, 4) if group_cents else None,
                "store_count": len(item["store_ids"]),
                "segment_count": item["segments"],
                "transfer_in": move.get(region, {}).get("in", 0),
                "transfer_out": move.get(region, {}).get("out", 0),
            }
        )
    return {
        **serialize_batch(batch),
        "reconcile": {
            "group_total": round(group_cents / 100, 2),
            "region_sum": round(region_cents / 100, 2),
            "store_sum": round(store_cents / 100, 2),
            "diff_region": round((region_cents - group_cents) / 100, 2),
            "diff_store": round((store_cents - group_cents) / 100, 2),
            "balanced": group_cents == region_cents == store_cents,
        },
        "regions": regions,
        "transfers": [
            {
                "store_code": impact["store_code"],
                "store_name": impact["store_name"],
                "transitions": [
                    {
                        "from_region": item["from_region"],
                        "to_region": item["to_region"],
                        "effective_date": iso_date(item["effective_date"]),
                        "reason": item["reason"],
                    }
                    for item in impact["transitions"]
                ],
                "segments": [
                    {
                        "region": seg["region"],
                        "period_start": iso_date(seg["period_start"]),
                        "period_end": iso_date(seg["period_end"]),
                        "days": seg["days"],
                        "target": round(seg["target_cents"] / 100, 2),
                    }
                    for seg in impact["segments"]
                ],
                "total": round(impact["total_cents"] / 100, 2),
                "note": impact["note"],
            }
            for impact in impacts
        ],
    }


def region_detail(db: Session, batch: TargetBatch, region: str) -> dict:
    lines = _lines_of(db, batch.id)
    agg = _aggregate(lines)
    if region not in agg["regions"]:
        raise not_found("region", region)
    group_cents = agg["group_cents"]
    ranked = sorted(agg["regions"].items(), key=lambda item: (-item[1]["cents"], item[0]))
    rank = next(index for index, (name, _) in enumerate(ranked, start=1) if name == region)
    region_lines = [line for line in lines if line.region == region]
    # 门店贡献：同一门店在该区域内的各段合计
    store_map: dict[int, dict] = {}
    for line in region_lines:
        entry = store_map.setdefault(
            line.store_id,
            {"store_id": line.store_id, "store_code": line.store_code, "store_name": line.store_name, "cents": 0, "periods": []},
        )
        entry["cents"] += line.target_cents
        entry["periods"].append(f"{line.period_start:%m-%d}~{line.period_end:%m-%d}")
    stores = sorted(store_map.values(), key=lambda item: (-item["cents"], item["store_code"]))
    # 跨区调整影响（相对该区域标记调入/调出）
    transfers = transfer_service.transfers_by_store(db, {line.store_id for line in lines})
    impacts = _impact_notes(target_split.transfer_impacts({"lines": _line_dicts(lines)}, transfers))
    region_impacts = []
    for impact in impacts:
        directions = set()
        for transition in impact["transitions"]:
            if transition["to_region"] == region:
                directions.add("in")
            if transition["from_region"] == region:
                directions.add("out")
        if not directions and not any(seg["region"] == region for seg in impact["segments"]):
            continue
        region_impacts.append(
            {
                "store_code": impact["store_code"],
                "store_name": impact["store_name"],
                "directions": sorted(directions),
                "note": impact["note"],
            }
        )
    summary = agg["regions"][region]
    return {
        "batch": serialize_batch(batch),
        "region": region,
        "summary": {
            "target": round(summary["cents"] / 100, 2),
            "rank": rank,
            "share": round(summary["cents"] / group_cents, 4) if group_cents else None,
            "store_count": len(summary["store_ids"]),
            "segment_count": summary["segments"],
        },
        "segments": [serialize_line(line) for line in region_lines],
        "stores": [
            {
                "store_id": item["store_id"],
                "store_code": item["store_code"],
                "store_name": item["store_name"],
                "target": round(item["cents"] / 100, 2),
                "segment_count": len(item["periods"]),
                "periods": "、".join(item["periods"]),
            }
            for item in stores
        ],
        "impacts": region_impacts,
    }
