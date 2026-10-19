"""规则加载。

规则全部来自 config/rules.yaml。改规则 = 改一行 YAML，不用改代码、
不用重新发版 —— 这是这套方案相对硬编码规则引擎最大的卖点，
也是跟客户谈判时最好用的一句话。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import Rule


class RuleLoadError(RuntimeError):
    pass


class RuleSet:
    def __init__(self, version: str, rules: list[Rule]) -> None:
        self.version = version
        self.rules = rules

    @property
    def enabled(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]

    @property
    def builtin_rules(self) -> list[Rule]:
        return [r for r in self.enabled if not r.is_llm]

    @property
    def llm_rules(self) -> list[Rule]:
        return [r for r in self.enabled if r.is_llm]

    def by_id(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)


def load_rules(path: Path) -> RuleSet:
    if not path.exists():
        raise RuleLoadError(f"规则文件不存在：{path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    version = str(raw.get("version", "unknown"))
    company = raw.get("company") or {}
    rules: list[Rule] = []

    for idx, item in enumerate(raw.get("rules") or []):
        if not isinstance(item, dict):
            raise RuleLoadError(f"第 {idx + 1} 条规则格式错误，应为字典")
        if "id" not in item or "evaluator" not in item:
            raise RuleLoadError(f"第 {idx + 1} 条规则缺少必填字段 id / evaluator")

        rule = Rule(**item)
        # 顶层 company 自动注入给发票类规则，避免在多条规则里重复维护
        if company and rule.evaluator.endswith("invoice_header"):
            rule.params.setdefault("company", company)
        rules.append(rule)

    if not rules:
        raise RuleLoadError("规则文件里没有任何规则")

    return RuleSet(version=version, rules=rules)
