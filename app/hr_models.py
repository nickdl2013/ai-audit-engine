"""HR 入职材料审核域的数据模型。

刻意与费用域共用同一套引擎骨架（规则库 YAML + 确定性校验 + 语义审核
+ 依据引用 + 决策分级 + 留痕），只替换「审核对象」这一层：

    费用域 → 一张报销单（ExpenseReport）
    HR 域  → 一个候选人的入职材料包（OnboardingRecord）

所以把它从费用换到 HR，改的是这一层 + 校验器 + Prompt，引擎本身不用动。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import Decision, RuleVerdict, TokenUsage


class OnboardingDoc(BaseModel):
    """一份入职材料。"""

    doc_id: str
    doc_type: str = Field(
        description="材料类型：身份证 / 学历证书 / 离职证明 / 体检报告 / 银行卡 / 劳动合同 …"
    )
    submitted: bool = Field(default=True, description="是否已实际收到并提交，false 视为缺失")
    file_name: str | None = None
    issue_date: str | None = Field(default=None, description="签发 / 出具日期 YYYY-MM-DD")
    expiry_date: str | None = Field(default=None, description="有效期至 YYYY-MM-DD")
    holder_name: str | None = Field(default=None, description="材料上的姓名")
    holder_id_no: str | None = Field(default=None, description="材料上的证件号码")
    issuer: str | None = Field(default=None, description="出具单位")
    description: str | None = Field(
        default=None, description="备注 / 说明，是语义审核的主要输入"
    )

    @property
    def category(self) -> str:
        """让通用规则匹配逻辑（Rule.applies_to）可以直接复用，无需改引擎。"""
        return self.doc_type


class OnboardingRecord(BaseModel):
    """一个候选人的入职材料包。"""

    record_id: str
    candidate_name: str = ""
    id_no: str | None = Field(default=None, description="登记身份证号")
    department: str = ""
    position: str = ""
    employment_type: str = Field(
        default="全职", description="用工形式：全职 / 实习 / 外包 / 劳务"
    )
    offer_date: str | None = None
    onboard_date: str | None = Field(default=None, description="拟入职日期")
    is_fresh_graduate: bool = Field(default=False, description="是否应届毕业生")
    work_years: float | None = None
    highest_degree: str | None = None
    docs: list[OnboardingDoc] = Field(default_factory=list)


class OnboardingAuditResult(BaseModel):
    """一份入职材料包的完整审核结果。"""

    record_id: str
    decision: Decision
    summary: str
    candidate_name: str = ""
    department: str = ""
    position: str = ""
    employment_type: str = ""
    onboard_date: str | None = None
    doc_count: int = 0
    submitted_count: int = 0
    verdicts: list[RuleVerdict] = Field(default_factory=list)
    rules_version: str = ""
    model: str = ""
    mock: bool = False
    elapsed_ms: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    created_at: str = ""

    def count(self, result) -> int:
        return sum(1 for v in self.verdicts if v.result == result)
