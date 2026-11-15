"""区域月目标控制器：目标下达、批次生成/发布、对平查询，写操作记领域审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, target_service


def list_batches(db: Session, **filters) -> dict:
    return target_service.list_batches(db, **filters)


def get_batch(db: Session, batch_id: int) -> dict:
    return target_service.batch_detail(db, target_service.get_batch(db, batch_id))


def get_region(db: Session, batch_id: int, region: str) -> dict:
    return target_service.region_detail(db, target_service.get_batch(db, batch_id), region)


def generate(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    batch = target_service.generate(db, payload["month"], principal["email"])
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="target.generate",
        entity="target_batch",
        entity_id=batch.id,
        detail={
            "batch_no": batch.batch_no,
            "month": batch.month.isoformat()[:7],
            "version": batch.version,
            "lines": batch.line_count,
            "total": batch.total_target,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return target_service.batch_detail(db, batch)


def publish(db: Session, batch_id: int, principal: dict, request: Request) -> dict:
    batch = target_service.get_batch(db, batch_id)
    target_service.publish(db, batch, principal["email"])
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="target.publish",
        entity="target_batch",
        entity_id=batch.id,
        detail={"batch_no": batch.batch_no, "status": batch.status, "version": batch.version},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return target_service.batch_detail(db, batch)


def list_store_targets(db: Session, **filters) -> dict:
    return target_service.list_store_targets(db, **filters)


def upsert_store_target(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    row, store = target_service.upsert_store_target(db, payload, principal["email"])
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="target.upsert",
        entity="store_month_target",
        entity_id=row.id,
        detail={"store": store.code, "month": row.month.isoformat()[:7], "target_amount": row.target_amount},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return target_service.serialize_store_target(row, store)
