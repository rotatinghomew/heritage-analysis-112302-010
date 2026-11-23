"""阅读诊断流水线的单元测试。

测试自带合成样例（写入临时目录），覆盖：
- 事件级有效性（闭馆曝光、受限请求、临时授权、继续阅读）；
- 同会话/同作品重复打开去重，不重复加权；
- 三分类：supported / spurious / inconclusive；
- 留一会话检验与独立业务解释；
- 补扫机会增量、受限阻断、约束缺报为 indeterminate；
- 影响台账三态与替代件质量边界；
- 全流程端到端跑通仓库自带 fixtures。
"""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from reading_diagnosis import run_analysis, write_reports
from reading_diagnosis.models import ClosureInterval, Event, ScanRecord
from reading_diagnosis.planner import (
    build_impact_ledger,
    evaluate_constraints,
)
from reading_diagnosis.protocol import evaluate_event, governance_state, is_during_closure
from reading_diagnosis.io_table import load_policies

ROOT = Path(__file__).resolve().parents[1]

EVENT_HEADER = [
    "event", "reader", "session", "work", "edition",
    "venue", "action", "visible", "authorization", "occurred",
]
SCAN_HEADER = ["work", "scan", "quality", "permission", "starts", "ends"]
POLICY_HEADER = ["policy", "item", "rule"]


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def build_case(root: Path, event_rows: list[list[str]],
               scan_rows: list[list[str]] | None = None,
               extra: dict[str, tuple[list[str], list[list[str]]]] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_csv(root / "primary.csv", EVENT_HEADER, event_rows)
    write_csv(
        root / "support-1.csv",
        SCAN_HEADER,
        scan_rows
        if scan_rows is not None
        else [
            ["W1", "SC1", "comparable", "study", "2025-01-01", "2026-12-31"],
            ["W2", "SC2", "low_quality", "onsite", "2025-01-01", "2026-06-01"],
            ["W3", "SC3", "comparable", "restricted", "2025-01-01", "2026-12-31"],
        ],
    )
    write_csv(
        root / "support-2.csv",
        POLICY_HEADER,
        [["P1", "scope", "reviewed"], ["P2", "version", "active"]],
    )
    for name, (header, rows) in (extra or {}).items():
        write_csv(root / name, header, rows)
    return root


def event(cid, reader, session, work, edition, venue, action, visible, auth, occurred):
    return Event(
        event_id=cid, reader=reader, session=session, work=work, edition=edition,
        venue=venue, action=action, visible=tuple(visible.split(";")) if visible else (),
        authorization=auth, occurred=__import__("datetime").datetime.fromisoformat(occurred),
        raw={},
    )


SCANS = {
    "W1": [ScanRecord("W1", "SC1", "comparable", "study",
                      date(2025, 1, 1), date(2026, 12, 31))],
    "W2": [ScanRecord("W2", "SC2", "low_quality", "onsite",
                      date(2025, 1, 1), date(2026, 6, 1))],
    "W3": [ScanRecord("W3", "SC3", "comparable", "restricted",
                      date(2025, 1, 1), date(2026, 12, 31))],
}


class ProtocolTests(unittest.TestCase):
    def test_closure_exposure_not_counted(self):
        ev = event("X", "R", "S", "W1", "E1", "A", "search",
                   "W1;W2", "valid", "2026-05-01T10:00")
        closure = [
            ClosureInterval(
                "A",
                __import__("datetime").datetime.fromisoformat("2026-05-01T09:00"),
                __import__("datetime").datetime.fromisoformat("2026-05-01T12:00"),
            )
        ]
        self.assertTrue(is_during_closure(ev.occurred, ev.venue, closure))
        assessment = evaluate_event(ev, SCANS, closure)
        self.assertTrue(assessment.blocked)
        self.assertEqual(assessment.exposure, ())

    def test_restricted_request_is_blocked_observation(self):
        ev = event("X", "R", "S", "W2", "E2", "B", "request",
                   "W2;W3", "restricted", "2026-05-02T11:00")
        assessment = evaluate_event(ev, SCANS, [])
        self.assertTrue(assessment.blocked)
        self.assertFalse(assessment.effective)

    def test_temporary_grant_makes_restricted_compare_effective(self):
        ev = event("X", "R", "S", "W3", "E1", "B", "compare",
                   "W1;W3", "valid", "2026-05-03T12:00")
        assessment = evaluate_event(ev, SCANS, [])
        self.assertTrue(assessment.effective)
        self.assertTrue(assessment.temporary_grant)

    def test_open_never_creates_pair(self):
        ev = event("X", "R", "S", "W1", "E1", "A", "open",
                   "W1", "valid", "2026-05-01T10:10")
        assessment = evaluate_event(ev, SCANS, [])
        self.assertFalse(assessment.effective)
        self.assertFalse(assessment.blocked)

    def test_low_quality_gate_fails_even_when_authorized(self):
        ev = event("X", "R", "S", "W1", "E1", "A", "compare",
                   "W1;W2", "valid", "2026-05-01T10:05")
        assessment = evaluate_event(ev, SCANS, [])
        self.assertFalse(assessment.effective)
        self.assertTrue(
            any("comparable" in reason for g in assessment.work_gates
                for reason in g.reasons if g.work == "W2")
        )

    def test_governance_requires_reviewed_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_case(
                Path(tmp),
                [["E1", "R1", "S1", "W1", "E1", "A", "open", "W1",
                  "valid", "2026-05-01T10:10"]],
            )
            policies = load_policies(root / "support-2.csv")
            state, _ = governance_state(policies)
            self.assertEqual(state, "controlled")
            state_missing, _ = governance_state([])
            self.assertEqual(state_missing, "missing")


class PairDiagnosisTests(unittest.TestCase):
    def _run(self, event_rows, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_case(Path(tmp), event_rows, extra=extra)
            return run_analysis(root, as_of=date(2026, 11, 23))

    def test_three_way_classification_on_fixture_pattern(self):
        rows = [
            ["E1", "R1", "S1", "W1", "E1", "A", "search", "W1;W2", "valid", "2026-05-01T10:00"],
            ["E2", "R1", "S1", "W1", "E1", "A", "compare", "W1;W2", "valid", "2026-05-01T10:05"],
            ["E3", "R1", "S1", "W1", "E1", "A", "open", "W1", "valid", "2026-05-01T10:10"],
            ["E4", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
            ["E5", "R3", "S3", "W3", "E1", "B", "compare", "W1;W3", "valid", "2026-05-03T12:00"],
        ]
        result = self._run(rows)
        classes = {p: s["classification"] for p, s in result["pairs"].items()}
        self.assertEqual(classes["W1|W2"], "spurious")
        self.assertEqual(classes["W1|W3"], "supported")
        self.assertEqual(classes["W2|W3"], "inconclusive")

    def test_repeated_opens_and_scans_do_not_double_weight(self):
        rows = [
            ["E1", "R1", "S1", "W1", "E1", "A", "compare", "W1;W3", "valid", "2026-05-01T10:00"],
            ["E2", "R1", "S1", "W1", "E1", "A", "compare", "W1;W3", "valid", "2026-05-01T10:30"],
            ["E3", "R1", "S1", "W1", "E1", "A", "open", "W1", "valid", "2026-05-01T10:40"],
            ["E4", "R1", "S1", "W1", "E1", "A", "open", "W1", "valid", "2026-05-01T10:50"],
        ]
        result = self._run(rows)
        summary = result["pairs"]["W1|W3"]
        self.assertEqual(summary["effective_sessions"], ["S1"])
        self.assertEqual(len(summary["event_ids"]), 2)  # compare 两条折叠为一个会话观察

    def test_holdout_fails_for_single_session_support(self):
        rows = [
            ["E5", "R3", "S3", "W3", "E1", "B", "compare", "W1;W3", "valid", "2026-05-03T12:00"],
        ]
        result = self._run(rows)
        self.assertEqual(
            result["pairs"]["W1|W3"]["holdout_verdict"],
            "in_sample_only_fails_holdout",
        )

    def test_holdout_confirms_when_second_session_reproduces(self):
        rows = [
            ["E1", "R1", "S1", "W1", "E1", "A", "compare", "W1;W3", "valid", "2026-05-01T10:00"],
            ["E2", "R2", "S2", "W1", "E1", "A", "compare", "W1;W3", "valid", "2026-05-04T10:00"],
        ]
        result = self._run(rows)
        self.assertEqual(
            result["pairs"]["W1|W3"]["holdout_verdict"],
            "confirmed_out_of_sample",
        )

    def test_low_frequency_unmet_request_requires_justification(self):
        rows = [
            ["E4", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
        ]
        result = self._run(rows)
        summary = result["pairs"]["W2|W3"]
        self.assertEqual(summary["classification"], "inconclusive")
        self.assertIsNotNone(summary["requires_business_justification"])

    def test_comparable_substitute_removes_justification_flag(self):
        rows = [
            ["E4", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
        ]
        extra = {
            "substitutes.csv": (
                ["substitute_work", "original_work", "quality", "note"],
                [["W9", "W3", "comparable", "题录明确替代 W3"]],
            )
        }
        result = self._run(rows, extra=extra)
        self.assertFalse(
            result["pairs"]["W2|W3"]["requires_business_justification"]
        )
        self.assertTrue(result["pairs"]["W2|W3"]["has_comparable_substitute"])

    def test_low_quality_substitute_does_not_count(self):
        rows = [
            ["E4", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
        ]
        extra = {
            "substitutes.csv": (
                ["substitute_work", "original_work", "quality", "note"],
                [["W9", "W3", "low_quality", "质量不达标"]],
            )
        }
        result = self._run(rows, extra=extra)
        self.assertFalse(result["pairs"]["W2|W3"]["has_comparable_substitute"])
        self.assertIsNotNone(
            result["pairs"]["W2|W3"]["requires_business_justification"]
        )


class PlannerTests(unittest.TestCase):
    def _result(self, event_rows, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_case(Path(tmp), event_rows, extra=extra)
            return run_analysis(root, as_of=date(2026, 11, 23))

    def test_rescan_delta_and_restriction_blocking(self):
        rows = [
            ["E1", "R1", "S1", "W1", "E1", "A", "compare", "W1;W2", "valid", "2026-05-01T10:05"],
            ["E4", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
        ]
        result = self._result(rows)
        candidates = result["rescan_candidates"]
        self.assertEqual([c["edition"] for c in candidates], ["E2"])
        candidate = candidates[0]
        self.assertEqual(candidate["realizable_delta"], 1)
        self.assertEqual(
            [item["pair"] for item in candidate["newly_realizable"]], ["W1|W2"]
        )
        self.assertEqual(
            [item["pair"] for item in candidate["remains_blocked"]], ["W2|W3"]
        )

    def test_missing_constraints_are_indeterminate_not_zero(self):
        rows = [
            ["E4", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
        ]
        result = self._result(rows)
        self.assertEqual(result["constraints"]["status"], "indeterminate")
        self.assertIn("open_close_budget", result["constraints"]["required_fields"])

    def test_impact_ledger_three_states(self):
        rows = [
            ["E1", "R1", "S1", "W1", "E1", "A", "compare", "W1;W3", "valid", "2026-05-03T12:00"],
            ["E2", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
        ]
        result = self._result(rows)
        ledger = {(r["pair"], r["research_group"]): r["status"]
                  for r in result["impact_ledger"]}
        # R3 不存在于此样例；R1 已交付 W1|W3 -> 收紧后按原约可用
        self.assertEqual(ledger[("W1|W3", "R1")], "usable_as_agreed")
        self.assertEqual(ledger[("W2|W3", "R2")], "postponed")

    def test_impact_ledger_alternative_delivery_via_substitute(self):
        rows = [
            ["E2", "R2", "S2", "W2", "E2", "B", "request", "W2;W3", "restricted", "2026-05-02T11:00"],
        ]
        extra = {
            "substitutes.csv": (
                ["substitute_work", "original_work", "quality", "note"],
                [["W9-onsite", "W3", "comparable", "题录明确替代 W3，馆内可阅"]],
            )
        }
        result = self._result(rows, extra=extra)
        scenario = {
            "scenario": "W3 许可临时收紧",
            "affected_work": "W3",
            "kind": "permission",
        }
        from reading_diagnosis.io_table import load_events, load_scans
        with tempfile.TemporaryDirectory() as tmp:
            root = build_case(Path(tmp), rows, extra=extra)
            events = load_events(root / "primary.csv")
            scans_by_work: dict = {}
            for rec in load_scans(root / "support-1.csv"):
                scans_by_work.setdefault(rec.work, []).append(rec)
            assessments = {
                ev.event_id: evaluate_event(ev, scans_by_work, []) for ev in events
            }
            ledger = build_impact_ledger(
                result["pairs"], assessments, events, [scenario]
            )
            self.assertEqual(
                [row["status"] for row in ledger], ["alternative_delivery"]
            )


class EndToEndTests(unittest.TestCase):
    def test_repo_fixtures_end_to_end(self):
        result = run_analysis(ROOT / "fixtures", as_of=date(2026, 11, 23))
        classes = {p: s["classification"] for p, s in result["pairs"].items()}
        self.assertEqual(
            classes,
            {"W1|W2": "spurious", "W1|W3": "supported", "W2|W3": "inconclusive"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            json_path, md_path = write_reports(result, Path(tmp))
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIn("pairs", payload)
            self.assertIn("共现诊断", md_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
