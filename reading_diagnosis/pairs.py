"""组合构建、去重、三分类与样本外检验。

去重规则（协议第 1、3 条）：
- 同一 session（本数据无研究任务字段，以会话代理任务）内同一 pair 只算一次观察，
  同一件拓片的多次拍摄、多次 open/compare 不重复加权；
- 装订册（edition）共现只作为来源标记，不自动加权。

三分类：
- supported   ：存在有效 compare 观察（授权 valid 且全部作品通过对照闸门）；
- spurious    ：没有任何有效观察，证据仅来自目录搜索曝光，或可见但对照质量/
                授权区间不达标——属于“能看到但比不了”的目录推荐型关联；
- inconclusive：存在受限/未兑现请求等需求信号，但材料从未获得观察机会，
                无证据可判真伪。
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Optional

from .models import (
    Event,
    EventAssessment,
    HoldoutFold,
    PairObservation,
    SubstituteLink,
)


def pair_key(works: tuple[str, ...] | list[str]) -> str:
    return "|".join(sorted(dict.fromkeys(works)))


def _pairs_in(material: tuple[str, ...]) -> list[str]:
    works = sorted(dict.fromkeys(material))
    return [pair_key(combo) for combo in combinations(works, 2)]


def _time_slice(occurred) -> str:
    if occurred is None:
        return "unknown"
    return occurred.strftime("%Y-%m")


def build_observations(
    events: list[Event],
    assessments: dict[str, EventAssessment],
) -> dict[tuple[str, str], PairObservation]:
    """按 (session, pair) 聚合；重复打开、重复拍摄在此折叠为一条。"""
    bucket: dict[tuple[str, str], dict] = {}

    def ensure(session: str, pair: str) -> dict:
        key = (session, pair)
        if key not in bucket:
            bucket[key] = {
                "pair": pair,
                "session": session,
                "readers": [],
                "venues": [],
                "time_slices": [],
                "editions": [],
                "visible_sets": [],
                "event_ids": [],
                "effective": False,
                "blocked": False,
                "temporary_grant": False,
                "gate_failures": set(),
            }
        return bucket[key]

    for event in events:
        assessment = assessments[event.event_id]
        session = event.session or "unknown-session"
        for pair in _pairs_in(assessment.material_set):
            entry = ensure(session, pair)
            if event.reader and event.reader not in entry["readers"]:
                entry["readers"].append(event.reader)
            if event.venue and event.venue not in entry["venues"]:
                entry["venues"].append(event.venue)
            slice_name = _time_slice(event.occurred)
            if slice_name not in entry["time_slices"]:
                entry["time_slices"].append(slice_name)
            if event.edition and event.edition not in entry["editions"]:
                entry["editions"].append(event.edition)
            if assessment.material_set and assessment.material_set not in entry["visible_sets"]:
                entry["visible_sets"].append(assessment.material_set)
            entry["event_ids"].append(event.event_id)
            entry["effective"] |= assessment.effective
            entry["blocked"] |= assessment.blocked
            entry["temporary_grant"] |= assessment.temporary_grant
            for gate in assessment.work_gates:
                if not gate.passes:
                    for reason in gate.reasons:
                        entry["gate_failures"].add(f"{gate.work}:{reason}")

    observations: dict[tuple[str, str], PairObservation] = {}
    for (session, _pair), entry in bucket.items():
        observations[(session, entry["pair"])] = PairObservation(
            pair=entry["pair"],
            session=session,
            reader=entry["readers"][0] if entry["readers"] else None,
            venue=entry["venues"][0] if entry["venues"] else None,
            time_slice=";".join(entry["time_slices"]),
            editions=tuple(entry["editions"]),
            visible_set=tuple(entry["visible_sets"][0]) if entry["visible_sets"] else (),
            event_ids=tuple(entry["event_ids"]),
            effective=entry["effective"],
            blocked=entry["blocked"],
            temporary_grant=entry["temporary_grant"],
            gate_failures=tuple(sorted(entry["gate_failures"])),
        )
    return observations


def _same_edition_map(events: list[Event]) -> dict[str, bool]:
    """同 pair 的作品是否在任何事件中由同一装订册承载（共现风险标记，不加权）。"""
    edition_to_works: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if event.edition and event.work:
            edition_to_works[event.edition].add(event.work)
    same_edition_pairs: set[str] = set()
    for works in edition_to_works.values():
        for combo in combinations(sorted(works), 2):
            same_edition_pairs.add(pair_key(combo))
    return {pair: pair in same_edition_pairs for pair in same_edition_pairs}


def _substitute_map(
    links: list[SubstituteLink],
) -> dict[str, list[SubstituteLink]]:
    mapping: dict[str, list[SubstituteLink]] = defaultdict(list)
    for link in links:
        mapping[link.original_work].append(link)
    return mapping


def summarize_pairs(
    observations: dict[tuple[str, str], PairObservation],
    events: list[Event],
    assessments: dict[str, EventAssessment],
    substitutes: list[SubstituteLink],
) -> dict[str, dict]:
    same_edition = _same_edition_map(events)
    sub_map = _substitute_map(substitutes)

    pairs: dict[str, dict] = {}
    for obs in observations.values():
        summary = pairs.setdefault(
            obs.pair,
            {
                "pair": obs.pair,
                "works": tuple(obs.pair.split("|")),
                "effective_sessions": set(),
                "blocked_sessions": set(),
                "exposure_only_sessions": set(),
                "readers": set(),
                "venues": set(),
                "time_slices": set(),
                "visible_sets": set(),
                "event_ids": [],
                "temporary_grant": False,
                "gate_failures": set(),
                "actions": set(),
            },
        )
        summary["readers"].add(obs.reader or "unknown")
        summary["venues"].add(obs.venue or "unknown")
        summary["time_slices"].add(obs.time_slice)
        summary["visible_sets"].add(obs.visible_set)
        summary["event_ids"].extend(obs.event_ids)
        summary["temporary_grant"] |= obs.temporary_grant
        summary["gate_failures"].update(obs.gate_failures)

    # 动作来源（搜索曝光 vs 实际比较/请求）按事件回填
    for event in events:
        assessment = assessments[event.event_id]
        for pair in _pairs_in(assessment.material_set):
            if pair not in pairs:
                continue
            pairs[pair]["actions"].add(event.action)
            session = event.session or "unknown-session"
            if assessment.effective:
                pairs[pair]["effective_sessions"].add(session)
            elif assessment.blocked:
                pairs[pair]["blocked_sessions"].add(session)
            elif event.action == "search" and not assessment.blocked:
                pairs[pair]["exposure_only_sessions"].add(session)

    results: dict[str, dict] = {}
    for pair, summary in pairs.items():
        works = summary["works"]
        effective = sorted(summary["effective_sessions"])
        blocked = sorted(summary["blocked_sessions"])
        exposure = sorted(summary["exposure_only_sessions"])
        sub_links = {
            work: [link.substitute_work for link in sub_map.get(work, [])]
            for work in works
        }
        has_quality_substitute = any(
            link.quality == "comparable"
            for work in works
            for link in sub_map.get(work, [])
        )

        reasons: list[str] = []
        if effective:
            classification = "supported"
            reasons.append(f"有效对照观察会话：{', '.join(effective)}")
            if summary["temporary_grant"]:
                reasons.append("观察依赖临时阅览授权，常设许可并不支持")
        elif exposure:
            classification = "spurious"
            if summary["gate_failures"]:
                reasons.append("目录可见但对照闸门失败：" + "；".join(sorted(summary["gate_failures"])))
            else:
                reasons.append("仅有搜索共曝光，没有任何比较动作支撑")
            reasons.append(f"曝光来源会话：{', '.join(exposure)}")
        else:
            classification = "inconclusive"
            if blocked:
                reasons.append(f"受限/未兑现请求会话：{', '.join(blocked)}，从未获得观察机会")
            else:
                reasons.append("没有观察机会记录，无法结论")

        same_ed = same_edition.get(pair, False)
        if same_ed:
            reasons.append("两件作品由同一装订册承载，共现可能来自装订而非研究需求")

        # 低频、无法替代的未满足请求：必须有独立业务解释，不得静默丢弃。
        # 仅适用于存在未满足需求信号（blocked 会话）但没有有效观察的组合；
        # 纯目录曝光的 spurious 组合没有“请求”，不触发本规则。
        low_freq_unmet = (
            not effective
            and bool(blocked)
            and len(blocked) <= 2
            and not has_quality_substitute
        )
        justification: Optional[str] = None
        if low_freq_unmet:
            justification = (
                f"组合 {pair} 仅有 {len(blocked)} 次未满足请求且题录中无达到 "
                "comparable 质量的替代件；需馆员登记独立业务解释（具体研究任务/"
                "不可替代原因）后才可进入补扫决策"
            )

        results[pair] = {
            "pair": pair,
            "works": works,
            "classification": classification,
            "reasons": reasons,
            "effective_sessions": effective,
            "blocked_sessions": blocked,
            "exposure_only_sessions": exposure,
            "distinct_readers": sorted(summary["readers"]),
            "distinct_venues": sorted(summary["venues"]),
            "time_slices": sorted(summary["time_slices"]),
            "distinct_visible_sets": [list(s) for s in summary["visible_sets"] if s],
            "source_actions": sorted(summary["actions"]),
            "same_edition_bundle": same_ed,
            "temporary_grant": summary["temporary_grant"],
            "gate_failures": sorted(summary["gate_failures"]),
            "substitute_links": sub_links,
            "has_comparable_substitute": has_quality_substitute,
            "requires_business_justification": justification,
            "event_ids": sorted(set(summary["event_ids"])),
        }
    return dict(sorted(results.items()))


def leave_one_session_out(
    observations: dict[tuple[str, str], PairObservation],
) -> list[HoldoutFold]:
    """留一会话折：用其余会话选组合，在保留会话中复核有效观察。"""
    sessions = sorted({obs.session for obs in observations.values()})
    folds: list[HoldoutFold] = []
    for holdout in sessions:
        training_pairs = {
            obs.pair
            for (session, _), obs in observations.items()
            if session != holdout and obs.effective
        }
        holdout_pairs = {
            obs.pair
            for (session, _), obs in observations.items()
            if session == holdout and obs.effective
        }
        folds.append(
            HoldoutFold(
                holdout_session=holdout,
                selected_in_training=tuple(sorted(training_pairs)),
                observed_in_holdout=tuple(sorted(holdout_pairs)),
                confirmed=tuple(sorted(training_pairs & holdout_pairs)),
            )
        )
    return folds


def out_of_sample_verdict(
    pair_summaries: dict[str, dict],
    folds: list[HoldoutFold],
) -> dict[str, str]:
    """对每个 supported 组合给出样本外结论。"""
    confirmed_any = {pair for fold in folds for pair in fold.confirmed}
    verdict: dict[str, str] = {}
    for pair, summary in pair_summaries.items():
        if summary["classification"] != "supported":
            verdict[pair] = "not_tested"
        elif pair in confirmed_any:
            verdict[pair] = "confirmed_out_of_sample"
        else:
            verdict[pair] = "in_sample_only_fails_holdout"
    return verdict
