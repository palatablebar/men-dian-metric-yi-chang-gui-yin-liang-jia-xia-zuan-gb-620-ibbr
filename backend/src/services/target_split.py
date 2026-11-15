"""区域月目标分期拆分（纯函数、可单测；不依赖数据库与第三方库）。

口径：
- 归属区间：调整生效日当天起计入新区域，生效前仍计入原区域（含生效日前一日）；
- 拆分：门店月目标按区间天数比例拆到各区域，金额以「分」计算，
  前 N-1 段四舍五入到分，最后一段吸收舍入残差 → Σ分段 ≡ 门店月目标；
- 对平：全集团合计 = Σ区域合计 = Σ门店合计（同一批分期行聚合，天然一致）；
- 确定性：同输入两次输出逐字段一致。
"""
from __future__ import annotations

from datetime import date, timedelta


def month_bounds(month: str) -> tuple[date, date, int]:
    """'2026-11' -> (2026-11-01, 2026-11-30, 30)。非法格式抛 ValueError。"""
    if not month or len(month) != 7 or month[4] != "-":
        raise ValueError(f"月份格式应为 YYYY-MM：{month!r}")
    year, mon = int(month[:4]), int(month[5:7])
    if not 1 <= mon <= 12:
        raise ValueError(f"月份超出范围：{month!r}")
    start = date(year, mon, 1)
    if mon == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, mon + 1, 1) - timedelta(days=1)
    return start, end, (end - start).days + 1


def region_at(current_region: str, transfers: list[dict], day: date) -> str:
    """某天所属区域：最后一笔 effective_date <= day 的 to_region；

    没有任何已生效调整时，取最早一笔调整的 from_region（调整前的原始归属）；
    没有任何调整记录时取门店当前区域。
    """
    ordered = sorted(transfers, key=lambda item: (item["effective_date"], item.get("id") or 0))
    if not ordered:
        return current_region
    region = ordered[0]["from_region"]
    for transfer in ordered:
        if transfer["effective_date"] <= day:
            region = transfer["to_region"]
    return region


def affiliation_intervals(
    current_region: str,
    transfers: list[dict],
    month_start: date,
    month_end: date,
) -> list[dict]:
    """把月份切成归属区间：[{region, start, end, days, transfer_id}]，覆盖整月、互不重叠。

    transfer_id 标在「因该笔调整而开始」的区间上（含月初前已生效的调整）；
    生效日 = 区间起点（生效当天计入新区域）。
    """
    ordered = sorted(transfers, key=lambda item: (item["effective_date"], item.get("id") or 0))
    region = region_at(current_region, ordered, month_start)
    # 月初归属若由某笔调整决定（含生效日 = 月初），首段标注该笔调整
    pending_transfer_id = None
    for transfer in ordered:
        if transfer["effective_date"] <= month_start and transfer["to_region"] == region:
            pending_transfer_id = transfer.get("id")
    segments: list[dict] = []
    cursor = month_start
    for transfer in ordered:
        effective = transfer["effective_date"]
        if effective <= month_start or effective > month_end:
            continue
        segments.append(
            {
                "region": region,
                "start": cursor,
                "end": effective - timedelta(days=1),
                "days": (effective - cursor).days,
                "transfer_id": pending_transfer_id,
            }
        )
        cursor = effective
        region = transfer["to_region"]
        pending_transfer_id = transfer.get("id")
    segments.append(
        {
            "region": region,
            "start": cursor,
            "end": month_end,
            "days": (month_end - cursor).days + 1,
            "transfer_id": pending_transfer_id,
        }
    )
    return segments


def split_target_cents(target_cents: int, segments: list[dict], month_days: int) -> list[dict]:
    """按区间天数比例拆分门店月目标（分）。最后一段吸收残差，保证 Σ = target_cents。"""
    if not segments:
        return []
    allocated = 0
    result: list[dict] = []
    for index, segment in enumerate(segments):
        if index < len(segments) - 1:
            cents = int(target_cents * segment["days"] / month_days + 0.5)
            allocated += cents
        else:
            cents = target_cents - allocated
        result.append({**segment, "segment_no": index + 1, "target_cents": cents})
    return result


def build_batch_plan(
    month: str,
    stores: list[dict],
    targets_cents: dict[int, int],
    transfers_by_store: dict[int, list[dict]],
) -> dict:
    """生成一个目标批次的完整计划（纯计算）：

    stores: [{id, code, name, region(当前归属)}]
    targets_cents: {store_id: 月目标（分）}，没有目标的门店不产生分期行
    transfers_by_store: {store_id: [{effective_date, from_region, to_region, id}]}
    """
    month_start, month_end, month_days = month_bounds(month)
    lines: list[dict] = []
    region_totals: dict[str, int] = {}
    store_totals: dict[int, int] = {}
    for store in sorted(stores, key=lambda item: item["code"]):
        target_cents = targets_cents.get(store["id"])
        if target_cents is None:
            continue
        segments = affiliation_intervals(
            store["region"], transfers_by_store.get(store["id"], []), month_start, month_end
        )
        for segment in split_target_cents(target_cents, segments, month_days):
            lines.append(
                {
                    "store_id": store["id"],
                    "store_code": store["code"],
                    "store_name": store["name"],
                    "segment_no": segment["segment_no"],
                    "region": segment["region"],
                    "period_start": segment["start"],
                    "period_end": segment["end"],
                    "days": segment["days"],
                    "target_cents": segment["target_cents"],
                    "transfer_id": segment["transfer_id"],
                }
            )
            region_totals[segment["region"]] = region_totals.get(segment["region"], 0) + segment["target_cents"]
        store_totals[store["id"]] = target_cents
    return {
        "month": month,
        "month_start": month_start,
        "month_end": month_end,
        "month_days": month_days,
        "lines": lines,
        "region_totals": region_totals,
        "store_totals": store_totals,
        "group_cents": sum(store_totals.values()),
    }


def reconcile_plan(plan: dict) -> dict:
    """三层对平：全集团 = Σ区域 = Σ门店。全部以分计，balanced 要求分毫不差。"""
    group_cents = plan["group_cents"]
    region_cents = sum(plan["region_totals"].values())
    store_cents = sum(plan["store_totals"].values())
    return {
        "group_cents": group_cents,
        "region_cents": region_cents,
        "store_cents": store_cents,
        "diff_region_cents": region_cents - group_cents,
        "diff_store_cents": store_cents - group_cents,
        "balanced": group_cents == region_cents == store_cents,
    }


def transfer_impacts(plan: dict, transfers_by_store: dict[int, list[dict]]) -> list[dict]:
    """从分期行还原跨区调整影响：一家门店在月内出现多段（或首段由调整决定）即有影响。"""
    lines_by_store: dict[int, list[dict]] = {}
    for line in plan["lines"]:
        lines_by_store.setdefault(line["store_id"], []).append(line)
    impacts: list[dict] = []
    for store_id, lines in sorted(lines_by_store.items(), key=lambda item: item[1][0]["store_code"]):
        lines = sorted(lines, key=lambda item: item["segment_no"])
        transfer_ids = {line["transfer_id"] for line in lines if line["transfer_id"] is not None}
        if len(lines) < 2 and not transfer_ids:
            continue
        transfers = {item.get("id"): item for item in transfers_by_store.get(store_id, [])}
        transitions: list[dict] = []
        for index, line in enumerate(lines):
            if line["transfer_id"] is None:
                continue
            transfer = transfers.get(line["transfer_id"], {})
            previous = lines[index - 1] if index > 0 else None
            transitions.append(
                {
                    "from_region": transfer.get("from_region") or (previous["region"] if previous else None),
                    "to_region": line["region"],
                    "effective_date": transfer.get("effective_date") or line["period_start"],
                    "reason": transfer.get("reason") or "",
                }
            )
        impacts.append(
            {
                "store_id": store_id,
                "store_code": lines[0]["store_code"],
                "store_name": lines[0]["store_name"],
                "transitions": transitions,
                "segments": [
                    {
                        "region": line["region"],
                        "period_start": line["period_start"],
                        "period_end": line["period_end"],
                        "days": line["days"],
                        "target_cents": line["target_cents"],
                    }
                    for line in lines
                ],
                "total_cents": sum(line["target_cents"] for line in lines),
            }
        )
    return impacts
