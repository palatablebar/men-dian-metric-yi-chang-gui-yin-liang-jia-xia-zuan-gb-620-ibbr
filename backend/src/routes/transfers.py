from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import READ_ROLES
from src.middlewares.rbac import require_roles
from src.services import transfer_service

router = APIRouter(tags=["transfers"])


@router.get("/transfers")
def list_transfers(
    store_id: int | None = None,
    month: str | None = None,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return transfer_service.list_transfers(db, store_id=store_id, month=month)
