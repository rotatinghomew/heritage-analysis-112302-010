"""CSV 载入与解析。

设计原则（对应 docs/数据说明.md）：
- 首行为字段名；空值表示来源没有给值，解析为 None；
- “未知 / 零 / 缺报”分开表示：缺报=文件或字段缺失；未知=字段为空；零仅用于显式计数；
- 只读标准库，解析失败抛出带文件名与行号的 ValueError，便于业务人员定位。
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .models import ClosureInterval, Event, PolicyRecord, ScanRecord, SubstituteLink


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _split_set(value: Optional[str]) -> tuple[str, ...]:
    cleaned = _clean(value)
    if cleaned is None:
        return ()
    return tuple(part.strip() for part in cleaned.split(";") if part.strip())


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError(f"无法解析时间戳 {cleaned!r}：{exc}") from exc


def _parse_date(value: Optional[str]) -> Optional[date]:
    dt = _parse_datetime(value)
    return dt.date() if dt else None


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"缺少输入文件：{path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError(f"{path} 缺少字段名行")
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"{path} 字段名重复：{reader.fieldnames}")
        rows = []
        for line, row in enumerate(reader, start=2):
            try:
                rows.append({key: (_clean(val) or "") for key, val in row.items()})
            except Exception as exc:  # pragma: no cover - 防御性
                raise ValueError(f"{path} 第 {line} 行解析失败：{exc}") from exc
    if not rows:
        raise ValueError(f"{path} 只有字段名、没有记录")
    return rows


def load_events(path: Path) -> list[Event]:
    events: list[Event] = []
    for row in load_csv_rows(path):
        events.append(
            Event(
                event_id=row.get("event", "") or f"line-{len(events) + 2}",
                reader=_clean(row.get("reader")),
                session=_clean(row.get("session")),
                work=_clean(row.get("work")),
                edition=_clean(row.get("edition")),
                venue=_clean(row.get("venue")),
                action=_clean(row.get("action")) or "unknown",
                visible=_split_set(row.get("visible")),
                authorization=_clean(row.get("authorization")),
                occurred=_parse_datetime(row.get("occurred")),
                raw=row,
            )
        )
    return events


def load_scans(path: Path) -> list[ScanRecord]:
    records: list[ScanRecord] = []
    for row in load_csv_rows(path):
        work = _clean(row.get("work"))
        scan = _clean(row.get("scan"))
        if work is None or scan is None:
            raise ValueError(f"{path} 存在缺少 work/scan 的记录：{row}")
        records.append(
            ScanRecord(
                work=work,
                scan=scan,
                quality=_clean(row.get("quality")) or "unknown",
                permission=_clean(row.get("permission")) or "unknown",
                starts=_parse_date(row.get("starts")),
                ends=_parse_date(row.get("ends")),
            )
        )
    return records


def load_policies(path: Path) -> list[PolicyRecord]:
    records: list[PolicyRecord] = []
    for row in load_csv_rows(path):
        records.append(
            PolicyRecord(
                policy=_clean(row.get("policy")) or "",
                item=_clean(row.get("item")) or "",
                rule=_clean(row.get("rule")) or "",
            )
        )
    return records


def load_optional(path: Path) -> list[dict[str, str]]:
    """可选输入：文件不存在返回空列表，由调用方区分“缺报”与“为空”。"""
    if not path.exists():
        return []
    return load_csv_rows(path)


def load_closures(path: Path) -> list[ClosureInterval]:
    intervals: list[ClosureInterval] = []
    for row in load_optional(path):
        starts = _parse_datetime(row.get("starts"))
        ends = _parse_datetime(row.get("ends"))
        venue = _clean(row.get("venue"))
        if venue is None or starts is None or ends is None:
            raise ValueError(f"{path} 闭馆记录缺少 venue/starts/ends：{row}")
        intervals.append(ClosureInterval(venue=venue, starts=starts, ends=ends))
    return intervals


def load_substitutes(path: Path) -> list[SubstituteLink]:
    links: list[SubstituteLink] = []
    for row in load_optional(path):
        sub = _clean(row.get("substitute_work"))
        original = _clean(row.get("original_work"))
        if sub is None or original is None:
            raise ValueError(
                f"{path} 替代关系缺少 substitute_work/original_work：{row}"
            )
        links.append(
            SubstituteLink(
                substitute_work=sub,
                original_work=original,
                quality=_clean(row.get("quality")) or "unknown",
                note=_clean(row.get("note")),
            )
        )
    return links
