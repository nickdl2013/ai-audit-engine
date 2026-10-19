"""HR 入职材料审核的编排引擎。

与费用域引擎结构完全一致，只换审核对象与措辞：

  1. 跑全部 hr_* 确定性规则（结果可复现）
  2. 把确定性结论作为上下文，连同材料一起交给模型做语义判断
  3. 汇总 → 决策（可办理 / 转人工 / 退回补正）
  4. 全量留痕到 logs/hr-YYYY-MM-DD.jsonl

决策规则同样刻意保守：
  - 命中高危违规           → REJECT（退回补正）
  - 一般违规 / 预警 / 无法判定 / 置信度不足 → MANUAL_REVIEW
  - 全部通过               → APPROVE
任何"拿不准"的情况都转人工，绝不硬判 —— 这是 HR 敢把系统用起来的唯一前提。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .audit import _SEVERITY_ORDER, _SORT_ORDER, map_llm_verdicts, read_recent_logs
from .config import Settings
from .hr_checkers import run_hr_builtin
from .hr_models import OnboardingAuditResult, OnboardingRecord
from .hr_prompts import HR_SYSTEM_PROMPT, build_hr_user_prompt, hr_mock_judge
from .llm import LLMClient, LLMError
from .models import Decision, Rule, RuleVerdict, TokenUsage
from .rules import RuleSet

__all__ = ["HRAuditEngine", "read_recent_logs"]


class HRAuditEngine:
    def __init__(
        self,
        ruleset: RuleSet,
        settings: Settings,
        client: LLMClient | None = None,
    ) -> None:
        self.ruleset = ruleset
        self.settings = settings
        self.client = client or LLMClient(settings)

    async def audit(self, record: OnboardingRecord) -> OnboardingAuditResult:
        started = time.perf_counter()
        verdicts: list[RuleVerdict] = []

        # ---------- 1. 确定性校验 ----------
        for rule in self.ruleset.builtin_rules:
            try:
                verdicts.extend(run_hr_builtin(rule, record))
            except Exception as exc:  # noqa: BLE001 —— 单条规则异常不能拖垮整份材料
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
            user_prompt = build_hr_user_prompt(record, self.ruleset.llm_rules, findings)
            mock_context = {
                "record": {
                    "employment_type": record.employment_type,
                    "is_fresh_graduate": record.is_fresh_graduate,
                    "work_years": record.work_years,
                    "highest_degree": record.highest_degree,
                    "position": record.position,
                },
                "docs": [
                    {
                        "doc_id": d.doc_id,
                        "doc_type": d.doc_type,
                        "description": d.description,
                        "submitted": d.submitted,
                    }
                    for d in record.docs
                ],
                "rules": [
                    {"rule_id": r.id, "name": r.name} for r in self.ruleset.llm_rules
                ],
            }

            try:
                response = await self.client.complete_json(
                    system=HR_SYSTEM_PROMPT,
                    user=user_prompt,
                    mock_context=mock_context,
                    mock_fn=hr_mock_judge,
                )
                usage = response.usage
                mock = response.mock
                verdicts.extend(
                    map_llm_verdicts(response.data, self.ruleset.llm_rules)
                )
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

        decision, summary = self._decide(record, verdicts)

        result = OnboardingAuditResult(
            record_id=record.record_id,
            decision=decision,
            summary=summary,
            candidate_name=record.candidate_name,
            department=record.department,
            position=record.position,
            employment_type=record.employment_type,
            onboard_date=record.onboard_date,
            doc_count=len(record.docs),
            submitted_count=sum(1 for d in record.docs if d.submitted),
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

    def _decide(
        self, record: OnboardingRecord, verdicts: list[RuleVerdict]
    ) -> tuple[Decision, str]:
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

        who = f"{record.candidate_name}（{record.position or '岗位未填'}）"
        submitted = sum(1 for d in record.docs if d.submitted)

        if high_fails:
            names = "、".join(sorted({v.rule_name for v in high_fails}))
            return "REJECT", (
                f"{who} 的入职材料命中 {len(high_fails)} 项高危不合规：{names}。"
                f"本材料包共 {len(record.docs)} 份材料（已提交 {submitted} 份），"
                f"建议退回补正后再办理入职手续。"
            )

        if normal_fails or warnings or uncertains or low_confidence:
            parts = []
            if normal_fails:
                parts.append(f"{len(normal_fails)} 项一般不合规")
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
                f"{who} 未发现高危不合规，但存在 {'、'.join(parts)}，涉及：{'、'.join(pending)}。"
                f"建议转 HR 人工复核确认后再办理入职。"
            )

        return "APPROVE", (
            f"{who} 的 {len(verdicts)} 项规则校验全部通过，"
            f"{len(record.docs)} 份材料（已提交 {submitted} 份）齐全合规，"
            f"可办理入职手续。"
        )

    def _write_log(self, result: OnboardingAuditResult) -> None:
        """审核留痕。出争议时，这张日志就是 HR 的免责证据。"""
        try:
            log_dir: Path = self.settings.log_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"hr-{datetime.now():%Y-%m-%d}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(result.model_dump_json() + "\n")
        except OSError:
            # 日志写失败不影响审核结果返回，但生产环境应接入告警
            pass
