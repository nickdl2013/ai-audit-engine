"""批量审核：Excel 进，Excel 出。

这是面向"真实投入使用"的第一种形态，刻意不做网页、不做服务、不做部署：

    HR 平时就在 Excel 里维护花名册和材料清单
    → 直接把这份 Excel 喂进来
    → 吐出另一份 Excel：《合规检查表》

为什么不做网页版：任何新 URL 都是一道门槛。HR 不会为了查材料合规性去打开一个陌生网址，
但会打开一个别人发来的 xlsx 附件。**先让工具出现在他们已经在用的地方。**

用法：
    # 1. 生成空白模板（含填写说明与示例行）
    python batch_audit.py --init-template 入职材料审核_模板.xlsx

    # 2. 填好后批量审核
    python batch_audit.py 入职材料审核_模板.xlsx
    python batch_audit.py 入职材料.xlsx -o 10月校招_检查结果.xlsx

    # 3. 换规则库
    python batch_audit.py 入职材料.xlsx -r config/rules_hr.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.config import settings  # noqa: E402
from app.hr_audit import HRAuditEngine  # noqa: E402
from app.hr_models import OnboardingDoc, OnboardingRecord  # noqa: E402
from app.models import RuleVerdict  # noqa: E402
from app.rules import load_rules  # noqa: E402

DEFAULT_RULES = BASE_DIR / "config" / "rules_hr.yaml"

SHEET_PERSON = "人员"
SHEET_DOC = "材料"
SHEET_SUMMARY = "汇总"
SHEET_ISSUES = "问题明细"
SHEET_STATS = "统计"
SHEET_HELP = "填写说明"

# 列定义：(表头, 字段名, 是否必填, 说明)
PERSON_FIELDS: list[tuple[str, str, bool, str]] = [
    ("记录号", "record_id", True, "唯一标识，如 RZ-2026-1102-018。两份表靠它对上"),
    ("姓名", "candidate_name", True, "会与材料上的姓名核对，须完全一致"),
    ("身份证号", "id_no", True, "18 位。会与材料上的证件号核对"),
    ("部门", "department", False, "如 产品中心"),
    ("岗位", "position", False, "如 高级产品经理"),
    ("用工形式", "employment_type", True, "全职 / 实习 / 外包 / 劳务（四选一，决定必备材料清单）"),
    ("offer日期", "offer_date", False, "YYYY-MM-DD。拟入职日期不得早于它"),
    ("拟入职日期", "onboard_date", True, "YYYY-MM-DD。多项时效规则以它为基准"),
    ("是否应届生", "is_fresh_graduate", False, "是 / 否。填「是」则免交离职证明"),
    ("最高学历", "highest_degree", False, "如 本科 / 硕士"),
    ("工作年限", "work_years", False, "数字，如 5"),
]

DOC_FIELDS: list[tuple[str, str, bool, str]] = [
    ("记录号", "record_id", True, "对应「人员」表的记录号，一个候选人可有多行"),
    (
        "材料类型",
        "doc_type",
        True,
        "身份证 / 学历证书 / 离职证明 / 体检报告 / 银行卡 / 劳动合同 / "
        "学生证 / 实习协议 / 劳务协议。名字必须与规则库一致，否则会被判为缺失",
    ),
    ("是否已提交", "submitted", True, "是 / 否。填「否」等同于缺失"),
    ("签发日期", "issue_date", False, "YYYY-MM-DD。体检报告需入职前 90 日内；离职证明不得晚于入职日"),
    ("有效期至", "expiry_date", False, "YYYY-MM-DD。身份证须覆盖入职日并留 30 天"),
    ("材料上姓名", "holder_name", False, "用于姓名一致性核对"),
    ("材料上证件号", "holder_id_no", False, "用于身份证号一致性核对"),
    ("出具单位", "issuer", False, "如 某某医院 / 某某大学"),
    ("备注", "description", False, "如「仅提供复印件，原件未交」——语义规则会读这一列"),
]

DECISION_LABEL = {
    "APPROVE": "可办理入职",
    "MANUAL_REVIEW": "转人工复核",
    "REJECT": "退回补正",
}

DECISION_FILL = {
    "APPROVE": "C6EFCE",
    "MANUAL_REVIEW": "FFEB9C",
    "REJECT": "FFC7CE",
}

DECISION_FONT = {
    "APPROVE": "1E6B33",
    "MANUAL_REVIEW": "7F6000",
    "REJECT": "9C0006",
}

RESULT_LABEL = {"fail": "不通过", "warn": "预警", "uncertain": "待人工", "pass": "通过"}

SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


# ---------------------------------------------------------------- 解析


def _norm_header(value: Any) -> str:
    return str(value or "").strip().replace(" ", "")


def _to_date_str(value: Any) -> str | None:
    """把 Excel 里各种写法的日期统一成 YYYY-MM-DD。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    raw = str(value).strip()
    if not raw:
        return None

    raw = raw.replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # 认不出来就原样传下去，让校验器报"日期格式不合法"


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"是", "y", "yes", "true", "1", "√", "已提交", "有"}


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _read_sheet(ws, fields: list[tuple[str, str, bool, str]]) -> list[dict[str, Any]]:
    """按表头文字取列，容忍多空列、少列、表头前后空格。"""
    header = [_norm_header(c) for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    index: dict[str, int] = {}
    for name, field, _required, _hint in fields:
        key = _norm_header(name)
        if key in header:
            index[field] = header.index(key)

    rows: list[dict[str, Any]] = []
    for raw in ws.iter_rows(min_row=2, values_only=True):
        if raw is None or all(c is None or str(c).strip() == "" for c in raw):
            continue
        row: dict[str, Any] = {}
        for field, idx in index.items():
            row[field] = raw[idx] if idx < len(raw) else None
        rows.append(row)
    return rows


def load_records(path: Path) -> tuple[list[OnboardingRecord], list[str]]:
    wb = load_workbook(path, data_only=True)
    warnings: list[str] = []

    if SHEET_PERSON not in wb.sheetnames:
        raise SystemExit(f"缺少工作表「{SHEET_PERSON}」，请用 --init-template 生成模板")

    persons = _read_sheet(wb[SHEET_PERSON], PERSON_FIELDS)
    docs_rows = _read_sheet(wb[SHEET_DOC], DOC_FIELDS) if SHEET_DOC in wb.sheetnames else []

    docs_by_record: dict[str, list[OnboardingDoc]] = {}
    known = {str(p.get("record_id") or "").strip() for p in persons}
    for i, row in enumerate(docs_rows, start=2):
        rid = str(row.get("record_id") or "").strip()
        if not rid:
            warnings.append(f"材料表第 {i} 行缺少记录号，已跳过")
            continue
        if rid not in known:
            warnings.append(f"材料表第 {i} 行的记录号「{rid}」在人员表中不存在，已跳过")
            continue
        doc_type = str(row.get("doc_type") or "").strip()
        if not doc_type:
            warnings.append(f"材料表第 {i} 行缺少材料类型，已跳过")
            continue
        docs_by_record.setdefault(rid, []).append(
            OnboardingDoc(
                doc_id=f"{rid}-DOC{len(docs_by_record.get(rid, [])) + 1:02d}",
                doc_type=doc_type,
                submitted=_to_bool(row.get("submitted"), default=True),
                issue_date=_to_date_str(row.get("issue_date")),
                expiry_date=_to_date_str(row.get("expiry_date")),
                holder_name=(str(row.get("holder_name")).strip() if row.get("holder_name") else None),
                holder_id_no=(
                    str(row.get("holder_id_no")).strip() if row.get("holder_id_no") else None
                ),
                issuer=(str(row.get("issuer")).strip() if row.get("issuer") else None),
                description=(
                    str(row.get("description")).strip() if row.get("description") else None
                ),
            )
        )

    records: list[OnboardingRecord] = []
    for i, row in enumerate(persons, start=2):
        rid = str(row.get("record_id") or "").strip()
        if not rid:
            warnings.append(f"人员表第 {i} 行缺少记录号，已跳过")
            continue
        if rid in {r.record_id for r in records}:
            warnings.append(f"人员表第 {i} 行的记录号「{rid}」重复，只保留第一行")
            continue
        records.append(
            OnboardingRecord(
                record_id=rid,
                candidate_name=str(row.get("candidate_name") or "").strip(),
                id_no=(str(row.get("id_no")).strip() if row.get("id_no") else None),
                department=str(row.get("department") or "").strip(),
                position=str(row.get("position") or "").strip(),
                employment_type=str(row.get("employment_type") or "全职").strip() or "全职",
                offer_date=_to_date_str(row.get("offer_date")),
                onboard_date=_to_date_str(row.get("onboard_date")),
                is_fresh_graduate=_to_bool(row.get("is_fresh_graduate")),
                highest_degree=(
                    str(row.get("highest_degree")).strip() if row.get("highest_degree") else None
                ),
                work_years=_to_float(row.get("work_years")),
                docs=docs_by_record.get(rid, []),
            )
        )

    if not records:
        raise SystemExit("人员表里没有读到任何有效记录")
    return records, warnings


# ---------------------------------------------------------------- 写结果


def _style_header(ws, widths: list[int]) -> None:
    for col, width in enumerate(widths, start=1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"


def _auto_width(ws, widths: list[int], cap: int = 60) -> None:
    for col, width in enumerate(widths, start=1):
        longest = width
        for cell in ws[get_column_letter(col)]:
            if cell.value is None:
                continue
            text = str(cell.value)
            length = sum(2 if ord(ch) > 127 else 1 for ch in text)
            longest = max(longest, min(length + 2, cap))
        ws.column_dimensions[get_column_letter(col)].width = min(longest, cap)


def _high_count(verdicts: list[RuleVerdict]) -> int:
    return sum(1 for v in verdicts if v.result == "fail" and v.severity == "high")


def write_result(
    path: Path,
    results,
    records: dict[str, OnboardingRecord],
    elapsed_total: float,
) -> None:
    wb = Workbook()

    # ---------- 汇总 ----------
    ws = wb.active
    ws.title = SHEET_SUMMARY
    summary_headers = [
        "记录号", "姓名", "部门", "岗位", "用工形式", "拟入职日期",
        "材料份数", "已提交", "结论", "高危", "一般", "预警", "待人工", "审核说明",
    ]
    ws.append(summary_headers)
    widths = [20, 10, 12, 16, 10, 13, 10, 9, 14, 7, 7, 7, 8, 70]
    _style_header(ws, widths)

    for r in results:
        fails = [v for v in r.verdicts if v.result == "fail"]
        ws.append(
            [
                r.record_id,
                r.candidate_name,
                r.department,
                r.position,
                r.employment_type,
                r.onboard_date or "",
                r.doc_count,
                r.submitted_count,
                DECISION_LABEL.get(r.decision, r.decision),
                sum(1 for v in fails if v.severity == "high"),
                sum(1 for v in fails if v.severity == "medium"),
                sum(1 for v in r.verdicts if v.result == "warn"),
                sum(1 for v in r.verdicts if v.result == "uncertain"),
                r.summary,
            ]
        )
        row = ws.max_row
        fill = DECISION_FILL.get(r.decision, "FFFFFF")
        ws.cell(row=row, column=9).fill = PatternFill("solid", fgColor=fill)
        ws.cell(row=row, column=9).font = Font(bold=True, color=DECISION_FONT.get(r.decision, "000000"))
        ws.cell(row=row, column=14).alignment = Alignment(wrap_text=True, vertical="top")
        for col in range(1, len(summary_headers) + 1):
            ws.cell(row=row, column=col).border = BORDER
    _auto_width(ws, widths)

    # ---------- 问题明细 ----------
    ws2 = wb.create_sheet(SHEET_ISSUES)
    issue_headers = [
        "记录号", "姓名", "材料类型", "规则编号", "规则名称",
        "结果", "严重度", "说明", "证据", "依据", "置信度", "来源",
    ]
    ws2.append(issue_headers)
    w2 = [20, 10, 12, 13, 24, 8, 8, 52, 46, 46, 9, 13]
    _style_header(ws2, w2)

    for r in results:
        doc_types = {d.doc_id: d.doc_type for d in (records.get(r.record_id).docs if records.get(r.record_id) else [])}
        for v in r.verdicts:
            if v.result == "pass":
                continue
            subjects = "、".join(doc_types.get(i, i) for i in v.item_ids) if v.item_ids else "材料包整体"
            ws2.append(
                [
                    r.record_id,
                    r.candidate_name,
                    subjects,
                    v.rule_id,
                    v.rule_name,
                    RESULT_LABEL.get(v.result, v.result),
                    SEVERITY_LABEL.get(v.severity, v.severity),
                    v.reason,
                    v.evidence or "",
                    v.basis or "",
                    round(v.confidence, 2),
                    v.source,
                ]
            )
            row = ws2.max_row
            if v.result == "fail" and v.severity == "high":
                ws2.cell(row=row, column=6).fill = PatternFill("solid", fgColor="FFC7CE")
                ws2.cell(row=row, column=6).font = Font(bold=True, color="9C0006")
            elif v.result == "warn":
                ws2.cell(row=row, column=6).fill = PatternFill("solid", fgColor="FFEB9C")
            elif v.result == "uncertain":
                ws2.cell(row=row, column=6).fill = PatternFill("solid", fgColor="DDEBF7")
            for col in (8, 9, 10):
                ws2.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
            for col in range(1, len(issue_headers) + 1):
                ws2.cell(row=row, column=col).border = BORDER
    _auto_width(ws2, w2)

    # ---------- 统计 ----------
    ws3 = wb.create_sheet(SHEET_STATS)
    total = len(results)
    approve = sum(1 for r in results if r.decision == "APPROVE")
    review = sum(1 for r in results if r.decision == "MANUAL_REVIEW")
    reject = sum(1 for r in results if r.decision == "REJECT")

    ws3.append(["审核概况", "数值"])
    _style_header(ws3, [26, 18])
    for label, value in [
        ("材料包总数", total),
        ("可办理入职", approve),
        ("转人工复核", review),
        ("退回补正", reject),
        ("需要处理的占比", f"{(total - approve) / total:.0%}" if total else "0%"),
        ("规则库版本", results[0].rules_version if results else ""),
        ("模型", results[0].model if results else ""),
        ("运行模式", "mock 模拟（未接真实模型）" if (results and results[0].mock) else "真实模型调用"),
        ("总耗时(秒)", round(elapsed_total, 1)),
    ]:
        ws3.append([label, value])

    ws3.append([])
    ws3.append(["规则命中排行", "不通过", "预警", "待人工", "命中合计"])
    hdr_row = ws3.max_row
    for col in range(1, 6):
        cell = ws3.cell(row=hdr_row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")

    tally: dict[str, dict[str, Any]] = {}
    for r in results:
        for v in r.verdicts:
            if v.result == "pass":
                continue
            item = tally.setdefault(v.rule_id, {"name": v.rule_name, "fail": 0, "warn": 0, "uncertain": 0})
            if v.result == "fail":
                item["fail"] += 1
            elif v.result == "warn":
                item["warn"] += 1
            elif v.result == "uncertain":
                item["uncertain"] += 1

    for rid, item in sorted(
        tally.items(), key=lambda kv: (-(kv[1]["fail"] + kv[1]["warn"] + kv[1]["uncertain"]), kv[0])
    ):
        ws3.append(
            [
                f"{rid} {item['name']}",
                item["fail"],
                item["warn"],
                item["uncertain"],
                item["fail"] + item["warn"] + item["uncertain"],
            ]
        )
    _auto_width(ws3, [40, 10, 10, 10, 12])

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# ---------------------------------------------------------------- 模板


def build_template(path: Path) -> None:
    wb = Workbook()

    ws = wb.active
    ws.title = SHEET_PERSON
    ws.append([f[0] for f in PERSON_FIELDS])
    _style_header(ws, [20, 10, 22, 12, 16, 12, 13, 13, 11, 10, 10])
    ws.append(
        [
            "RZ-2026-1102-018", "李思远", "310101199008123017", "产品中心", "高级产品经理",
            "全职", "2026-10-20", "2026-11-02", "否", "本科", 8,
        ]
    )
    ws.append(
        [
            "RZ-2026-1102-019", "周雨桐", "330106200203145621", "市场部", "市场专员",
            "全职", "2026-10-21", "2026-11-05", "是", "硕士", "",
        ]
    )
    ws.append(
        [
            "RZ-2026-1102-020", "王皓", "440305199510208834", "技术中心", "前端实习生",
            "实习", "2026-10-22", "2026-11-08", "否", "本科", "",
        ]
    )

    ws2 = wb.create_sheet(SHEET_DOC)
    ws2.append([f[0] for f in DOC_FIELDS])
    _style_header(ws2, [20, 12, 11, 13, 13, 12, 22, 14, 30])
    demo = [
        ("RZ-2026-1102-018", "身份证", "是", "2019-03-01", "2039-03-01", "李思远", "310101199008123017", "某某市公安局", ""),
        ("RZ-2026-1102-018", "学历证书", "是", "2013-07-01", None, "李思远", "310101199008123170", "某某大学", "仅提供复印件，原件未交"),
        ("RZ-2026-1102-018", "体检报告", "是", "2026-06-15", None, "李思远", None, "某某医院", ""),
        ("RZ-2026-1102-018", "银行卡", "是", None, None, None, None, "某银行", ""),
        ("RZ-2026-1102-019", "身份证", "是", "2022-05-01", "2042-05-01", "周雨桐", "330106200203145621", "某某市公安局", ""),
        ("RZ-2026-1102-019", "学历证书", "是", "2025-06-30", None, "周雨桐", "330106200203145621", "某某大学", ""),
        ("RZ-2026-1102-019", "体检报告", "是", "2026-10-01", None, "周雨桐", None, "某某医院", ""),
        ("RZ-2026-1102-019", "银行卡", "是", None, None, None, None, "某银行", ""),
        ("RZ-2026-1102-019", "劳动合同", "是", None, None, None, None, "示例科技有限公司", "待签字"),
        ("RZ-2026-1102-020", "身份证", "是", "2023-09-01", "2043-09-01", "王皓", "440305199510208834", "某某市公安局", ""),
        ("RZ-2026-1102-020", "学生证", "是", None, None, "王皓", None, "某某大学", ""),
        ("RZ-2026-1102-020", "实习协议", "是", None, None, None, None, "示例科技有限公司", ""),
    ]
    for row in demo:
        ws2.append(list(row))

    ws3 = wb.create_sheet(SHEET_HELP)
    ws3.append(["字段", "是否必填", "填写说明"])
    _style_header(ws3, [16, 11, 96])
    for name, _field, required, hint in PERSON_FIELDS:
        ws3.append([f"{SHEET_PERSON} · {name}", "必填" if required else "选填", hint])
    for name, _field, required, hint in DOC_FIELDS:
        ws3.append([f"{SHEET_DOC} · {name}", "必填" if required else "选填", hint])

    ws3.append([])
    notes = [
        ["三个必须注意的地方", "", ""],
        ["1", "", "材料类型必须和规则库里的名字一字不差，否则会被判成「缺失」。常用：身份证 / 学历证书 / 离职证明 / 体检报告 / 银行卡 / 劳动合同 / 学生证 / 实习协议 / 劳务协议"],
        ["2", "", "用工形式决定必备材料清单：全职 6 项、实习 4 项、外包 2 项、劳务 3 项。填错会直接改变审核结论"],
        ["3", "", "「是否应届生」填「是」会自动免交离职证明。这一条不填，校招材料会全部被判不合规"],
        ["", "", ""],
        ["隐私提醒", "", "本表含身份证号等个人信息。只在公司内网使用，不要用真实材料做演示，对外分享前先脱敏"],
    ]
    for row in notes:
        ws3.append(row)
    for r in range(ws3.max_row - len(notes), ws3.max_row + 1):
        ws3.cell(row=r, column=3).alignment = Alignment(wrap_text=True, vertical="top")

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# ---------------------------------------------------------------- 控制台


def print_report(results, elapsed: float) -> None:
    total = len(results)
    approve = sum(1 for r in results if r.decision == "APPROVE")
    review = sum(1 for r in results if r.decision == "MANUAL_REVIEW")
    reject = sum(1 for r in results if r.decision == "REJECT")

    print()
    print("=" * 78)
    print(f"批量审核完成｜材料包 {total} 份｜耗时 {elapsed:.1f}s")
    print("=" * 78)
    print(f"  可办理入职  {approve:>4} 份")
    print(f"  转人工复核  {review:>4} 份")
    print(f"  退回补正  {reject:>4} 份")
    if total:
        print(f"  需要处理    {total - approve:>4} 份（{(total - approve) / total:.0%}）")
    print("-" * 78)

    tally: dict[str, dict[str, Any]] = {}
    for r in results:
        for v in r.verdicts:
            if v.result == "pass":
                continue
            item = tally.setdefault(v.rule_id, {"name": v.rule_name, "n": 0})
            item["n"] += 1

    if tally:
        print("命中最多的问题：")
        for rid, item in sorted(tally.items(), key=lambda kv: -kv[1]["n"])[:6]:
            print(f"  {item['n']:>3} 次  {rid}  {item['name']}")
    print("=" * 78)

    for r in results:
        flag = {"APPROVE": "OK  ", "MANUAL_REVIEW": "复核", "REJECT": "退回"}.get(r.decision, "?")
        fails = [v for v in r.verdicts if v.result == "fail"]
        warns = [v for v in r.verdicts if v.result in ("warn", "uncertain")]
        if fails:
            detail = "、".join(v.rule_name for v in fails[:3])
            if len(fails) > 3:
                detail += f" 等 {len(fails)} 项"
        elif warns:
            detail = "预警 " + "、".join(v.rule_name for v in warns[:2])
            if len(warns) > 2:
                detail += f" 等 {len(warns)} 项"
        else:
            detail = "未发现不合规"
        print(f"  [{flag}] {r.record_id:<22} {r.candidate_name:<8} {detail}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HR 入职材料批量合规审核：Excel 进，Excel 出",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="待审核的 Excel 文件")
    parser.add_argument("-o", "--output", help="结果文件路径，默认 <输入名>_审核结果.xlsx")
    parser.add_argument("-r", "--rules", default=str(DEFAULT_RULES), help="规则库 YAML 路径")
    parser.add_argument("--init-template", metavar="PATH", help="生成空白模板后退出")
    parser.add_argument("--from", dest="start", help="只审核记录号 >= 该值的记录")
    parser.add_argument("--to", dest="end", help="只审核记录号 <= 该值的记录")
    args = parser.parse_args()

    if args.init_template:
        target = Path(args.init_template)
        build_template(target)
        print(f"\n模板已生成：{target}")
        print("「人员」和「材料」两张表各有一个示例行，照着改就行。填写说明在第三个标签页。\n")
        return 0

    if not args.input:
        parser.print_help()
        return 1

    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"文件不存在：{src}")

    out = Path(args.output) if args.output else src.with_name(f"{src.stem}_审核结果.xlsx")

    records, warnings = load_records(src)
    for w in warnings:
        print(f"提醒：{w}")
    if args.start:
        records = [r for r in records if r.record_id >= args.start]
    if args.end:
        records = [r for r in records if r.record_id <= args.end]
    if not records:
        raise SystemExit("筛选后没有可审核的记录")

    ruleset = load_rules(Path(args.rules))
    engine = HRAuditEngine(ruleset, settings)

    print(f"规则库 {ruleset.version}｜确定性 {len(ruleset.builtin_rules)} 条｜"
          f"语义 {len(ruleset.llm_rules)} 条｜模型 {settings.model}"
          f"{'（mock 模拟）' if settings.mock else ''}")
    print(f"待审核 {len(records)} 份材料包…")

    async def _run():
        return [await engine.audit(r) for r in records]

    import time

    started = time.perf_counter()
    results = asyncio.run(_run())
    elapsed = time.perf_counter() - started

    write_result(out, results, {r.record_id: r for r in records}, elapsed)
    print_report(results, elapsed)
    print(f"结果已写入：{out}")
    print(f"{SHEET_SUMMARY}：每人一行结论　{SHEET_ISSUES}：每条问题一行，带证据与依据　"
          f"{SHEET_STATS}：整体统计与规则命中排行\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
