"""全流程编排：载入 -> 协议判定 -> 组合诊断 -> 样本外检验 -> 补扫规划 -> 报告。"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from .io_table import (
    load_closures,
    load_events,
    load_optional,
    load_policies,
    load_scans,
    load_substitutes,
)
from .pairs import (
    build_observations,
    leave_one_session_out,
    out_of_sample_verdict,
    summarize_pairs,
)
from .planner import (
    build_impact_ledger,
    build_rescan_candidates,
    current_fulfillable_pairs,
    evaluate_constraints,
    horizon_risks,
)
from .protocol import evaluate_event, governance_state

CLASSIFICATION_TITLE = {
    "supported": "真正有支持的组合",
    "spurious": "目录推荐型假关联（可见但不可比/仅曝光）",
    "inconclusive": "无观察机会、无法结论的组合",
}


def run_analysis(
    fixtures_dir: Path,
    as_of: Optional[date] = None,
    scenarios: Optional[list[dict]] = None,
) -> dict[str, Any]:
    fixtures_dir = Path(fixtures_dir)
    as_of = as_of or date.today()

    events = load_events(fixtures_dir / "primary.csv")
    scans = load_scans(fixtures_dir / "support-1.csv")
    policies = load_policies(fixtures_dir / "support-2.csv")
    closures = load_closures(fixtures_dir / "closures.csv")
    substitutes = load_substitutes(fixtures_dir / "substitutes.csv")
    constraints_rows = load_optional(fixtures_dir / "constraints.csv")
    missing_inputs = []
    for name in ("closures.csv", "substitutes.csv", "constraints.csv"):
        if not (fixtures_dir / name).exists():
            missing_inputs.append(name)

    scans_by_work: dict[str, list] = {}
    for record in scans:
        scans_by_work.setdefault(record.work, []).append(record)

    gov_state, gov_notes = governance_state(policies)
    assessments = {
        event.event_id: evaluate_event(event, scans_by_work, closures)
        for event in events
    }
    observations = build_observations(events, assessments)
    pair_summaries = summarize_pairs(
        observations, events, assessments, substitutes
    )
    folds = leave_one_session_out(observations)
    holdout_verdict = out_of_sample_verdict(pair_summaries, folds)
    for pair, verdict in holdout_verdict.items():
        pair_summaries[pair]["holdout_verdict"] = verdict

    candidates = build_rescan_candidates(events, scans, pair_summaries, as_of)
    constraints = evaluate_constraints(constraints_rows, candidates)
    fulfillable = current_fulfillable_pairs(pair_summaries, scans, as_of)
    risks = horizon_risks(scans, pair_summaries, as_of)

    scenarios = scenarios or [
        {
            "scenario": "某涉及作品许可临时收紧",
            "affected_work": pair_summaries and next(iter(pair_summaries.values()))["works"][0],
            "kind": "permission",
        },
        {
            "scenario": "新扫件质量未达可比要求",
            "affected_work": "W2",
            "kind": "quality",
        },
    ]
    scenarios = [s for s in scenarios if s.get("affected_work")]
    impact = build_impact_ledger(pair_summaries, assessments, events, scenarios)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "governance": {"state": gov_state, "notes": list(gov_notes)},
        "missing_optional_inputs": missing_inputs,
        "event_assessments": [asdict(a) for a in assessments.values()],
        "pair_observations_dedup": [asdict(o) for o in observations.values()],
        "pairs": pair_summaries,
        "holdout_folds": [asdict(f) for f in folds],
        "rescan_candidates": candidates,
        "constraints": constraints,
        "current_fulfillable_pairs": list(fulfillable.values()),
        "horizon_risks": risks,
        "impact_ledger": impact,
    }


def write_reports(result: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "diagnosis.json"
    md_path = out_dir / "diagnosis_report.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(_render_markdown(result), encoding="utf-8")
    return json_path, md_path


def _bullets(lines: list[str], indent: str = "") -> str:
    return "\n".join(f"{indent}- {line}" for line in lines) if lines else f"{indent}- 无"


def _render_markdown(result: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append("# 共现诊断与主动补扫报告（合成样例）\n")
    parts.append(f"- 生成时间：{result['generated_at']}")
    parts.append(f"- 评估基准日：{result['as_of']}")
    parts.append(
        f"- 治理状态：{result['governance']['state']}（{'；'.join(result['governance']['notes'])}）"
    )
    if result["missing_optional_inputs"]:
        parts.append(
            "- 缺报输入（按未知处理，未当作零值）："
            + "、".join(result["missing_optional_inputs"])
        )

    parts.append("\n## 1. 事件级有效性判定\n")
    parts.append("| 事件 | 动作 | 授权状态 | 有效观察 | 受阻 | 临时授权 | 判定说明 |")
    parts.append("|---|---|---|---|---|---|---|")
    for a in result["event_assessments"]:
        parts.append(
            "| {e} | {act} | {auth} | {eff} | {blk} | {tmp} | {notes} |".format(
                e=a["event_id"],
                act=a["action"],
                auth=a["authorization_state"],
                eff="是" if a["effective"] else "否",
                blk="是" if a["blocked"] else "否",
                tmp="是" if a["temporary_grant"] else "否",
                notes="；".join(a["notes"]),
            )
        )

    parts.append("\n## 2. 组合三分类\n")
    grouped: dict[str, list] = {"supported": [], "spurious": [], "inconclusive": []}
    for summary in result["pairs"].values():
        grouped[summary["classification"]].append(summary)
    for classification in ("supported", "spurious", "inconclusive"):
        parts.append(f"\n### 2.{list(grouped).index(classification) + 1} {CLASSIFICATION_TITLE[classification]}\n")
        for summary in grouped[classification]:
            parts.append(f"**{summary['pair']}**（作品 {'、'.join(summary['works'])}）")
            parts.append(_bullets(summary["reasons"], indent="  "))
            parts.append(
                f"  - 样本外检验：{summary['holdout_verdict']}"
            )
            parts.append(
                f"  - 维度覆盖：馆点 {summary['distinct_venues']}、"
                f"读者 {summary['distinct_readers']}、时间片 {summary['time_slices']}、"
                f"可见集合 {summary['distinct_visible_sets']}"
            )
            if summary["requires_business_justification"]:
                parts.append(
                    f"  - ⚠ 独立业务解释要求：{summary['requires_business_justification']}"
                )
            if summary["same_edition_bundle"]:
                parts.append("  - 同装订册共现标记（不加权）：是")
            parts.append("")

    parts.append("## 3. 去重与样本外检验\n")
    parts.append(
        "观察以（研究任务=会话, 组合）去重；同一件拓片的多次拍摄与重复打开不重复加权。\n"
    )
    parts.append("| 保留会话 | 训练折选中组合 | 保留折有效组合 | 复核通过 |")
    parts.append("|---|---|---|---|")
    for fold in result["holdout_folds"]:
        parts.append(
            "| {h} | {tr} | {ho} | {cf} |".format(
                h=fold["holdout_session"],
                tr=", ".join(fold["selected_in_training"]) or "—",
                ho=", ".join(fold["observed_in_holdout"]) or "—",
                cf=", ".join(fold["confirmed"]) or "无",
            )
        )

    parts.append("\n## 4. 补扫候选与机会增量\n")
    if not result["rescan_candidates"]:
        parts.append("无扫描质量缺口驱动的补扫候选（受限问题须走授权路径而非补扫）。")
    for candidate in result["rescan_candidates"]:
        parts.append(f"### 装订册 {candidate['edition']}（含 {'、'.join(candidate['works'])}）")
        parts.append(_bullets(list(candidate["gap_reasons"]), indent=""))
        if candidate["newly_realizable"]:
            for item in candidate["newly_realizable"]:
                parts.append(
                    f"- 新增可比较机会：{item['pair']}，交付方式 {item['delivery']}（{item['note']}）"
                )
        if candidate["remains_blocked"]:
            for item in candidate["remains_blocked"]:
                parts.append(f"- 仍无法兑现：{item['pair']} —— {item['reason']}")
        if candidate["no_gain_pairs"]:
            parts.append(f"- 无增量组合：{', '.join(candidate['no_gain_pairs'])}")
        if candidate["restrictions"]:
            parts.append(_bullets(list(candidate["restrictions"]), indent=""))
        parts.append(
            f"- 汇总：可兑现机会增量 {candidate['realizable_delta']}；"
            f"仍被阻断 {candidate['blocked_count']}\n"
        )

    parts.append("## 5. 排期约束\n")
    constraints = result["constraints"]
    parts.append(f"- 状态：{constraints['status']}")
    if constraints["status"] == "indeterminate":
        parts.append(f"- 原因：{constraints['reason']}")
        parts.append(f"- 决策：{constraints['scheduling_decision']}")
        parts.append("- 所需字段：" + "、".join(constraints["required_fields"]))
    else:
        for decision in constraints["decisions"]:
            parts.append(f"- {decision}")

    parts.append("\n## 6. 当前可兑现组合与时间窗风险\n")
    if result["current_fulfillable_pairs"]:
        for item in result["current_fulfillable_pairs"]:
            parts.append(
                f"- {item['pair']}：{'当前可兑现' if item['fulfillable_now'] else '受限阻断'}；{item['note']}"
            )
    else:
        parts.append("- 当前没有可直接兑现的组合（限制/质量问题均未解除）")
    parts.append("\n风险项：")
    parts.append(_bullets(result["horizon_risks"], indent=""))

    parts.append("\n## 7. 许可收紧 / 质量不达标影响台账\n")
    parts.append("| 场景 | 组合 | 研究组 | 处置 | 依据 |")
    parts.append("|---|---|---|---|---|")
    for row in result["impact_ledger"]:
        parts.append(
            "| {s} | {p} | {g} | {st} | {b} |".format(
                s=row["scenario"],
                p=row["pair"],
                g=row["research_group"],
                st=row["status"],
                b=row["basis"],
            )
        )
    parts.append(
        "\n> 替代合集只有在题录明确替代对象且达到 comparable 质量时承担对照；"
        "不同研究组共享同一替代册不等于都具备原件比较条件（见 substitutes 题录校验）。"
    )
    return "\n".join(parts) + "\n"
