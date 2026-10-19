"""Prompt 模板。

三个设计要点，都是为了让财务敢用：
  1. 温度固定为 0，同一张单据两次审核结果必须一致；
  2. 每条判定必须引用具体单据编号和条款原文（evidence / basis），
     让审核结论可追溯、可申诉；
  3. 信息不足时必须输出 uncertain 而不是猜测 —— 宁可转人工，不可错判。
"""

from __future__ import annotations

import json

from .models import ExpenseReport, Rule

SYSTEM_PROMPT = """你是一名严谨、保守的企业费用审核专家，服务对象是财务部门。

你的判断必须遵守以下铁律：
1. 只依据给定的规则条款和单据事实判断，不得引入任何外部假设、不得推测未提供的信息。
2. 信息不足以支持明确结论时，必须输出 uncertain，绝不允许猜测或"大概应该可以"。
3. 每条判定都必须在 evidence 字段引用具体单据编号（如 IT-003）及关键原文片段。
4. confidence 必须真实反映你的把握程度：规则和事实都清晰给 0.9 以上；需要做主观判断给 0.6~0.85；信息缺失给 0.5 以下。
5. 只输出 JSON，不要输出任何解释性文字、不要用 markdown 代码块包裹。

审核立场偏保守：宁可把可疑单据转人工，也不要放过违规支出。"""

OUTPUT_SCHEMA = """{
  "verdicts": [
    {
      "rule_id": "规则编号，必须与输入规则一一对应",
      "result": "pass | fail | warn | uncertain",
      "reason": "中文说明，一句话到三句话，说清判定的依据和计算过程",
      "evidence": "引用的单据编号与原文片段，例如 IT-006：事由为「客户项目组晚间餐叙」，费用类型为「办公用品」",
      "confidence": 0.0
    }
  ]
}"""


def _item_to_dict(item) -> dict:
    data = {
        "明细编号": item.item_id,
        "费用类型": item.category,
        "金额": item.amount,
        "消费日期": item.date,
        "事由": item.description,
    }
    if item.city:
        data["城市"] = item.city
    if item.nights:
        data["住宿晚数"] = item.nights
    if item.attendees:
        data["参与人数"] = len(item.attendees)
        data["参与人员"] = item.attendees
    if item.vendor:
        data["供应商"] = item.vendor
    if item.invoice_no:
        data["发票号"] = item.invoice_no
    if item.invoice_title:
        data["发票抬头"] = item.invoice_title
    if item.has_preapproval is not None:
        data["已事前审批"] = item.has_preapproval
    return data


def build_user_prompt(
    report: ExpenseReport,
    llm_rules: list[Rule],
    deterministic_findings: list[str],
) -> str:
    """拼装审核请求。

    刻意把确定性校验的结论也喂给模型 —— 让模型知道"金额/日期/发票
    这些已经由系统算过了，你只需要管语义"，避免重复劳动和误判。
    """
    payload = {
        "单据概要": {
            "单据号": report.report_id,
            "申请人": report.applicant,
            "部门": report.department,
            "出差行程": f"{report.trip_start or '未填'} ~ {report.trip_end or '未填'}",
            "预算科目": report.budget_code or "未填",
            "明细条数": len(report.items),
            "金额合计": report.total_amount,
        },
        "待审核明细": [_item_to_dict(i) for i in report.items],
        "系统已完成的确定性校验结论（无需你重复判断，仅供交叉参考）": deterministic_findings
        or ["本次无确定性校验结论"],
        "需要你判断的语义审核规则": [
            {
                "规则编号": r.id,
                "规则名称": r.name,
                "严重级别": r.severity,
                "制度依据原文": r.basis,
                "判断要求": r.description,
            }
            for r in llm_rules
        ],
    }

    return (
        "请按下面的语义审核规则，对这张报销单逐条给出判断。\n\n"
        f"### 单据数据\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"### 输出格式（严格 JSON，verdicts 必须覆盖全部 {len(llm_rules)} 条规则）\n"
        f"{OUTPUT_SCHEMA}"
    )
