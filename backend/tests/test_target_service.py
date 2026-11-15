"""目标批次服务级测试（SQLite 内存库）：幂等重跑、已发布版不可改换、中断恢复。

纯算法口径见 test_target_split.py；本文件验证事务与恢复约定。
运行：docker compose exec -T backend python -m pytest tests/test_target_service.py -q
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.models import Base, Store, StoreMonthTarget, StoreRegionTransfer, TargetBatch, TargetLine
from src.services import target_service, transfer_service
from src.utils.errors import AppError

OPERATOR = "admin@anomalyattr.io"
MONTH = "2026-11"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def make_store(db: Session, code: str, region: str) -> Store:
    store = Store(code=code, name=f"蓝湾·{code}店", region=region, city="上海", room_count=200)
    db.add(store)
    db.flush()
    return store


def seed_stores(db: Session) -> dict[str, Store]:
    stores = {
        "A": make_store(db, "LB-TA-001", "华东"),
        "B": make_store(db, "LB-TA-002", "华南"),
    }
    db.commit()
    return stores


def set_target(db: Session, store: Store, amount: float, month: str = MONTH) -> None:
    target_service.upsert_store_target(
        db, {"store_code": store.code, "month": month, "target_amount": amount}, OPERATOR
    )
    db.commit()


def lines_of(db: Session, batch_id: int) -> list[TargetLine]:
    return db.scalars(
        select(TargetLine)
        .where(TargetLine.batch_id == batch_id)
        .order_by(TargetLine.store_id, TargetLine.segment_no)
    ).all()


def test_generate_then_rerun_is_idempotent(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    set_target(db, stores["B"], 2_000_000)

    first = target_service.generate(db, MONTH, OPERATOR)
    first_lines = [(line.store_id, line.segment_no, line.target_cents) for line in lines_of(db, first.id)]
    second = target_service.generate(db, MONTH, OPERATOR)

    assert second.id == first.id  # 同一草稿批次原地重跑，不新增版本
    assert second.version == 1
    assert db.scalar(select(func.count()).select_from(TargetBatch)) == 1
    assert db.scalar(select(func.count()).select_from(TargetLine)) == len(first_lines)  # 没有重复行
    second_lines = [(line.store_id, line.segment_no, line.target_cents) for line in lines_of(db, second.id)]
    assert sorted(second_lines) == sorted(first_lines)
    assert second.metrics["reconcile"]["balanced"] is True


def test_rerun_after_publish_keeps_published_version_untouched(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    set_target(db, stores["B"], 2_000_000)

    v1 = target_service.generate(db, MONTH, OPERATOR)
    target_service.publish(db, v1, OPERATOR)
    published_snapshot = [(line.store_id, line.segment_no, line.region, line.target_cents) for line in lines_of(db, v1.id)]

    # 发布后调整一笔跨区 + 改目标，再重跑：应生成 v2 草稿，v1 内容不变
    transfer_service.record_transfer(
        db, stores["A"], {"to_region": "华南", "effective_date": "2026-11-10", "reason": "测试"}, OPERATOR,
        today=date(2026, 11, 14),
    )
    db.commit()
    set_target(db, stores["B"], 2_600_000)
    v2 = target_service.generate(db, MONTH, OPERATOR)

    assert v2.id != v1.id
    assert v2.version == 2 and v2.status == "draft"
    db.refresh(v1)
    assert v1.status == "published"  # 已发布版不被重跑改换
    after = [(line.store_id, line.segment_no, line.region, line.target_cents) for line in lines_of(db, v1.id)]
    assert after == published_snapshot
    # v2 反映新口径：A 店拆成华东/华南两段，B 店目标更新
    a_lines = [line for line in lines_of(db, v2.id) if line.store_id == stores["A"].id]
    assert [line.region for line in sorted(a_lines, key=lambda line: line.segment_no)] == ["华东", "华南"]
    assert sum(line.target_cents for line in a_lines) == 300_000_000
    b_lines = [line for line in lines_of(db, v2.id) if line.store_id == stores["B"].id]
    assert sum(line.target_cents for line in b_lines) == 260_000_000
    # 发布 v2 后 v1 归档、内容仍可查
    target_service.publish(db, v2, OPERATOR)
    db.refresh(v1)
    assert v1.status == "archived"
    assert v2.status == "published"
    assert [(line.store_id, line.segment_no) for line in lines_of(db, v1.id)] == [
        (line[0], line[1]) for line in published_snapshot
    ]


def test_interrupted_generation_recovers_old_draft(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    batch = target_service.generate(db, MONTH, OPERATOR)
    old_lines = [(line.store_id, line.segment_no, line.target_cents) for line in lines_of(db, batch.id)]

    # 模拟生成中断：批次停在 running，写入事务未提交（旧行还在）
    batch.prev_status = "draft"
    batch.status = "running"
    db.commit()
    set_target(db, stores["B"], 2_000_000)

    rerun = target_service.generate(db, MONTH, OPERATOR)  # 先恢复旧版再续跑
    assert rerun.id == batch.id
    assert rerun.status == "draft"
    assert db.scalar(select(func.count()).select_from(TargetBatch)) == 1
    new_lines = lines_of(db, batch.id)
    assert len(new_lines) == 2  # B 店目标已纳入，且没有重复行
    assert sum(line.target_cents for line in new_lines) == 500_000_000
    assert sorted(old_lines) != sorted((line.store_id, line.segment_no, line.target_cents) for line in new_lines)


def test_interrupted_fresh_batch_is_removed_before_rerun(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    # 模拟首次生成即中断：只留下 running 标记，没有任何已提交分期行
    stuck = TargetBatch(
        batch_no="TB202611-0001", month=date(2026, 11, 1), version=1,
        status="running", prev_status=None, operator_email=OPERATOR,
    )
    db.add(stuck)
    db.commit()

    batch = target_service.generate(db, MONTH, OPERATOR)
    assert batch.status == "draft"
    batches = db.scalars(select(TargetBatch)).all()
    assert len(batches) == 1  # 中断的全新批次被清掉，没有残留
    assert batches[0].id == batch.id
    assert len(lines_of(db, batch.id)) == 1


def test_interrupted_publish_keeps_previous_published_version(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    v1 = target_service.generate(db, MONTH, OPERATOR)
    target_service.publish(db, v1, OPERATOR)
    set_target(db, stores["B"], 2_000_000)
    v2 = target_service.generate(db, MONTH, OPERATOR)

    # 模拟发布中断：v2 停在 publishing，归档/发布事务未提交
    v2.prev_status = "draft"
    v2.status = "publishing"
    db.commit()

    target_service.publish(db, v2, OPERATOR)  # 先恢复（v2 回草稿、v1 仍为已发布）再续跑
    db.refresh(v1)
    db.refresh(v2)
    assert v2.status == "published"
    assert v1.status == "archived"
    assert db.scalar(select(func.count()).select_from(TargetBatch)) == 2


def test_publish_is_idempotent_and_rejects_non_draft(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    batch = target_service.generate(db, MONTH, OPERATOR)
    target_service.publish(db, batch, OPERATOR)
    again = target_service.publish(db, batch, OPERATOR)  # 重复发布不改动
    assert again.status == "published"
    assert db.scalar(select(func.count()).select_from(TargetBatch)) == 1
    # 归档版本不能再发布
    set_target(db, stores["B"], 2_000_000)
    v2 = target_service.generate(db, MONTH, OPERATOR)
    target_service.publish(db, v2, OPERATOR)
    db.refresh(batch)
    assert batch.status == "archived"
    with pytest.raises(AppError) as exc:
        target_service.publish(db, batch, OPERATOR)
    assert exc.value.status_code == 409


def test_generate_without_targets_422_and_no_residue(db):
    seed_stores(db)
    with pytest.raises(AppError) as exc:
        target_service.generate(db, MONTH, OPERATOR)
    assert exc.value.status_code == 422
    assert db.scalar(select(func.count()).select_from(TargetBatch)) == 0  # 不残留批次


def test_store_target_upsert_keeps_single_row(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    set_target(db, stores["A"], 3_300_000)
    rows = db.scalars(select(StoreMonthTarget)).all()
    assert len(rows) == 1
    assert rows[0].target_amount == 3_300_000


def test_record_transfer_updates_region_and_validates(db):
    stores = seed_stores(db)
    transfer = transfer_service.record_transfer(
        db, stores["A"], {"to_region": "华南", "effective_date": "2026-11-10"}, OPERATOR,
        today=date(2026, 11, 14),
    )
    assert transfer.from_region == "华东"
    assert stores["A"].region == "华南"  # 已生效 → 当前归属回写
    # 未生效的调整不回写当前归属
    transfer_service.record_transfer(
        db, stores["B"], {"to_region": "华北", "effective_date": "2026-12-01"}, OPERATOR,
        today=date(2026, 11, 14),
    )
    assert stores["B"].region == "华南"
    # 同一生效日重复登记 → 409；目标区域与当时归属相同 → 422
    with pytest.raises(AppError) as conflict:
        transfer_service.record_transfer(
            db, stores["A"], {"to_region": "西南", "effective_date": "2026-11-10"}, OPERATOR,
            today=date(2026, 11, 14),
        )
    assert conflict.value.status_code == 409
    with pytest.raises(AppError) as invalid:
        transfer_service.record_transfer(
            db, stores["A"], {"to_region": "华南", "effective_date": "2026-11-20"}, OPERATOR,
            today=date(2026, 11, 14),
        )
    assert invalid.value.status_code == 422
    assert db.scalar(select(func.count()).select_from(StoreRegionTransfer)) == 2


def test_batch_detail_reconciles_three_layers(db):
    stores = seed_stores(db)
    set_target(db, stores["A"], 3_000_000)
    set_target(db, stores["B"], 2_000_000)
    transfer_service.record_transfer(
        db, stores["A"], {"to_region": "华南", "effective_date": "2026-11-10", "reason": "片区重组"}, OPERATOR,
        today=date(2026, 11, 14),
    )
    db.commit()
    batch = target_service.generate(db, MONTH, OPERATOR)
    detail = target_service.batch_detail(db, batch)
    assert detail["reconcile"]["balanced"] is True
    assert detail["reconcile"]["group_total"] == 5_000_000
    assert detail["reconcile"]["region_sum"] == detail["reconcile"]["store_sum"] == 5_000_000
    assert len(detail["transfers"]) == 1
    assert detail["transfers"][0]["store_code"] == "LB-TA-001"
    assert "华东" in detail["transfers"][0]["note"] and "华南" in detail["transfers"][0]["note"]
    regions = {item["region"]: item for item in detail["regions"]}
    assert regions["华东"]["transfer_out"] == 1
    assert regions["华南"]["transfer_in"] == 1
    # 区域明细：分期行 + 门店贡献 + 影响说明
    south = target_service.region_detail(db, batch, "华南")
    assert south["summary"]["store_count"] == 2  # B 全月 + A 后段
    assert any(seg["store_code"] == "LB-TA-001" for seg in south["segments"])
    assert south["impacts"][0]["directions"] == ["in"]
    east = target_service.region_detail(db, batch, "华东")
    assert east["impacts"][0]["directions"] == ["out"]
    assert east["summary"]["target"] + south["summary"]["target"] == 5_000_000
