"""审核编排：把确定性校验和语义审核串起来，产出可追溯的最终结论。

流程：
  1. 跑全部 builtin 规则（确定性，结果可复现）
  2. 把确定性结论作为上下文，连同单据一起交给模型做语义判断
  3. 汇总 → 决策（通过 / 退回 / 转人工）
  4. 全量留痕到 audit-YYYY-MM-DD.jsonl

决策规则刻意保守：
  - 命中高危违规         → REJECT
  - 一般违规 / 预警 / 无法判定 / 置信度不足 → MANUAL_REVIEW
  - 全部通过             → APPROVE
任何"拿不准"的情况都转人工，绝不硬判 —— 这是财务敢把系统上线的唯一前提。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .checkers import run_builtin
from .config import Settings
from .llm import LLMClient, LLMError
from .models import AuditResult, Decision, ExpenseReport, Rule, RuleVerdict, TokenUsage
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .rules import RuleSet

# 展示顺序：先把拦下来的放前面，财务一眼能看到问题
_SORT_ORDER = {"fail": 0, "warn": 1, "uncertain": 2, "pass": 3, "skipped": 4}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_VALID_RESULTS = {"pass", "fail", "warn", "uncertain"}


def map_llm_verdicts(data: dict, llm_rules: list[Rule]) -> list[RuleVerdict]:
    """把模型输出映射成标准判定，并对模型的越界行为做防御。

    防御点：
      - 模型编造的 rule_id 直接丢弃，不能污染结论
      - result 不在枚举内一律降级为 uncertain
      - confidence 强制裁剪到 [0, 1]
      - 模型漏答的规则补一条 uncertain，不能让规则被静默跳过

    这个函数是领域无关的，费用域和 HR 域共用。
    """
    by_id = {r.id: r for r in llm_rules}
    raw_list = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(raw_list, list):
        raw_list = []

    out: list[RuleVerdict] = []
    answered: set[str] = set()

    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        rule_id = str(entry.get("rule_id") or "").strip()
        rule = by_id.get(rule_id)
        if rule is None:
            continue

        result = str(entry.get("result") or "").strip().lower()
        if result not in _VALID_RESULTS:
            result = "uncertain"

        try:
            confidence = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(max(confidence, 0.0), 1.0)

        evidence = entry.get("evidence")
        out.append(
            RuleVerdict(
                rule_id=rule.id,
                rule_name=rule.name,
                severity=rule.severity,
                result=result,  # type: ignore[arg-type]
                reason=str(entry.get("reason") or "").strip() or "模型未给出说明",
                evidence=str(evidence).strip() if evidence else None,
                basis=rule.basis,
                confidence=confidence,
                source="llm",
            )
        )
        answered.add(rule_id)

    for rule in llm_rules:
        if rule.id not in answered:
            out.append(
                RuleVerdict(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    severity=rule.severity,
                    result="uncertain",
                    reason="模型未返回该规则的审核结论，转人工复核",
                    basis=rule.basis,
                    confidence=0.0,
                    source="llm",
                )
            )
    return out


class AuditEngine:
    def __init__(
        self,
        ruleset: RuleSet,
        settings: Settings,
        client: LLMClient | None = None,
    ) -> None:
        self.ruleset = ruleset
        self.settings = settings
        self.client = client or LLMClient(settings)

    async def audit(self, report: ExpenseReport) -> AuditResult:
        started = time.perf_counter()
        verdicts: list[RuleVerdict] = []

        # ---------- 1. 确定性校验 ----------
        for rule in self.ruleset.builtin_rules:
            try:
                verdicts.extend(run_builtin(rule, report))
            except Exception as exc:  # noqa: BLE001 —— 单条规则异常不能拖垮整张单据
                verdicts.append(
                    RuleVerdict(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        severity=rule.severity,
                        result="uncertain",
                        reason=f"规则执行异常，转人工核对：{exc}",
                        basis=rule.basis,
                        confidence=0.0,
                        source="deterministic",
                    )
                )

        # ---------- 2. 语义审核 ----------
        usage = TokenUsage()
        mock = self.client.is_mock
        if self.ruleset.llm_rules:
            findings = [
                f"[{v.rule_id}] {v.rule_name}：{v.result} —— {v.reason}"
                for v in verdicts
                if v.result in ("fail", "warn", "uncertain")
            ]
            user_prompt = build_user_prompt(report, self.ruleset.llm_rules, findings)
            mock_context = {
                "items": [
                    {
                        "item_id": i.item_id,
                        "category": i.category,
                        "amount": i.amount,
                        "date": i.date,
                        "description": i.description,
                        "vendor": i.vendor,
                    }
                    for i in report.items
                ],
                "rules": [{"rule_id": r.id, "name": r.name} for r in self.ruleset.llm_rules],
            }

            try:
                response = await self.client.complete_json(
                    system=SYSTEM_PROMPT,
                    user=user_prompt,
                    mock_context=mock_context,
                )
                usage = response.usage
                mock = response.mock
                verdicts.extend(self._map_llm_verdicts(response.data, self.ruleset.llm_rules))
            except LLMError as exc:
                # 模型不可用时，语义规则全部转人工，绝不放行
                for rule in self.ruleset.llm_rules:
                    verdicts.append(
                        RuleVerdict(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            severity=rule.severity,
                            result="uncertain",
                            reason=f"模型服务不可用，该规则未能完成校验，请转人工：{exc}",
                            basis=rule.basis,
                            confidence=0.0,
                            source="llm",
                        )
                    )

        verdicts.sort(
            key=lambda v: (_SORT_ORDER.get(v.result, 9), _SEVERITY_ORDER.get(v.severity, 9))
        )

        decision, summary = self._decide(report, verdicts)

        result = AuditResult(
            report_id=report.report_id,
            decision=decision,
            summary=summary,
            applicant=report.applicant,
            department=report.department,
            total_amount=report.total_amount,
            item_count=len(report.items),
            verdicts=verdicts,
            rules_version=self.ruleset.version,
            model=self.settings.model,
            mock=mock,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            usage=usage,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._write_log(result)
        return result

    # ------------------------------------------------------------------

    def _map_llm_verdicts(self, data: dict, llm_rules: list[Rule]) -> list[RuleVerdict]:
        return map_llm_verdicts(data, llm_rules)

    def _decide(self, report: ExpenseReport, verdicts: list[RuleVerdict]) -> tuple[Decision, str]:
        threshold = self.settings.confidence_threshold

        high_fails = [v for v in verdicts if v.result == "fail" and v.severity == "high"]
        normal_fails = [v for v in verdicts if v.result == "fail" and v.severity != "high"]
        warnings = [v for v in verdicts if v.result == "warn"]
        uncertains = [v for v in verdicts if v.result == "uncertain"]
        low_confidence = [
            v
            for v in verdicts
            if v.result in ("fail", "warn", "uncertain") and v.confidence < threshold
        ]

        if high_fails:
            names = "、".join(sorted({v.rule_name for v in high_fails}))
            return "REJECT", (
                f"命中 {len(high_fails)} 项高危违规：{names}。"
                f"本单共 {len(report.items)} 条明细、金额合计 {report.total_amount:,.2f} 元，"
                f"建议退回申请人补正后重新提交。"
            )

        if normal_fails or warnings or uncertains or low_confidence:
            parts = []
            if normal_fails:
                parts.append(f"{len(normal_fails)} 项一般违规")
            if warnings:
                parts.append(f"{len(warnings)} 项预警")
            if uncertains:
                parts.append(f"{len(uncertains)} 项无法判定")
            if low_confidence:
                parts.append(f"{len(low_confidence)} 项置信度不足（低于 {threshold}）")
            pending = sorted(
                {
                    v.rule_name
                    for v in (normal_fails + warnings + uncertains + low_confidence)
                }
            )
            return "MANUAL_REVIEW", (
                f"未发现高危违规，但存在{'、'.join(parts)}，涉及：{'、'.join(pending)}。"
                f"建议转人工复核，确认后再进入审批流。"
            )

        return "APPROVE", (
            f"全部 {len(verdicts)} 项规则校验通过，{len(report.items)} 条明细、"
            f"金额合计 {report.total_amount:,.2f} 元，可进入审批流。"
        )

    def _write_log(self, result: AuditResult) -> None:
        """审计留痕。To B 系统上线后，这张日志就是你的免责证据。"""
        try:
            self.settings.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.settings.log_dir / f"audit-{datetime.now():%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(result.model_dump_json() + "\n")
        except OSError:
            # 日志写失败不影响审核结果返回，但生产环境应接入告警
            pass


def read_recent_logs(log_dir: Path, limit: int = 20) -> list[dict]:
    if not log_dir.exists():
        return []
    files = sorted(log_dir.glob("audit-*.jsonl"), reverse=True)
    records: list[dict] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if len(records) >= limit:
                return records
            line = line.strip()
            if not line:
                continue
            try:
                import json

                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records
