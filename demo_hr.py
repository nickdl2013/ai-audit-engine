"""HR 入职材料审核 · 命令行 demo —— 不启服务也能跑。

用法：
    python demo_hr.py                              # 用 samples/onboarding.json（问题件）
    python demo_hr.py samples/onboarding_clean.json # 全部合规
    python demo_hr.py samples/onboarding_review.json
    python demo_hr.py your_record.json --only-issues
    python demo_hr.py --json                        # 输出原始 JSON，方便接管道
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Windows 控制台默认不是 UTF-8，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from app.config import settings  # noqa: E402
from app.hr_audit import HRAuditEngine  # noqa: E402
from app.hr_models import OnboardingAuditResult, OnboardingRecord  # noqa: E402
from app.rules import load_rules  # noqa: E402

DEFAULT_RULES = BASE_DIR / "config" / "rules_hr.yaml"

RESULT_MARK = {
    "fail": "[不通过]",
    "warn": "[预  警]",
    "uncertain": "[待确认]",
    "pass": "[通  过]",
    "skipped": "[已跳过]",
}
DECISION_MARK = {
    "APPROVE": "PASS 可办理入职",
    "MANUAL_REVIEW": "MANUAL_REVIEW 转 HR 人工复核",
    "REJECT": "REJECT 退回补正",
}


def _line(width: int = 78) -> str:
    return "-" * width


def render(result: OnboardingAuditResult, only_issues: bool = False) -> str:
    out: list[str] = []
    out.append(_line())
    out.append(
        f"材料包 {result.record_id}｜候选人 {result.candidate_name}"
        f"｜部门 {result.department}｜岗位 {result.position}"
    )
    out.append(
        f"用工形式 {result.employment_type}｜拟入职 {result.onboard_date}"
        f"｜材料 {result.submitted_count}/{result.doc_count} 份已提交"
        f"｜规则版本 {result.rules_version}｜模型 {result.model}"
        + ("（mock 模拟）" if result.mock else "")
    )
    out.append(_line())
    out.append(
        "审核结论：" + DECISION_MARK.get(result.decision, result.decision)
        + f"   耗时 {result.elapsed_ms} ms"
    )
    out.append(result.summary)
    out.append(_line())

    verdicts = result.verdicts
    hidden = 0
    if only_issues:
        kept = [v for v in verdicts if v.result != "pass"]
        hidden = len(verdicts) - len(kept)
        verdicts = kept

    for v in verdicts:
        out.append(
            f"{RESULT_MARK.get(v.result, v.result)} [{v.severity.upper()}] "
            f"{v.rule_id} {v.rule_name}  (置信度 {v.confidence:.2f}｜{v.source})"
        )
        out.append(f"    说明：{v.reason}")
        if v.evidence:
            out.append(f"    证据：{v.evidence}")
        if v.basis:
            out.append(f"    依据：{v.basis}")
        out.append("")

    if hidden:
        out.append(f"（已隐藏 {hidden} 条通过项，去掉 --only-issues 可查看完整留痕）")
        out.append("")

    if result.usage.total_tokens:
        out.append(
            f"Token 消耗：prompt {result.usage.prompt_tokens} / "
            f"completion {result.usage.completion_tokens} / "
            f"total {result.usage.total_tokens}"
        )
    out.append(_line())
    return "\n".join(out)


async def main() -> int:
    parser = argparse.ArgumentParser(description="HR 入职材料智能审核 · 命令行 demo")
    parser.add_argument(
        "record",
        nargs="?",
        default=str(BASE_DIR / "samples" / "onboarding.json"),
        help="入职材料包 JSON 文件路径",
    )
    parser.add_argument(
        "--rules", default=str(DEFAULT_RULES), help="规则库 YAML 路径"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    parser.add_argument(
        "--only-issues",
        action="store_true",
        help="只显示不通过 / 预警 / 待确认项，隐藏通过项（给 HR 演示时用）",
    )
    args = parser.parse_args()

    record_path = Path(args.record)
    if not record_path.exists():
        print(f"找不到材料包文件：{record_path}", file=sys.stderr)
        return 1

    record = OnboardingRecord(**json.loads(record_path.read_text(encoding="utf-8")))
    ruleset = load_rules(Path(args.rules))
    engine = HRAuditEngine(ruleset, settings)

    if not args.json:
        print()
        print(
            f"规则库版本 {ruleset.version}｜确定性规则 {len(ruleset.builtin_rules)} 条｜"
            f"语义规则 {len(ruleset.llm_rules)} 条"
        )
        print(
            f"模型：{settings.model}"
            f"（{'mock 模拟模式，换成 deepseek/qwen/ollama 即为真实调用' if settings.mock else '真实调用'}）"
        )

    result = await engine.audit(record)

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(render(result, only_issues=args.only_issues))

    # 退出码便于接入 CI 或批处理
    return {"APPROVE": 0, "MANUAL_REVIEW": 2, "REJECT": 3}[result.decision]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
