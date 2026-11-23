"""领域模型。

所有记录均为合成样例。空字符串在 io 层即转为 None，表示“来源没有给值”，
与数值 0、布尔 False 严格区分（见 docs/数据说明.md：未知、零、缺报不同义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass(frozen=True)
class Event:
    """一条阅览链路事件（primary.csv）。"""

    event_id: str
    reader: Optional[str]
    session: Optional[str]
    work: Optional[str]
    edition: Optional[str]
    venue: Optional[str]
    action: str
    visible: tuple[str, ...]
    authorization: Optional[str]
    occurred: Optional[datetime]
    raw: dict[str, str]


@dataclass(frozen=True)
class ScanRecord:
    """一件作品的一个拍摄件（support-1.csv）。"""

    work: str
    scan: str
    quality: str
    permission: str
    starts: Optional[date]
    ends: Optional[date]


@dataclass(frozen=True)
class PolicyRecord:
    """治理政策记录（support-2.csv）。"""

    policy: str
    item: str
    rule: str


@dataclass(frozen=True)
class ClosureInterval:
    """闭馆时段（可选输入 closures.csv）。"""

    venue: str
    starts: datetime
    ends: datetime


@dataclass(frozen=True)
class SubstituteLink:
    """替代件关系（可选输入 substitutes.csv）。

    协议要求：替代件须在题录中明确替代对象且达到对照质量，方可承担对照。
    """

    substitute_work: str
    original_work: str
    quality: str
    note: Optional[str]


@dataclass(frozen=True)
class WorkGate:
    """组合观察中单件作品的对照资格闸门结果。"""

    work: str
    has_scan: bool
    has_comparable_scan: bool
    window_state: str  # covered | outside | unknown | no_scan
    standing_permission: Optional[str]
    passes: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EventAssessment:
    """事件级有效性评估。"""

    event_id: str
    action: str
    authorization_state: str  # valid | restricted | unknown
    effective: bool           # 是否构成有效观察
    blocked: bool             # 是否因授权/闭馆等根本没有观察机会
    exposure: tuple[str, ...]
    material_set: tuple[str, ...]
    work_gates: tuple[WorkGate, ...]
    temporary_grant: bool     # 常设许可不支持、靠临时阅览授权成立
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PairObservation:
    """一个研究任务（本数据以会话代理）内对某组合的一次去重观察。"""

    pair: str
    session: str
    reader: Optional[str]
    venue: Optional[str]
    time_slice: str
    editions: tuple[str, ...]
    visible_set: tuple[str, ...]
    event_ids: tuple[str, ...]
    effective: bool
    blocked: bool
    temporary_grant: bool
    gate_failures: tuple[str, ...]


@dataclass(frozen=True)
class HoldoutFold:
    """留一（会话）折：训练折选出的组合在保留折上能否复核。"""

    holdout_session: str
    selected_in_training: tuple[str, ...]
    observed_in_holdout: tuple[str, ...]
    confirmed: tuple[str, ...]


@dataclass(frozen=True)
class RescanCandidate:
    """一个装订册（edition）级别的补扫候选及其机会增量。"""

    edition: str
    works: tuple[str, ...]
    gap_reasons: tuple[str, ...]
    newly_realizable: tuple[dict, ...]
    remains_blocked: tuple[dict, ...]
    no_gain: tuple[str, ...]
    realizable_delta: int
    blocked_count: int


@dataclass(frozen=True)
class ImpactRow:
    """影响台账一行：许可收紧或新扫件不达标时，某组合对某研究组的处置。"""

    pair: str
    research_group: Optional[str]
    scenario: str
    status: str  # usable_as_agreed | alternative_delivery | postponed
    basis: str
