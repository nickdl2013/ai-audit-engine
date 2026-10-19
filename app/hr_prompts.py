"""HR 入职材料审核的 Prompt 模板与内置模拟模型。

与费用域的 Prompt 结构完全一致（同样的 JSON 契约、同样的依据强制、
同样的保守立场），只换角色设定和字段。这样模型侧的调参经验可以直接迁移。

三个设计要点，都是为了让 HR 敢用：
  1. 温度固定为 0，同一份材料包两次审核结果必须一致；
  2. 每条判定必须引用具体材料编号与条款原文（evidence / basis），可申诉；
  3. 信息不足时必须输出 uncertain 而不是猜测 —— 宁可转人工，不可错判。
"""

from __future__ import annotations

import json

from .hr_models import OnboardingRecord
from .models import Rule

HR_SYSTEM_PROMPT = """你是一名严谨、保守的企业入离职材料审核专员，服务对象是人力资源部门。

你的判断必须遵守以下铁律：
1. 只依据给定的规则条款和材料事实判断，不得引入任何外部假设、不得推测未提供的信息。
2. 信息不足以支持明确结论时，必须输出 uncertain，绝不允许猜测或"大概可以"。
3. 每条判定都必须在 evidence 字段引用具体材料编号（如 DOC-003）及关键原文片段。
4. confidence 必须真实反映你的把握程度：规则和事实都清晰给 0.9 以上；需要做主观判断给 0.6~0.85；信息缺失给 0.5 以下。
5. 只输出 JSON，不要输出任何解释性文字、不要用 markdown 代码块包裹。

审核立场偏保守：宁可把可疑材料转人工，也不要放过合规风险。
入职材料直接关系劳动关系成立、社保缴纳与后续争议举证，
一旦错放，解除劳动合同、双重劳动关系、背调争议的责任都会落到 HR 头上。"""

HR_OUTPUT_SCHEMA = """{
  "verdicts": [
    {
      "rule_id": "规则编号，必须与输入规则一一对应",
      "result": "pass | fail | warn | uncertain",
      "reason": "中文说明，一句话到三句话，说清判定的依据",
      "evidence": "引用的材料编号与原文片段，例如 DOC-002 学历证书：备注为「仅提供复印件，原件未交」",
      "confidence": 0.0
    }
  ]
}"""


def _doc_to_dict(doc) -> dict:
    data = {
        "材料编号": doc.doc_id,
        "材料类型": doc.doc_type,
        "是否已提交": doc.submitted,
    }
    if doc.holder_name:
        data["材料姓名"] = doc.holder_name
    if doc.holder_id_no:
        data["材料证件号"] = doc.holder_id_no
    if doc.issue_date:
        data["出具日期"] = doc.issue_date
    if doc.expiry_date:
        data["有效期至"] = doc.expiry_date
    if doc.issuer:
        data["出具单位"] = doc.issuer
    if doc.description:
        data["备注"] = doc.description
    return data


def build_hr_user_prompt(
    record: OnboardingRecord,
    llm_rules: list[Rule],
    deterministic_findings: list[str],
) -> str:
    """拼装审核请求。

    同样把确定性校验的结论喂给模型 —— 让它知道"材料齐全性、日期、证件号
    这些已经由系统算过了，你只需要管语义"，避免重复劳动和误判。
    """
    payload = {
        "候选人概要": {
            "材料包编号": record.record_id,
            "姓名": record.candidate_name,
            "身份证号": record.id_no or "未填",
            "部门": record.department,
            "岗位": record.position,
            "用工形式": record.employment_type,
            "offer 日期": record.offer_date or "未填",
            "拟入职日期": record.onboard_date or "未填",
            "是否应届毕业生": record.is_fresh_graduate,
            "自报工作年限": record.work_years if record.work_years is not None else "未填",
            "最高学历": record.highest_degree or "未填",
            "材料份数": len(record.docs),
        },
        "待审核材料": [_doc_to_dict(d) for d in record.docs],
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
        "请按下面的语义审核规则，对这个入职材料包逐条给出判断。\n\n"
        f"### 材料数据\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"### 输出格式（严格 JSON，verdicts 必须覆盖全部 {len(llm_rules)} 条规则）\n"
        f"{HR_OUTPUT_SCHEMA}"
    )


# --------------------------------------------------------------------------
# 内置模拟模型：不联网、不需要 Key，只为了让演示链路跑通
# --------------------------------------------------------------------------

_RISK_WORDS = (
    "复印件",
    "原件未交",
    "原件未提供",
    "原件未",
    "待补",
    "后补",
    "待签",
    "待盖章",
    "待用印",
    "口头",
    "未提供",
    "暂缺",
    "不清晰",
    "模糊",
)


def hr_mock_judge(context: dict) -> dict:
    """内置假模型。

    它的判断逻辑是几个关键词与字段规则，只为了演示链路跑通，
    不代表真实模型能力。真实能力请把 LLM_PROVIDER 换成 deepseek / qwen / ollama。
    """
    docs: list[dict] = context.get("docs", [])
    rules: list[dict] = context.get("rules", [])
    rec: dict = context.get("record", {}) or {}
    verdicts: list[dict] = []

    for rule in rules:
        rule_id = rule.get("rule_id", "")
        name = rule.get("name", "")

        # 材料构成与用工形式适配性
        if "适配" in name or "用工形式" in name:
            employment = str(rec.get("employment_type") or "")
            types = {str(d.get("doc_type") or "") for d in docs}
            problems: list[str] = []
            if employment == "实习" and "劳动合同" in types and "实习协议" not in types:
                problems.append(
                    "实习岗位提交的是劳动合同，与用工形式不符，应为实习协议"
                )
            if employment == "外包" and "劳动合同" in types:
                problems.append("外包人员的劳动合同主体应为服务商，不应由公司直接签署")
            if employment == "全职" and "实习协议" in types:
                problems.append("全职岗位提交实习协议，与用工形式不符")

            if problems:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "warn",
                        "reason": "；".join(problems)
                        + "。用工形式与合同类型不匹配会直接影响劳动关系认定，建议确认后再办理。",
                        "evidence": f"用工形式「{employment}」｜已提交材料：{'、'.join(sorted(types)) or '无'}",
                        "confidence": 0.8,
                    }
                )
            else:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "pass",
                        "reason": f"材料构成与「{employment}」用工形式匹配。",
                        "evidence": f"共核查 {len(docs)} 份材料，用工形式「{employment}」",
                        "confidence": 0.85,
                    }
                )

        # 备注中的证据瑕疵与风险信号
        elif "风险信号" in name or "瑕疵" in name:
            hits: list[tuple[dict, list[str]]] = []
            for doc in docs:
                desc = str(doc.get("description") or "")
                matched = [w for w in _RISK_WORDS if w in desc]
                if matched:
                    hits.append((doc, matched))
            if hits:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "warn",
                        "reason": "部分材料的备注中存在证据瑕疵表述，材料效力可能不足，"
                        "建议要求补交原件或补充书面说明后再归档。",
                        "evidence": "；".join(
                            f"{h[0].get('doc_id')} {h[0].get('doc_type')}：备注「{h[0].get('description')}」"
                            f"，命中 {h[1]}"
                            for h in hits
                        ),
                        "confidence": 0.82,
                    }
                )
            else:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "pass",
                        "reason": "材料备注中未发现证据瑕疵表述。",
                        "evidence": f"共核查 {len(docs)} 份材料的备注字段",
                        "confidence": 0.86,
                    }
                )

        # 候选人信息内部一致性
        elif "一致性" in name:
            problems = []
            if rec.get("is_fresh_graduate") and (rec.get("work_years") or 0) > 1:
                problems.append(
                    f"标记为应届毕业生，但自报工作年限 {rec.get('work_years')} 年"
                )
            degree = rec.get("highest_degree")
            position = str(rec.get("position") or "")
            if position.startswith("高级") and degree in (None, "", "高中", "中专"):
                problems.append(
                    f"岗位「{position}」与最高学历「{degree or '未填'}」不相称"
                )
            if problems:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "warn",
                        "reason": "；".join(problems) + "。建议核对后再办理入职手续。",
                        "evidence": "；".join(problems),
                        "confidence": 0.7,
                    }
                )
            else:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "pass",
                        "reason": "候选人自报信息与所交材料之间未发现矛盾。",
                        "evidence": (
                            f"应届生 {rec.get('is_fresh_graduate')}"
                            f"｜工作年限 {rec.get('work_years')}"
                            f"｜最高学历 {rec.get('highest_degree') or '未填'}"
                        ),
                        "confidence": 0.8,
                    }
                )

        else:
            verdicts.append(
                {
                    "rule_id": rule_id,
                    "result": "uncertain",
                    "reason": "内置模拟模型无法处理该规则类型，真实模型不受此限制。",
                    "evidence": None,
                    "confidence": 0.3,
                }
            )

    return {"verdicts": verdicts}
