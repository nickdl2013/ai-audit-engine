"""数据模型。

单据结构刻意贴近国内 OA/费控系统的实际字段（用友、泛微、金蝶、
自研系统的报销单基本都能映射到这组字段）。对接真实系统时，
在 adapter 层做一次字段映射即可，审核逻辑不用动。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["high", "medium", "low"]
VerdictResult = Literal["pass", "fail", "warn", "uncertain", "skipped"]
Decision = Literal["APPROVE", "REJECT", "MANUAL_REVIEW"]


class ExpenseItem(BaseModel):
    """一条报销明细。"""

    item_id: str
    category: str = Field(description="费用类型，如 住宿费 / 市内交通费 / 招待费")
    amount: float
    date: str = Field(description="消费发生日期 YYYY-MM-DD")
    description: str = Field(default="", description="报销事由，语义审核的主要输入")
    city: str | None = None
    nights: int | None = None
    attendees: list[str] | None = None
    vendor: str | None = None
    invoice_no: str | None = None
    invoice_title: str | None = None
    invoice_tax_no: str | None = None
    has_preapproval: bool | None = None


class ExpenseReport(BaseModel):
    """一张报销单。"""

    report_id: str
    applicant: str = ""
    department: str = ""
    trip_start: str | None = None
    trip_end: str | None = None
    budget_code: str | None = None
    items: list[ExpenseItem] = Field(default_factory=list)

    @property
    def total_amount(self) -> float:
        return round(sum(i.amount for i in self.items), 2)


class Rule(BaseModel):
    """一条审核规则，从 rules.yaml 加载。"""

    id: str
    name: str
    severity: Severity = "medium"
    scope: Literal["item", "report"] = "item"
    evaluator: str = Field(description="builtin:xxx 或 llm:semantic")
    basis: str = Field(default="", description="依据的制度条款原文，必须随判定一起输出")
    description: str = ""
    category: str | None = None
    enabled: bool = True
    applies_to_categories: list[str] | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_llm(self) -> bool:
        return self.evaluator.startswith("llm:")

    def applies_to(self, item: ExpenseItem) -> bool:
        if self.applies_to_categories is None:
            return True
        return item.category in self.applies_to_categories


class RuleVerdict(BaseModel):
    """一条规则的判定结果。basis 和 evidence 是让财务敢签字的关键字段。"""

    rule_id: str
    rule_name: str
    severity: Severity
    result: VerdictResult
    reason: str = ""
    evidence: str | None = Field(default=None, description="引用的具体单据字段或原文片段")
    basis: str = Field(default="", description="依据条款原文")
    confidence: float = 1.0
    source: Literal["deterministic", "llm"] = "deterministic"
    item_ids: list[str] = Field(default_factory=list)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class AuditResult(BaseModel):
    """一张单据的完整审核结果。"""

    report_id: str
    decision: Decision
    summary: str
    applicant: str = ""
    department: str = ""
    total_amount: float = 0.0
    item_count: int = 0
    verdicts: list[RuleVerdict] = Field(default_factory=list)
    rules_version: str = ""
    model: str = ""
    mock: bool = False
    elapsed_ms: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    created_at: str = ""

    def count(self, result: VerdictResult) -> int:
        return sum(1 for v in self.verdicts if v.result == result)
