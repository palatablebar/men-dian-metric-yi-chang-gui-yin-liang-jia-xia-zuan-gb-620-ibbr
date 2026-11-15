"""区域月目标分期拆分（算法 C）单测：归属区间、按日拆分、三层对平、确定性。

纯函数测试，不依赖数据库；与 test_detection.py / test_decompose.py 同级。
"""
from __future__ import annotations

from datetime import date

from src.services import target_split

MONTH = "2026-11"
M_START, M_END, M_DAYS = target_split.month_bounds(MONTH)


def transfer(tid: int, effective: str, from_region: str, to_region: str) -> dict:
    return {
        "id": tid,
        "effective_date": date.fromisoformat(effective),
        "from_region": from_region,
        "to_region": to_region,
        "reason": "",
    }


def test_month_bounds():
    start, end, days = target_split.month_bounds("2026-11")
    assert (start, end, days) == (date(2026, 11, 1), date(2026, 11, 30), 30)
    start, end, days = target_split.month_bounds("2026-02")
    assert (start, end, days) == (date(2026, 2, 1), date(2026, 2, 28), 28)
    start, end, days = target_split.month_bounds("2026-12")
    assert (start, end, days) == (date(2026, 12, 1), date(2026, 12, 31), 31)


def test_month_bounds_invalid():
    for bad in ("2026-13", "2026-1", "202611", "abcd-ef", ""):
        try:
            target_split.month_bounds(bad)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝非法月份：{bad!r}")


def test_no_transfer_single_segment_current_region():
    segments = target_split.affiliation_intervals("华东", [], M_START, M_END)
    assert len(segments) == 1
    assert segments[0]["region"] == "华东"
    assert segments[0]["days"] == 30
    assert segments[0]["transfer_id"] is None


def test_mid_month_transfer_splits_at_effective_date():
    transfers = [transfer(1, "2026-11-10", "华东", "华南")]
    segments = target_split.affiliation_intervals("华南", transfers, M_START, M_END)
    assert [seg["region"] for seg in segments] == ["华东", "华南"]
    assert segments[0]["start"] == date(2026, 11, 1)
    assert segments[0]["end"] == date(2026, 11, 9)
    assert segments[0]["days"] == 9
    assert segments[1]["start"] == date(2026, 11, 10)
    assert segments[1]["end"] == date(2026, 11, 30)
    assert segments[1]["days"] == 21
    assert segments[1]["transfer_id"] == 1
    assert sum(seg["days"] for seg in segments) == 30


def test_transfer_not_yet_effective_stays_in_origin_region():
    transfers = [transfer(1, "2026-11-20", "西南", "华中")]
    # 对 11 月整月生成时，11-20 仍在月内 → 会拆分；对 10 月则完全不影响
    october = target_split.month_bounds("2026-10")
    segments = target_split.affiliation_intervals("西南", transfers, october[0], october[1])
    assert len(segments) == 1
    assert segments[0]["region"] == "西南"
    segments = target_split.affiliation_intervals("西南", transfers, M_START, M_END)
    assert [seg["region"] for seg in segments] == ["西南", "华中"]
    assert segments[0]["days"] == 19
    assert segments[1]["days"] == 11


def test_transfer_effective_on_month_start_no_split():
    transfers = [transfer(1, "2026-11-01", "华中", "华东")]
    segments = target_split.affiliation_intervals("华东", transfers, M_START, M_END)
    assert len(segments) == 1
    assert segments[0]["region"] == "华东"
    assert segments[0]["transfer_id"] == 1


def test_transfer_before_month_carries_new_region():
    transfers = [transfer(1, "2026-10-15", "华中", "华东")]
    segments = target_split.affiliation_intervals("华东", transfers, M_START, M_END)
    assert len(segments) == 1
    assert segments[0]["region"] == "华东"
    assert segments[0]["transfer_id"] == 1


def test_multiple_transfers_chain():
    transfers = [
        transfer(2, "2026-11-20", "华南", "西南"),
        transfer(1, "2026-11-10", "华东", "华南"),
    ]
    segments = target_split.affiliation_intervals("西南", transfers, M_START, M_END)
    assert [seg["region"] for seg in segments] == ["华东", "华南", "西南"]
    assert [seg["days"] for seg in segments] == [9, 10, 11]
    assert [seg["transfer_id"] for seg in segments] == [None, 1, 2]


def test_split_target_sums_exactly_to_store_target():
    transfers = [transfer(1, "2026-11-10", "华东", "华南")]
    segments = target_split.affiliation_intervals("华南", transfers, M_START, M_END)
    # 故意取不能被 30 整除的分数：1,000,001 分
    result = target_split.split_target_cents(1_000_001, segments, M_DAYS)
    assert sum(seg["target_cents"] for seg in result) == 1_000_001
    assert result[0]["target_cents"] == int(1_000_001 * 9 / 30 + 0.5)
    assert result[-1]["target_cents"] == 1_000_001 - result[0]["target_cents"]


def test_split_single_segment_equals_target():
    segments = target_split.affiliation_intervals("华东", [], M_START, M_END)
    result = target_split.split_target_cents(987_654_321, segments, M_DAYS)
    assert len(result) == 1
    assert result[0]["target_cents"] == 987_654_321


def _demo_plan():
    stores = [
        {"id": 1, "code": "LB-HD-001", "name": "外滩店", "region": "华东"},
        {"id": 2, "code": "LB-HD-003", "name": "南京店", "region": "华南"},  # 11-10 华东→华南（已生效）
        {"id": 3, "code": "LB-XN-002", "name": "重庆店", "region": "西南"},  # 11-20 西南→华中（未生效仍计原区域）
    ]
    targets = {1: 30_000_000, 2: 20_000_000, 3: 10_000_000}
    transfers = {
        2: [transfer(11, "2026-11-10", "华东", "华南")],
        3: [transfer(12, "2026-11-20", "西南", "华中")],
    }
    return target_split.build_batch_plan(MONTH, stores, targets, transfers)


def test_batch_plan_reconciles_group_region_store():
    plan = _demo_plan()
    reconcile = target_split.reconcile_plan(plan)
    assert reconcile["balanced"] is True
    assert reconcile["group_cents"] == 60_000_000
    assert reconcile["diff_region_cents"] == 0
    assert reconcile["diff_store_cents"] == 0
    # 区域合计：华东 = 店1 全月 + 店2 前 9 天；华南 = 店2 后 22 天；西南 = 店3 前 19 天；华中 = 店3 后 11 天
    assert plan["region_totals"]["华东"] == 30_000_000 + int(20_000_000 * 9 / 30 + 0.5)
    assert plan["region_totals"]["华南"] == 20_000_000 - int(20_000_000 * 9 / 30 + 0.5)
    assert plan["region_totals"]["西南"] + plan["region_totals"]["华中"] == 10_000_000
    # 门店层：Σ分段 = 门店月目标
    for store_id, cents in plan["store_totals"].items():
        assert sum(line["target_cents"] for line in plan["lines"] if line["store_id"] == store_id) == cents


def test_batch_plan_skips_stores_without_target():
    stores = [{"id": 1, "code": "A", "name": "甲", "region": "华东"}, {"id": 2, "code": "B", "name": "乙", "region": "华北"}]
    plan = target_split.build_batch_plan(MONTH, stores, {1: 100}, {})
    assert {line["store_id"] for line in plan["lines"]} == {1}
    assert plan["group_cents"] == 100


def test_transfer_impacts_explain_moves():
    plan = _demo_plan()
    transfers = {
        2: [transfer(11, "2026-11-10", "华东", "华南")],
        3: [transfer(12, "2026-11-20", "西南", "华中")],
    }
    impacts = target_split.transfer_impacts(plan, transfers)
    assert [impact["store_code"] for impact in impacts] == ["LB-HD-003", "LB-XN-002"]
    first = impacts[0]
    assert first["transitions"][0]["from_region"] == "华东"
    assert first["transitions"][0]["to_region"] == "华南"
    assert first["transitions"][0]["effective_date"] == date(2026, 11, 10)
    assert [seg["region"] for seg in first["segments"]] == ["华东", "华南"]
    assert first["total_cents"] == 20_000_000
    # 未跨区的门店不出现在影响清单
    assert all(impact["store_code"] != "LB-HD-001" for impact in impacts)


def test_deterministic_output():
    first = _demo_plan()
    second = _demo_plan()
    assert first["lines"] == second["lines"]
    assert first["region_totals"] == second["region_totals"]
    assert target_split.reconcile_plan(first) == target_split.reconcile_plan(second)
