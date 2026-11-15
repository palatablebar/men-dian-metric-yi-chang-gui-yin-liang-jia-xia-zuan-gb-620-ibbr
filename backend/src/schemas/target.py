from __future__ import annotations

from pydantic import BaseModel, Field

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class TargetBatchIn(BaseModel):
    month: str = Field(pattern=MONTH_PATTERN)


class StoreTargetIn(BaseModel):
    store_code: str = Field(min_length=2, max_length=40)
    month: str = Field(pattern=MONTH_PATTERN)
    target_amount: float = Field(gt=0, le=1_000_000_000_000)


class TransferIn(BaseModel):
    to_region: str = Field(min_length=2, max_length=40)
    effective_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str | None = Field(default=None, max_length=500)
