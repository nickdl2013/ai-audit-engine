"""HR 入职材料审核的确定性校验器。

原则与费用域完全一致：能用代码算准的，绝不交给大模型。
材料齐全性、日期、证件号、姓名这些全部在这里用代码算，结果 100% 可复现。

这些校验器的共同特征：它们算出来的每一条，HR 都能自己复核一遍
（"离职证明确实没交""体检报告是 6 月的"），所以结论无法被反驳。
这正是 HR 敢把系统用起来的前提。

新增一个校验器只要两步：
  1. 写一个 check_hr_xxx(rule, record) -> list[RuleVerdict]
  2. 注册进 HR_CHECKERS
然后在 config/rules_hr.yaml 里用 evaluator: "builtin:hr_xxx" 引用它。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Callable

from .checkers import _parse_date
from .hr_models import OnboardingDoc, OnboardingRecord
from .models import Rule, RuleVerdict, VerdictResult


def _mk(
    rule: Rule,
    result: VerdictResult,
    reason: str,
    *,
    evidence: str | None = None,
    confidence: float = 1.0,
    doc_ids: list[str] | None = None,
) -> RuleVerdict:
    return RuleVerdict(
        rule_id=rule.id,
        rule_name=rule.name,
        severity=rule.severity,
        result=result,
        reason=reason,
        evidence=evidence,
        basis=rule.basis,
        confidence=confidence,
        source="deterministic",
        item_ids=doc_ids or [],
    )


def _norm(text: object) -> str:
    """姓名 / 证件号比对前的归一化：去空白与全角空格，证件号再统一大写。"""
    return str(text or "").strip().replace(" ", "").replace("\u3000", "")


def _docs(rule: Rule, record: OnboardingRecord) -> list[OnboardingDoc]:
    return [d for d in record.docs if rule.applies_to(d)]


# ---------------------------------------------------------------- 材料完整性


def check_hr_required_docs(rule: Rule, record: OnboardingRecord) -> list[RuleVerdict]:
    """按用工形式核对应提交的必备材料清单。

    两条容易做错的业务细节，都在参数里配置化：
      - 不同用工形式清单不同（实习生要学生证和实习协议，不是劳动合同）
      - 有豁免条款（应届生没有前雇主，按制度免交离职证明）
    """
    params = rule.params
    required_map: dict = params.get("required_by_type") or {}
    required = list(
        required_map.get(record.employment_type) or required_map.get("全职") or []
    )
    if not required:
        return []

    exemptions = {
        e.get("doc_type"): e
        for e in (params.get("exempt_rules") or [])
        if isinstance(e, dict) and e.get("doc_type")
    }
    submitted_types = {d.doc_type for d in record.docs if d.submitted}

    missing: list[str] = []
    exempted: list[tuple[str, str]] = []
    for doc_type in required:
        if doc_type in submitted_types:
            continue
        exempt = exemptions.get(doc_type)
        if exempt:
            field = exempt.get("when_field")
            if field and getattr(record, field, None) == exempt.get("when_value"):
                exempted.append((doc_type, str(exempt.get("note") or "按制度豁免")))
                continue
        missing.append(doc_type)

    have = [t for t in required if t in submitted_types]
    parts = [f"用工形式「{record.employment_type}」应提交 {len(required)} 项：{'、'.join(required)}"]
    parts.append(f"实际已提交 {len(have)} 项：{'、'.join(have) if have else '无'}")
    if exempted:
        parts.append("豁免：" + "；".join(f"{t}（{n}）" for t, n in exempted))
    evidence = "｜".join(parts)

    if missing:
        return [
            _mk(
                rule,
                "fail",
                f"缺少 {len(missing)} 项必备材料：{'、'.join(missing)}，"
                f"按制度材料不齐不得办理入职手续",
                evidence=evidence,
            )
        ]
    tail = f"（其中 {len(exempted)} 项按制度豁免）" if exempted else ""
    return [_mk(rule, "pass", f"{len(required)} 项必备材料齐全{tail}", evidence=evidence)]


# ---------------------------------------------------------------- 日期与时效


def check_hr_doc_expiry(rule: Rule, record: OnboardingRecord) -> list[RuleVerdict]:
    """证件有效期须覆盖入职日（含缓冲期）。"""
    onboard = _parse_date(record.onboard_date)
    min_days = int(rule.params.get("min_valid_days", 0) or 0)
    expiry_required = set(rule.params.get("expiry_required_types") or [])

    verdicts: list[RuleVerdict] = []
    for doc in _docs(rule, record):
        if not doc.submitted:
            continue
        expiry = _parse_date(doc.expiry_date)
        if expiry is None:
            # 只有制度明确要求有效期的材料，缺日期才转人工；其余不产出结论
            if doc.doc_type in expiry_required:
                verdicts.append(
                    _mk(
                        rule,
                        "uncertain",
                        f"{doc.doc_id} {doc.doc_type} 未填写有效期，"
                        f"无法判断是否覆盖入职日 {record.onboard_date or '未填'}，转人工核验",
                        evidence=f"doc_id={doc.doc_id}｜expiry_date 为空",
                        confidence=0.4,
                        doc_ids=[doc.doc_id],
                    )
                )
            continue

        if onboard is None:
            verdicts.append(
                _mk(
                    rule,
                    "uncertain",
                    f"{doc.doc_id} 缺少拟入职日期，无法判断有效期是否覆盖，转人工",
                    confidence=0.4,
                    doc_ids=[doc.doc_id],
                )
            )
            continue

        need_until = onboard + timedelta(days=min_days)
        ok = expiry >= need_until
        if ok:
            tail = f"覆盖入职日（要求至少有效至 {need_until}）"
        else:
            tail = (
                f"距入职日 {record.onboard_date} 不足 {min_days} 天，"
                f"应至少有效至 {need_until}"
            )
        verdicts.append(
            _mk(
                rule,
                "pass" if ok else "fail",
                f"{doc.doc_id} {doc.doc_type} 有效期至 {doc.expiry_date}，{tail}",
                evidence=(
                    f"有效期至 {doc.expiry_date}｜入职日 {record.onboard_date}"
                    f"｜要求有效至 {need_until}"
                ),
                doc_ids=[doc.doc_id],
            )
        )
    return verdicts


def check_hr_issue_date_window(
    rule: Rule, record: OnboardingRecord
) -> list[RuleVerdict]:
    """材料出具日期须落在制度规定的时间窗内。

    典型场景：体检报告一般要求入职前 90 天内出具；离职证明原则上
    不能晚于入职日（否则存在双重劳动关系风险）。
    """
    onboard = _parse_date(record.onboard_date)
    offer = _parse_date(record.offer_date)
    windows: dict = rule.params.get("windows") or {}

    verdicts: list[RuleVerdict] = []
    for doc in _docs(rule, record):
        if not doc.submitted:
            continue
        spec = windows.get(doc.doc_type)
        if not isinstance(spec, dict):
            continue

        issue = _parse_date(doc.issue_date)
        if issue is None:
            verdicts.append(
                _mk(
                    rule,
                    "uncertain",
                    f"{doc.doc_id} {doc.doc_type} 未填写出具日期，无法判断时效，转人工核验",
                    evidence=f"doc_id={doc.doc_id}｜issue_date 为空",
                    confidence=0.4,
                    doc_ids=[doc.doc_id],
                )
            )
            continue
        if onboard is None:
            verdicts.append(
                _mk(
                    rule,
                    "uncertain",
                    f"{doc.doc_id} 缺少拟入职日期，无法判断出具时效，转人工",
                    confidence=0.4,
                    doc_ids=[doc.doc_id],
                )
            )
            continue

        problems: list[str] = []
        max_before = spec.get("max_days_before_onboard")
        if max_before is not None and issue < onboard - timedelta(days=int(max_before)):
            gap = (onboard - issue).days
            problems.append(
                f"出具日期 {doc.issue_date} 距入职日 {record.onboard_date} 已 {gap} 天，"
                f"超过 {int(max_before)} 天时效要求，需重新办理"
            )
        if spec.get("must_before_onboard") and issue > onboard:
            problems.append(
                f"出具日期 {doc.issue_date} 晚于入职日 {record.onboard_date}，"
                f"存在未与前雇主解除劳动关系即入职的风险"
            )
        if spec.get("must_after_offer") and offer and issue < offer:
            problems.append(
                f"出具日期 {doc.issue_date} 早于 offer 日期 {record.offer_date}"
            )

        verdicts.append(
            _mk(
                rule,
                "fail" if problems else "pass",
                "；".join(problems)
                if problems
                else f"{doc.doc_id} {doc.doc_type} 出具日期 {doc.issue_date} 符合时效要求",
                evidence=(
                    f"出具日期 {doc.issue_date}｜入职日 {record.onboard_date}"
                    f"｜offer 日期 {record.offer_date or '未填'}"
                ),
                doc_ids=[doc.doc_id],
            )
        )
    return verdicts


def check_hr_date_logic(rule: Rule, record: OnboardingRecord) -> list[RuleVerdict]:
    """offer 日期与入职日期的先后逻辑。"""
    offer = _parse_date(record.offer_date)
    onboard = _parse_date(record.onboard_date)

    problems: list[str] = []
    if onboard is None:
        problems.append("未填写拟入职日期")
    if offer and onboard and onboard < offer:
        problems.append(
            f"拟入职日期 {record.onboard_date} 早于 offer 日期 {record.offer_date}，"
            f"日期填报有误"
        )

    evidence = f"offer 日期 {record.offer_date or '未填'}｜拟入职日期 {record.onboard_date or '未填'}"
    if problems:
        return [_mk(rule, "fail", "；".join(problems), evidence=evidence)]
    return [
        _mk(
            rule,
            "pass",
            f"offer 日期 {record.offer_date or '未填'} 与拟入职日期 {record.onboard_date} 逻辑一致",
            evidence=evidence,
        )
    ]


# ---------------------------------------------------------------- 主体一致性


def check_hr_name_consistency(
    rule: Rule, record: OnboardingRecord
) -> list[RuleVerdict]:
    """材料持有人姓名须与候选人姓名一致。"""
    want = _norm(record.candidate_name)
    verdicts: list[RuleVerdict] = []
    for doc in _docs(rule, record):
        if not doc.submitted or not doc.holder_name:
            continue
        got = _norm(doc.holder_name)
        ok = (not want) or got == want
        verdicts.append(
            _mk(
                rule,
                "pass" if ok else "fail",
                f"{doc.doc_id} {doc.doc_type} 持有人「{doc.holder_name}」"
                + ("与候选人姓名一致" if ok else f"与候选人「{record.candidate_name}」不一致"),
                evidence=f"材料姓名 {doc.holder_name}｜登记姓名 {record.candidate_name}",
                doc_ids=[doc.doc_id],
            )
        )
    return verdicts


def check_hr_id_consistency(rule: Rule, record: OnboardingRecord) -> list[RuleVerdict]:
    """材料上的证件号须与登记身份证号一致 —— 这条是防冒名顶替的关键。"""
    want = _norm(record.id_no).upper()
    verdicts: list[RuleVerdict] = []
    for doc in _docs(rule, record):
        if not doc.submitted or not doc.holder_id_no:
            continue
        got = _norm(doc.holder_id_no).upper()
        if not want:
            verdicts.append(
                _mk(
                    rule,
                    "uncertain",
                    f"{doc.doc_id} {doc.doc_type} 已提供证件号，但候选人登记信息中缺少身份证号，"
                    f"无法比对，转人工",
                    evidence=f"材料证件号 {doc.holder_id_no}",
                    confidence=0.4,
                    doc_ids=[doc.doc_id],
                )
            )
            continue
        same = got == want
        verdicts.append(
            _mk(
                rule,
                "pass" if same else "fail",
                f"{doc.doc_id} {doc.doc_type} 证件号"
                + (
                    "与登记身份证号一致"
                    if same
                    else "与登记身份证号不一致，需核实是否为同一人"
                ),
                evidence=f"材料证件号 {doc.holder_id_no}｜登记证件号 {record.id_no}",
                doc_ids=[doc.doc_id],
            )
        )
    return verdicts


HR_CHECKERS: dict[str, Callable[[Rule, OnboardingRecord], list[RuleVerdict]]] = {
    "hr_required_docs": check_hr_required_docs,
    "hr_doc_expiry": check_hr_doc_expiry,
    "hr_issue_date_window": check_hr_issue_date_window,
    "hr_date_logic": check_hr_date_logic,
    "hr_name_consistency": check_hr_name_consistency,
    "hr_id_consistency": check_hr_id_consistency,
}


def run_hr_builtin(rule: Rule, record: OnboardingRecord) -> list[RuleVerdict]:
    name = rule.evaluator.split(":", 1)[1] if ":" in rule.evaluator else rule.evaluator
    checker = HR_CHECKERS.get(name)
    if checker is None:
        return [
            _mk(
                rule,
                "skipped",
                f"未注册的校验器「{name}」，请检查 rules_hr.yaml 或在 hr_checkers.py 中注册",
                confidence=0.0,
            )
        ]
    return checker(rule, record)
