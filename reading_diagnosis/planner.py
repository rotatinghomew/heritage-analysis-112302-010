"""补扫规划与影响台账。

调度边界（协议 + 题目约束）：
- 装订册（edition）是调度单元，开合额度按物件（work）逐项计；
- 一册补扫可同时兑现多个研究组合；若另一涉及作品仍受限，则组合无法兑现；
- 开合承受次数、馆点工时、近期开卷锁定借阅、禁止跨许可转交均为外部输入；
  缺报时结论为 indeterminate，绝不把缺报当作额度为 0；
- 临时收紧立即停止后续配送；已交付副本按原授权留痕（usable_as_agreed）；
- 替代合集只有在题录链接 + comparable 质量同时满足时才承担对照，
  不同研究组共享一本替代合集不等于都具备原件比较条件。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from .models import Event, EventAssessment, ScanRecord


def _eligible_scan(records: list[ScanRecord], as_of: date) -> Optional[ScanRecord]:
    """评估时点是否存在授权区间覆盖的 comparable 拍摄件。"""
    for record in records:
        if record.quality != "comparable":
            continue
        if record.starts is None or record.ends is None:
            continue
        if record.starts <= as_of <= record.ends:
            return record
    return None


def _scan_gaps(work: str, records: list[ScanRecord], as_of: date) -> list[str]:
    if not records:
        return [f"{work} 无任何拍摄件（未数字化，属缺报）"]
    gaps: list[str] = []
    if not any(r.quality == "comparable" for r in records):
        qualities = sorted({r.quality for r in records})
        gaps.append(f"{work} 现有拍摄质量 {qualities} 未达 comparable")
    covered = [
        r
        for r in records
        if r.starts is not None and r.ends is not None
        and r.starts <= as_of <= r.ends
    ]
    if not covered:
        gaps.append(f"{work} 授权区间在 {as_of} 已失效或区间缺报")
    return gaps


def _hard_restricted(work: str, scans_by_work: dict[str, list[ScanRecord]]) -> bool:
    """常设 restricted 且不存在更宽松许可：补扫无法解除，须走授权路径。"""
    records = scans_by_work.get(work, [])
    if not records:
        return False
    return all(r.permission == "restricted" for r in records) and any(
        r.permission == "restricted" for r in records
    )


def build_rescan_candidates(
    events: list[Event],
    scans: list[ScanRecord],
    pair_summaries: dict[str, dict],
    as_of: date,
) -> list[dict]:
    scans_by_work: dict[str, list[ScanRecord]] = defaultdict(list)
    for record in scans:
        scans_by_work[record.work].append(record)

    edition_works: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if event.edition and event.work:
            edition_works[event.edition].add(event.work)

    candidates: list[dict] = []
    for edition in sorted(edition_works):
        works = tuple(sorted(edition_works[edition]))
        gap_reasons: list[str] = []
        restrictions: list[str] = []
        for work in works:
            gap_reasons.extend(_scan_gaps(work, scans_by_work[work], as_of))
            if _hard_restricted(work, scans_by_work):
                restrictions.append(
                    f"{work} 常设 restricted：补扫不解除限制，需授权/政策动作"
                )
        if not gap_reasons:
            # 无扫描质量缺口的装订册不进入补扫清单（限制问题走授权路径）
            continue

        newly_realizable: list[dict] = []
        remains_blocked: list[dict] = []
        no_gain: list[str] = []
        touched_pairs = [
            summary
            for summary in pair_summaries.values()
            if set(summary["works"]) & set(works)
        ]
        for summary in touched_pairs:
            pair = summary["pair"]
            other_works = [w for w in summary["works"] if w not in works]
            blocker = next(
                (w for w in other_works if _hard_restricted(w, scans_by_work)),
                None,
            )
            if blocker is not None:
                remains_blocked.append(
                    {
                        "pair": pair,
                        "reason": f"另一涉及作品 {blocker} 常设受限，本册补扫后仍无法兑现完整组合",
                        "current_classification": summary["classification"],
                    }
                )
                continue
            if summary["classification"] == "supported" and summary["effective_sessions"]:
                no_gain.append(pair)
                continue
            # 补扫后可新增的比较机会；交付方式按常设许可给出
            delivery = "onsite_supervised" if _needs_onsite(summary, scans_by_work) else "standard_digital"
            newly_realizable.append(
                {
                    "pair": pair,
                    "delivery": delivery,
                    "current_classification": summary["classification"],
                    "note": "补扫达 comparable 且授权区间续期后方可成立",
                }
            )

        candidates.append(
            {
                "edition": edition,
                "works": works,
                "gap_reasons": tuple(gap_reasons),
                "restrictions": tuple(restrictions),
                "newly_realizable": tuple(newly_realizable),
                "remains_blocked": tuple(remains_blocked),
                "no_gain_pairs": tuple(no_gain),
                "realizable_delta": len(newly_realizable),
                "blocked_count": len(remains_blocked),
            }
        )
    return candidates


def _needs_onsite(
    summary: dict, scans_by_work: dict[str, list[ScanRecord]]
) -> bool:
    for work in summary["works"]:
        if any(r.permission == "onsite" for r in scans_by_work.get(work, [])):
            return True
    return False


def evaluate_constraints(
    constraints_rows: list[dict[str, str]],
    candidates: list[dict],
) -> dict:
    """开合额度/工时/锁定借阅/跨许可转交的外部约束检查。

    期望可选输入 constraints.csv 字段：
    venue, edition, open_close_budget, labor_hours, locked_until, license_zone
    缺报时返回 indeterminate 并列出所需字段，而非默认额度为 0。
    """
    if not constraints_rows:
        return {
            "status": "indeterminate",
            "reason": "约束输入缺报：未提供开合承受次数、馆点工时、锁定借阅与许可区域",
            "required_fields": [
                "venue",
                "edition",
                "open_close_budget",
                "labor_hours",
                "locked_until",
                "license_zone",
            ],
            "scheduling_decision": "不得在缺报情况下锁定排期；补扫候选仅按机会增量排序",
        }

    decisions: list[dict] = []
    by_edition = {row.get("edition"): row for row in constraints_rows}
    for candidate in candidates:
        row = by_edition.get(candidate["edition"])
        if row is None:
            decisions.append(
                {"edition": candidate["edition"], "decision": "indeterminate",
                 "reason": "该装订册约束记录缺报"}
            )
            continue
        decisions.append(
            {
                "edition": candidate["edition"],
                "venue": row.get("venue"),
                "open_close_budget": row.get("open_close_budget"),
                "labor_hours": row.get("labor_hours"),
                "locked_until": row.get("locked_until"),
                "license_zone": row.get("license_zone"),
                "decision": "evaluated",
                "note": "禁止跨 license_zone 转交；锁定借阅期内不得排期；"
                        "开合按册内 work 逐项累计，不得超 open_close_budget",
            }
        )
    return {"status": "evaluated", "decisions": decisions}


def current_fulfillable_pairs(
    pair_summaries: dict[str, dict],
    scans: list[ScanRecord],
    as_of: date,
) -> dict[str, dict]:
    """当前时点即可兑现的组合：双方都有区间覆盖的 comparable 拍摄件，
    且不存在常设硬限制（restricted 仅对已获临时授权并交付的会话留痕有效）。"""
    scans_by_work: dict[str, list[ScanRecord]] = defaultdict(list)
    for record in scans:
        scans_by_work[record.work].append(record)

    result: dict[str, dict] = {}
    for pair, summary in pair_summaries.items():
        if summary["classification"] != "supported":
            continue
        blocked_by = [
            work
            for work in summary["works"]
            if _hard_restricted(work, scans_by_work)
        ]
        result[pair] = {
            "pair": pair,
            "fulfillable_now": not blocked_by,
            "blocked_by": blocked_by,
            "delivered_sessions": summary["effective_sessions"],
            "temporary_grant": summary["temporary_grant"],
            "note": "已交付副本按原授权留痕；新请求在硬限制解除前停止配送"
            if blocked_by
            else "常设材料即可支撑",
        }
    return result


def build_impact_ledger(
    pair_summaries: dict[str, dict],
    assessments: dict[str, EventAssessment],
    events: list[Event],
    scenarios: list[dict],
) -> list[dict]:
    """许可收紧 / 新扫件不达标时，逐研究组给出处置台账。

    scenario: {"scenario": ..., "affected_work": ..., "kind": "permission"|"quality"}
    处置三态：
    - usable_as_agreed   ：该组此前已获交付（有效 compare 会话），按原授权留痕；
    - alternative_delivery：可转馆内监督阅览等替代提供方式；
    - postponed          ：既无交付留痕也无替代路径，推迟。
    """
    # 读者 -> pair -> 是否已交付
    delivered: dict[str, set[str]] = defaultdict(set)
    pair_readers: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if event.reader is None:
            continue
        assessment = assessments[event.event_id]
        for work_a, work_b in _combos(assessment.material_set):
            pair = "|".join(sorted((work_a, work_b)))
            pair_readers[pair].add(event.reader)
            if assessment.effective:
                delivered[event.reader].add(pair)

    ledger: list[dict] = []
    for scenario in scenarios:
        affected = scenario["affected_work"]
        kind = scenario["kind"]
        for pair, summary in pair_summaries.items():
            if affected not in summary["works"]:
                continue
            for group in sorted(pair_readers.get(pair, set())) or ["(无登记研究组)"]:
                was_delivered = pair in delivered.get(group, set())
                if was_delivered:
                    status = "usable_as_agreed"
                    basis = "已交付副本按原授权留痕管理，收紧不追溯"
                elif kind == "permission" and _onsite_path_exists(summary):
                    status = "alternative_delivery"
                    basis = "常设馆内许可存在，改馆内监督阅览交付"
                elif kind == "quality":
                    status = "postponed"
                    basis = "新扫件未达 comparable，对照质量不成立，推迟至补扫合格"
                else:
                    status = "postponed"
                    basis = "无交付留痕且无替代提供方式，停止后续配送"
                ledger.append(
                    {
                        "scenario": scenario["scenario"],
                        "pair": pair,
                        "research_group": group,
                        "status": status,
                        "basis": basis,
                    }
                )
    return ledger


def _combos(material: tuple[str, ...]):
    works = sorted(dict.fromkeys(material))
    for i in range(len(works)):
        for j in range(i + 1, len(works)):
            yield works[i], works[j]


def _onsite_path_exists(summary: dict) -> bool:
    # 题录中登记了馆内可达路径（替代件名称或 note 中的 onsite 标记），
    # 无登记则不得假设替代交付路径存在。
    for links in summary.get("substitute_links", {}).values():
        for link in links:
            if isinstance(link, str) and "onsite" in link:
                return True
    return False


def horizon_risks(
    scans: list[ScanRecord],
    pair_summaries: dict[str, dict],
    as_of: date,
) -> list[str]:
    """时间窗风险：将临期授权与机会窗口显式列给馆员。"""
    risks: list[str] = []
    for record in scans:
        if record.ends is not None and record.starts is not None:
            if record.starts <= as_of <= record.ends:
                days = (record.ends - as_of).days
                if days <= 60:
                    risks.append(
                        f"{record.work}/{record.scan} 授权将于 {record.ends} 到期"
                        f"（剩余 {days} 天），依赖它的组合需先续期"
                    )
    for pair, summary in pair_summaries.items():
        if summary["requires_business_justification"]:
            risks.append(summary["requires_business_justification"])
    return risks
