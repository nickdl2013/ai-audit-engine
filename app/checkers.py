"""确定性校验器。

原则：能用代码算准的，绝不交给大模型。
金额、日期、发票号、必填项这些全部在这里用代码算，
结果 100% 可复现 —— 这是财务能接受 AI 审核的前提。

新增一个校验器只要两步：
  1. 写一个 check_xxx(rule, report) -> list[RuleVerdict]
  2. 注册进 CHECKERS
然后在 rules.yaml 里用 evaluator: "builtin:xxx" 引用它。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable

from .models import ExpenseItem, ExpenseReport, Rule, RuleVerdict, VerdictResult

# 工具函数


def _mk(
    rule: Rule,
    result: VerdictResult,
    reason: str,
    *,
    evidence: str | None = None,
    confidence: float = 1.0,
    item_ids: list[str] | None = None,
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
        item_ids=item_ids or [],
    )


def _parse_date(raw: str | None):
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _resolve_tier(city: str | None, tier_map: dict[str, list[str]]) -> str:
    if not city:
        return "其他"
    for tier, cities in (tier_map or {}).items():
        for name in cities or []:
            # 支持 "北京朝阳区" 这类带后缀的城市名
            if city == name or city.startswith(name):
                return tier
    return "其他"


def _resolve_limit(rule: Rule, item: ExpenseItem) -> float | None:
    """解析该明细适用的限额。支持按城市等级分档，也支持固定值。"""
    params = rule.params
    limits = params.get("limits")
    if isinstance(limits, dict) and limits:
        tier = _resolve_tier(item.city, params.get("city_tier_map") or {})
        value = limits.get(tier, limits.get("其他"))
        return float(value) if value is not None else None
    if "limit" in params:
        return float(params["limit"])
    return None


# 校验器实现


def check_amount_limit(rule: Rule, report: ExpenseReport) -> list[RuleVerdict]:
    """限额校验。支持 per_item / per_night / per_person / per_day 四种口径。"""
    params = rule.params
    mode = params.get("mode", "per_item")
    items = [i for i in report.items if rule.applies_to(i)]
    if not items:
        return []

    # 日累计口径：按 (费用类型, 日期) 分组后比总额
    if mode == "per_day":
        limit = float(params.get("limit", 0))
        groups: dict[tuple[str, str], list[ExpenseItem]] = defaultdict(list)
        for item in items:
            groups[(item.category, item.date)].append(item)

        verdicts: list[RuleVerdict] = []
        for (category, day), group in sorted(groups.items()):
            total = round(sum(i.amount for i in group), 2)
            ids = [i.item_id for i in group]
            detail = "、".join(f"{i.item_id} {_money(i.amount)}" for i in group)
            if total > limit:
                verdicts.append(
                    _mk(
                        rule,
                        "fail",
                        f"{day} 的{category}累计 {_money(total)} 元，"
                        f"超过每日限额 {_money(limit)} 元，超出 {_money(total - limit)} 元",
                        evidence=f"{day} 共 {len(group)} 笔：{detail}",
                        item_ids=ids,
                    )
                )
            else:
                verdicts.append(
                    _mk(
                        rule,
                        "pass",
                        f"{day} 的{category}累计 {_money(total)} 元，未超每日限额 {_money(limit)} 元",
                        evidence=f"{day} 共 {len(group)} 笔：{detail}",
                        item_ids=ids,
                    )
                )
        return verdicts

    # 单笔 / 按晚 / 按人口径
    verdicts = []
    for item in items:
        limit = _resolve_limit(rule, item)
        if limit is None:
            continue

        divisor = 1
        divisor_text = "单笔"
        confidence = 1.0
        note = ""

        if mode == "per_night":
            if item.nights and item.nights > 0:
                divisor = item.nights
                divisor_text = f"{divisor} 晚"
            else:
                divisor_text = "未填写晚数，按 1 晚核算"
                note = "，未填写晚数，建议人工核对"
                confidence = 0.6
        elif mode == "per_person":
            people = len(item.attendees or [])
            if people == 0:
                # 折算不出来就不能硬判 —— 这正是要转人工的场景
                verdicts.append(
                    _mk(
                        rule,
                        "uncertain",
                        f"{item.item_id} 未填写参与人数，无法折算人均金额，转人工核对",
                        evidence=f"金额 {_money(item.amount)} 元，attendees 为空",
                        confidence=0.4,
                        item_ids=[item.item_id],
                    )
                )
                continue
            divisor = people
            divisor_text = f"{divisor} 人"

        unit = round(item.amount / divisor, 2)
        ok = unit <= limit
        reason = (
            f"{item.item_id} {item.category} 折算单价 {_money(unit)} 元"
            f"{'（' + divisor_text + '）' if divisor_text != '单笔' else ''}，"
            + (f"未超过限额 {_money(limit)} 元" if ok else f"超过限额 {_money(limit)} 元，超出 {_money(unit - limit)} 元")
            + note
        )
        verdicts.append(
            _mk(
                rule,
                "pass" if ok else "fail",
                reason,
                evidence=f"金额 {_money(item.amount)} 元 ÷ {divisor_text} = {_money(unit)} 元"
                + (f"｜城市 {item.city}" if item.city else ""),
                confidence=confidence,
                item_ids=[item.item_id],
            )
        )
    return verdicts


def check_trip_date_range(rule: Rule, report: ExpenseReport) -> list[RuleVerdict]:
    """消费日期必须落在出差行程区间内。"""
    start = _parse_date(report.trip_start)
    end = _parse_date(report.trip_end)
    if not start and not end:
        return []

    tolerance = int(rule.params.get("tolerance_days", 0) or 0)
    span = f"{report.trip_start or '不限'} ~ {report.trip_end or '不限'}"

    verdicts = []
    for item in (i for i in report.items if rule.applies_to(i)):
        day = _parse_date(item.date)
        if day is None:
            verdicts.append(
                _mk(
                    rule,
                    "uncertain",
                    f"{item.item_id} 消费日期「{item.date}」无法解析，转人工核对",
                    confidence=0.4,
                    item_ids=[item.item_id],
                )
            )
            continue

        in_range = True
        if start and day < start - timedelta(days=tolerance):
            in_range = False
        if end and day > end + timedelta(days=tolerance):
            in_range = False

        verdicts.append(
            _mk(
                rule,
                "pass" if in_range else "fail",
                f"{item.item_id} 消费日期 {item.date} "
                + ("在出差行程区间内" if in_range else f"不在出差行程区间「{span}」内"),
                evidence=f"行程区间 {span}｜消费日期 {item.date}",
                item_ids=[item.item_id],
            )
        )
    return verdicts


def check_invoice_header(rule: Rule, report: ExpenseReport) -> list[RuleVerdict]:
    """发票抬头与纳税人识别号必须与公司主体一致。"""
    company = rule.params.get("company") or {}
    want_name = str(company.get("name") or "").strip()
    want_tax = str(company.get("tax_no") or "").strip()

    verdicts = []
    for item in (i for i in report.items if rule.applies_to(i)):
        if not item.invoice_title and not item.invoice_tax_no:
            continue  # 明细未提供票据信息，跳过而不产出结论

        problems = []
        if want_name and item.invoice_title and item.invoice_title.strip() != want_name:
            problems.append(f"发票抬头为「{item.invoice_title}」，应为「{want_name}」")
        if want_tax and item.invoice_tax_no and item.invoice_tax_no.strip() != want_tax:
            problems.append(f"纳税人识别号为「{item.invoice_tax_no}」，与公司税号不一致")

        verdicts.append(
            _mk(
                rule,
                "fail" if problems else "pass",
                "；".join(problems) if problems else "发票抬头与纳税人识别号均与公司主体一致",
                evidence=f"{item.item_id}｜发票号 {item.invoice_no or '未填'}",
                item_ids=[item.item_id],
            )
        )
    return verdicts


def check_duplicate_invoice(rule: Rule, report: ExpenseReport) -> list[RuleVerdict]:
    """同一发票号在同一张单据中出现多次即为重复报销（单据级规则）。"""
    index: dict[str, list[ExpenseItem]] = defaultdict(list)
    for item in report.items:
        if item.invoice_no:
            index[item.invoice_no.strip()].append(item)

    if not index:
        return []

    duplicated = {no: group for no, group in index.items() if len(group) > 1}
    if not duplicated:
        return [
            _mk(
                rule,
                "pass",
                f"共校验 {len(index)} 个发票号，未发现重复报销",
                evidence="、".join(sorted(index)[:8]) + ("…" if len(index) > 8 else ""),
            )
        ]

    verdicts = []
    for invoice_no, group in duplicated.items():
        total = round(sum(i.amount for i in group), 2)
        verdicts.append(
            _mk(
                rule,
                "fail",
                f"发票号 {invoice_no} 在单据中出现 {len(group)} 次，"
                f"涉及金额合计 {_money(total)} 元，疑似重复报销",
                evidence="、".join(f"{i.item_id}（{_money(i.amount)} 元）" for i in group),
                item_ids=[i.item_id for i in group],
            )
        )
    return verdicts


def check_require_field(rule: Rule, report: ExpenseReport) -> list[RuleVerdict]:
    """必填项校验。"""
    fields: list[str] = list(rule.params.get("fields") or [])
    min_len = int(rule.params.get("min_text_length", 0) or 0)

    verdicts = []
    for item in (i for i in report.items if rule.applies_to(i)):
        problems = []
        for field_name in fields:
            value = getattr(item, field_name, None)
            if value is None or (hasattr(value, "__len__") and len(value) == 0):
                problems.append(f"未填写「{field_name}」")
        if min_len and len(item.description.strip()) < min_len:
            problems.append(
                f"事由描述不足 {min_len} 字（当前 {len(item.description.strip())} 字）"
            )

        verdicts.append(
            _mk(
                rule,
                # 高危规则直接判 fail，中低危判 warn 转人工
                "fail" if problems and rule.severity == "high" else ("warn" if problems else "pass"),
                "；".join(problems) if problems else "必填项完整",
                evidence=f"{item.item_id}｜事由「{item.description}」",
                item_ids=[item.item_id],
            )
        )
    return verdicts


CHECKERS: dict[str, Callable[[Rule, ExpenseReport], list[RuleVerdict]]] = {
    "amount_limit": check_amount_limit,
    "trip_date_range": check_trip_date_range,
    "invoice_header": check_invoice_header,
    "duplicate_invoice": check_duplicate_invoice,
    "require_field": check_require_field,
}


def run_builtin(rule: Rule, report: ExpenseReport) -> list[RuleVerdict]:
    name = rule.evaluator.split(":", 1)[1] if ":" in rule.evaluator else rule.evaluator
    checker = CHECKERS.get(name)
    if checker is None:
        return [
            _mk(
                rule,
                "skipped",
                f"未注册的校验器「{name}」，请检查 rules.yaml 或在 checkers.py 中注册",
                confidence=0.0,
            )
        ]
    return checker(rule, report)
