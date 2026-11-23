"""事件级有效性判定，严格对应 docs/业务规程.md（阅览与补扫协议）。

协议要点到代码的映射：
1. 一次研究会话对同一作品的重复打开不构成新组合观察
   -> pairs 层按 (session, pair) 去重，open 事件不产生组合；
2. 组合成立须有相同研究任务下的有效可比材料及许可
   -> compare 事件需要授权 valid 且每件作品通过对照资格闸门；
3. 替代件须题录明确替代对象且达到对照质量
   -> 没有 substitutes 题录链接时，替代合集不得算作原件对照；
4. 装订册按物件逐项计开合额度
   -> planner 以 edition 为调度单元、以 work 逐项计开合；
5. 临时收紧许可立即停止后续配送，已交付副本按原授权留痕
   -> impact 台账区分 usable_as_agreed / alternative_delivery / postponed；
6. 闭馆时段内未发生的曝光不能计为可见未点击
   -> 闭馆命中的事件标 blocked，其 visible 不进入曝光证据。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from .models import (
    ClosureInterval,
    Event,
    EventAssessment,
    PolicyRecord,
    ScanRecord,
    WorkGate,
)

COMPARABLE_QUALITY = "comparable"
# 允许在临时授权下承担对照的常设许可类别；restricted 仅在事件授权 valid 时临时放行。
ON_DEMAND_PERMISSIONS = frozenset({"restricted", "onsite"})


def is_during_closure(
    occurred: Optional[datetime],
    venue: Optional[str],
    closures: Iterable[ClosureInterval],
) -> bool:
    if occurred is None:
        return False
    for interval in closures:
        if interval.venue == venue and interval.starts <= occurred < interval.ends:
            return True
    return False


def governance_state(policies: list[PolicyRecord]) -> tuple[str, tuple[str, ...]]:
    """support-2 的政策闸门：scope=reviewed 且 version=active 才在受控范围内。"""
    if not policies:
        return "missing", ("support-2 缺报：无法确认分析范围与版本治理，结论降级使用",)
    notes: list[str] = []
    scope_ok = version_ok = False
    for record in policies:
        if record.item == "scope":
            scope_ok = record.rule == "reviewed"
        if record.item == "version":
            version_ok = record.rule == "active"
        notes.append(f"{record.policy}:{record.item}={record.rule}")
    if scope_ok and version_ok:
        return "controlled", tuple(notes)
    return "out_of_scope", tuple(
        notes + ["政策未满足 reviewed/active，分析结果不具备受控发布条件"]
    )


def _window_state(record: ScanRecord, at: Optional[date]) -> str:
    if record.starts is None or record.ends is None or at is None:
        return "unknown"
    if record.starts <= at <= record.ends:
        return "covered"
    return "outside"


def work_gate(
    work: str,
    at: Optional[date],
    scans_by_work: dict[str, list[ScanRecord]],
) -> WorkGate:
    """单件作品在某一时点的对照资格：必须有授权区间覆盖的 comparable 拍摄件。

    常设许可不在本闸门内否决——临时阅览授权可覆盖 restricted/onsite，
    该情况由调用方通过 temporary_grant 标记，避免把临时放行误记为常设可见。
    """
    records = scans_by_work.get(work, [])
    if not records:
        return WorkGate(
            work=work,
            has_scan=False,
            has_comparable_scan=False,
            window_state="no_scan",
            standing_permission=None,
            passes=False,
            reasons=("没有任何拍摄记录，属缺报/未数字化",),
        )

    comparable = [r for r in records if r.quality == COMPARABLE_QUALITY]
    best_window = "unknown"
    for record in records:
        state = _window_state(record, at)
        if state == "covered":
            best_window = "covered"
            break
        if state == "unknown" and best_window != "outside":
            best_window = "unknown"
        elif state == "outside" and best_window == "unknown":
            best_window = "outside"

    reasons: list[str] = []
    if not comparable:
        qualities = sorted({r.quality for r in records})
        reasons.append(f"仅有的拍摄质量为 {qualities}，未达 comparable 对照质量")
    if best_window == "outside":
        reasons.append("授权区间未覆盖评估时点（区间外）")
    elif best_window == "unknown":
        reasons.append("授权区间缺报，无法确认覆盖")

    return WorkGate(
        work=work,
        has_scan=True,
        has_comparable_scan=bool(comparable),
        window_state=best_window,
        standing_permission=sorted({r.permission for r in records})[0]
        if records
        else None,
        passes=bool(comparable) and best_window == "covered",
        reasons=tuple(reasons),
    )


def _authorization_state(value: Optional[str]) -> str:
    if value == "valid":
        return "valid"
    if value == "restricted":
        return "restricted"
    return "unknown"


def evaluate_event(
    event: Event,
    scans_by_work: dict[str, list[ScanRecord]],
    closures: list[ClosureInterval],
) -> EventAssessment:
    at = event.occurred.date() if event.occurred else None
    closed = is_during_closure(event.occurred, event.venue, closures)
    auth = _authorization_state(event.authorization)
    material = tuple(dict.fromkeys(w for w in event.visible if w))
    gates = tuple(work_gate(w, at, scans_by_work) for w in material)
    notes: list[str] = []

    if event.occurred is None:
        notes.append("时间缺报：无法校验闭馆与授权区间，时间片归入 unknown")
    if closed:
        notes.append("命中闭馆时段：该曝光不计为可见未点击")
    if auth == "unknown":
        notes.append("授权字段缺报或非受控取值")

    effective = False
    blocked = closed
    temporary_grant = False
    gate_failures = tuple(
        f"{gate.work}:{reason}"
        for gate in gates
        if not gate.passes
        for reason in gate.reasons
    )

    action = event.action
    if action == "compare":
        standing_blocked = any(
            gate.standing_permission in ON_DEMAND_PERMISSIONS
            and gate.window_state == "covered"
            for gate in gates
        )
        temporary_grant = auth == "valid" and standing_blocked
        if temporary_grant:
            if gate_failures:
                notes.append(
                    "常设许可受限/仅馆内，本需事件级临时阅览授权；"
                    "但对照闸门另有失败项，观察仍不成立，临时授权留痕"
                )
            else:
                notes.append("常设许可受限/仅馆内：靠事件级临时阅览授权成立，留痕管理")
        if closed:
            blocked = True
        elif auth == "restricted":
            blocked = True
            notes.append("受限请求被拒：没有发生有效对照观察")
        elif auth != "valid":
            blocked = True
            notes.append("授权未确认：不构成有效观察")
        elif gate_failures:
            notes.append("对照资格闸门未通过：有材料可见但不具可比性")
        else:
            effective = True

    elif action == "request":
        # 请求被预约满额/授权拦截而放弃部分材料：记录未满足需求，永不计为观察。
        blocked = closed or auth != "valid" or bool(gate_failures)
        if auth == "restricted":
            notes.append("restricted 请求未兑现（预约/授权受限）")
        elif gate_failures:
            notes.append("请求材料未达对照资格，需求未满足")

    elif action == "search":
        # 搜索只产生曝光；闭馆内的曝光按协议不可作为“可见未点击”证据。
        if closed:
            notes.append("闭馆内搜索曝光不计入假关联证据")
        else:
            notes.append("搜索曝光：仅证明目录可见，不证明比较需求")

    elif action == "open":
        # 继续读同一材料：单作品访问，不产生组合；重复打开在 pairs 层再去重。
        if not closed and auth == "valid" and not gate_failures:
            notes.append("单作品继续阅读，不计组合观察")
        elif auth == "restricted" or gate_failures:
            blocked = True
    else:
        notes.append(f"未知动作 {action}：不参与组合证据")

    return EventAssessment(
        event_id=event.event_id,
        action=action,
        authorization_state=auth,
        effective=effective,
        blocked=blocked,
        exposure=() if closed else material,
        material_set=material,
        work_gates=gates,
        temporary_grant=temporary_grant,
        notes=tuple(notes),
    )
