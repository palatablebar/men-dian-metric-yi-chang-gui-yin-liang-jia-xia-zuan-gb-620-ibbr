from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import READ_ROLES, TARGET_WRITE_ROLES
from src.controllers import target_controller
from src.middlewares.rbac import require_roles
from src.schemas.target import StoreTargetIn, TargetBatchIn
from src.utils.pagination import page_params

router = APIRouter(tags=["targets"])


@router.get("/target-batches")
def list_batches(
    month: str | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return target_controller.list_batches(db, month=month, limit=page.limit, offset=page.offset)


@router.post("/target-batches", status_code=201)
def generate_batch(
    payload: TargetBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*TARGET_WRITE_ROLES)),
) -> dict:
    return target_controller.generate(db, payload.model_dump(), principal, request)


@router.get("/target-batches/{batch_id}")
def get_batch(
    batch_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return target_controller.get_batch(db, batch_id)


@router.get("/target-batches/{batch_id}/regions/{region}")
def get_region(
    batch_id: int,
    region: str,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return target_controller.get_region(db, batch_id, region)


@router.post("/target-batches/{batch_id}/publish")
def publish_batch(
    batch_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*TARGET_WRITE_ROLES)),
) -> dict:
    return target_controller.publish(db, batch_id, principal, request)


@router.get("/targets")
def list_store_targets(
    month: str | None = None,
    region: str | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return target_controller.list_store_targets(db, month=month, region=region)


@router.put("/targets")
def upsert_store_target(
    payload: StoreTargetIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*TARGET_WRITE_ROLES)),
) -> dict:
    return target_controller.upsert_store_target(db, payload.model_dump(), principal, request)
