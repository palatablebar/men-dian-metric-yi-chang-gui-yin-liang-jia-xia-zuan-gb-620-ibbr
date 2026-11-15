"""区域月目标领域实体：门店月目标 / 跨区调整 / 目标批次 / 目标分期行。

口径：
- `StoreMonthTarget` 是输入（门店 × 月份 唯一），由管理员下达；
- `StoreRegionTransfer` 记录跨区调整，生效日当天起计入新区域；
- `TargetBatch` 是一个月的一次生成版本（month, version 唯一），
  running/publishing 为可恢复中间态，已发布版本不被重跑改换；
- `TargetLine` 是批次内的分期结果（batch_id, store_id, segment_no 唯一），
  金额以分（target_cents）为准，保证 全集团 = Σ区域 = Σ门店 逐层对平。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, BigInteger, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class StoreMonthTarget(Base):
    """门店月目标（输入）：自然键 (store_id, month)，month 统一存当月 1 日。"""

    __tablename__ = "store_month_targets"
    __table_args__ = (
        UniqueConstraint("store_id", "month", name="uq_store_month_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    month: Mapped[date] = mapped_column(Date, index=True)
    target_amount: Mapped[float] = mapped_column(Float, default=0.0)
    updated_by: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class StoreRegionTransfer(Base):
    """门店跨区调整：自然键 (store_id, effective_date)。生效日当天起计入新区域。"""

    __tablename__ = "store_region_transfers"
    __table_args__ = (
        UniqueConstraint("store_id", "effective_date", name="uq_transfer_store_effective"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    from_region: Mapped[str] = mapped_column(String(40))
    to_region: Mapped[str] = mapped_column(String(40))
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(160), default="")
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TargetBatch(Base):
    """目标批次：一个月的一个生成版本。自然键 (month, version)。

    中断恢复约定：生成/发布先把状态置为 running/publishing 并提交（prev_status
    记录原状态），随后在一个事务内完成全部写入。进程中断后批次停留在中间态，
    下次生成/发布前由 recover_interrupted 恢复旧版（或清掉没有内容的全新批次）。
    """

    __tablename__ = "target_batches"
    __table_args__ = (
        UniqueConstraint("month", "version", name="uq_target_batch_month_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    month: Mapped[date] = mapped_column(Date, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    prev_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    store_count: Mapped[int] = mapped_column(Integer, default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    total_target: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TargetLine(Base):
    """目标分期行：自然键 (batch_id, store_id, segment_no)。金额以 target_cents 为准。"""

    __tablename__ = "target_lines"
    __table_args__ = (
        UniqueConstraint("batch_id", "store_id", "segment_no", name="uq_target_line_segment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("target_batches.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    store_code: Mapped[str] = mapped_column(String(40))
    store_name: Mapped[str] = mapped_column(String(120))
    segment_no: Mapped[int] = mapped_column(Integer, default=1)
    region: Mapped[str] = mapped_column(String(40), index=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer, default=0)
    target_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    target_amount: Mapped[float] = mapped_column(Float, default=0.0)
    transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("store_region_transfers.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
