"""HTTP 接口层。

同一套引擎挂了两个业务域，接口刻意保持对称：

  费用域  POST /audit        → APPROVE / MANUAL_REVIEW / REJECT
  HR 域   POST /hr/audit     → 同上（可办理入职 / 转人工 / 退回补正）

对接方式（旁路集成，不动客户核心系统）：
  业务系统提交单据/材料包 → 调用对应接口 → 拿到 decision → 按结论流转
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .audit import AuditEngine, read_recent_logs
from .config import BASE_DIR, settings
from .hr_audit import HRAuditEngine
from .hr_models import OnboardingAuditResult, OnboardingRecord
from .models import AuditResult, ExpenseReport
from .rules import RuleLoadError, load_rules

_HR_RULES_PATH = BASE_DIR / "config" / "rules_hr.yaml"

_state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ruleset = load_rules(settings.rules_path)
    _state["ruleset"] = ruleset
    _state["engine"] = AuditEngine(ruleset, settings)

    hr_ruleset = load_rules(_HR_RULES_PATH)
    _state["hr_ruleset"] = hr_ruleset
    _state["hr_engine"] = HRAuditEngine(hr_ruleset, settings)

    print(
        f"[启动] 费用域 规则版本 {ruleset.version}｜"
        f"确定性规则 {len(ruleset.builtin_rules)} 条｜语义规则 {len(ruleset.llm_rules)} 条"
    )
    print(
        f"[启动] HR 域 规则版本 {hr_ruleset.version}｜"
        f"确定性规则 {len(hr_ruleset.builtin_rules)} 条｜语义规则 {len(hr_ruleset.llm_rules)} 条｜"
        f"模型 {settings.model}（{'mock 模拟模式' if settings.mock else '真实调用'}）"
    )
    yield
    _state.clear()


app = FastAPI(
    title="企业单据智能审核引擎",
    description="确定性规则 + 语义审核分层的审核骨架；同一引擎支持费用审核与 HR 入职材料审核两个域",
    version=__version__,
    lifespan=lifespan,
)

# 演示方便，生产环境请收紧
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _engine() -> AuditEngine:
    engine = _state.get("engine")
    if engine is None:
        raise HTTPException(status_code=503, detail="服务尚未初始化完成")
    return engine  # type: ignore[return-value]


@app.get("/health", summary="健康检查")
async def health() -> dict:
    ruleset = _state.get("ruleset")
    return {
        "status": "ok",
        "version": __version__,
        "rules_version": getattr(ruleset, "version", None),
        "llm": settings.describe(),
    }


@app.get("/rules", summary="查看当前生效的规则库")
async def list_rules() -> dict:
    ruleset = _state.get("ruleset")
    if ruleset is None:
        raise HTTPException(status_code=503, detail="规则尚未加载")
    return {
        "version": ruleset.version,
        "count": len(ruleset.rules),
        "rules": [
            {
                "id": r.id,
                "name": r.name,
                "severity": r.severity,
                "scope": r.scope,
                "evaluator": r.evaluator,
                "basis": r.basis,
                "description": r.description,
                "applies_to_categories": r.applies_to_categories,
                "params": r.params,
            }
            for r in ruleset.rules
        ],
    }


@app.post("/audit", response_model=AuditResult, summary="审核一张报销单")
async def audit(report: ExpenseReport) -> AuditResult:
    if not report.items:
        raise HTTPException(status_code=400, detail="单据没有任何明细，无法审核")
    return await _engine().audit(report)


@app.post("/audit/batch", response_model=list[AuditResult], summary="批量审核")
async def audit_batch(reports: list[ExpenseReport]) -> list[AuditResult]:
    if len(reports) > 20:
        raise HTTPException(status_code=400, detail="单次批量审核上限 20 张单据")
    engine = _engine()
    results = []
    for report in reports:
        try:
            results.append(await engine.audit(report))
        except Exception as exc:  # noqa: BLE001 —— 单张失败不影响整批
            results.append(
                AuditResult(
                    report_id=report.report_id,
                    decision="MANUAL_REVIEW",
                    summary=f"审核过程异常，已转人工：{exc}",
                )
            )
    return results


@app.get("/audit/logs", summary="查看最近的审核留痕")
async def audit_logs(limit: int = Query(default=20, ge=1, le=200)) -> dict:
    records = read_recent_logs(settings.log_dir, limit=limit)
    return {"count": len(records), "records": records}


# ---------------------------------------------------------------- HR 域接口


def _hr_engine() -> HRAuditEngine:
    engine = _state.get("hr_engine")
    if engine is None:
        raise HTTPException(status_code=503, detail="HR 审核服务尚未初始化完成")
    return engine  # type: ignore[return-value]


@app.get("/hr/rules", summary="查看 HR 域生效的规则库")
async def hr_list_rules() -> dict:
    ruleset = _state.get("hr_ruleset")
    if ruleset is None:
        raise HTTPException(status_code=503, detail="HR 规则尚未加载")
    return {
        "version": ruleset.version,
        "count": len(ruleset.rules),
        "rules": [
            {
                "id": r.id,
                "name": r.name,
                "severity": r.severity,
                "scope": r.scope,
                "evaluator": r.evaluator,
                "basis": r.basis,
                "description": r.description,
                "params": r.params,
            }
            for r in ruleset.rules
        ],
    }


@app.post(
    "/hr/audit",
    response_model=OnboardingAuditResult,
    summary="审核一份入职材料包",
)
async def hr_audit_record(record: OnboardingRecord) -> OnboardingAuditResult:
    if not record.docs:
        raise HTTPException(status_code=400, detail="材料包没有任何材料，无法审核")
    return await _hr_engine().audit(record)


@app.post(
    "/hr/audit/batch",
    response_model=list[OnboardingAuditResult],
    summary="批量审核入职材料包",
)
async def hr_audit_batch(records: list[OnboardingRecord]) -> list[OnboardingAuditResult]:
    if len(records) > 20:
        raise HTTPException(status_code=400, detail="单次批量审核上限 20 份材料包")
    engine = _hr_engine()
    results = []
    for record in records:
        try:
            results.append(await engine.audit(record))
        except Exception as exc:  # noqa: BLE001 —— 单份失败不影响整批
            results.append(
                OnboardingAuditResult(
                    record_id=record.record_id,
                    decision="MANUAL_REVIEW",
                    summary=f"审核过程异常，已转人工：{exc}",
                )
            )
    return results


@app.get("/hr", include_in_schema=False)
async def hr_demo_page():
    page = settings.static_dir / "hr.html"
    if not page.exists():
        return {"message": "HR 演示页不存在，请从 /docs 查看 API 文档"}
    return FileResponse(page)


@app.get("/", include_in_schema=False)
async def index():
    page = settings.static_dir / "index.html"
    if not page.exists():
        return {"message": "接口服务已启动，请从 /docs 查看 API 文档"}
    return FileResponse(page)


if settings.static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

# 样例单据目录，方便演示页直接取用
_samples_dir = settings.static_dir.parent / "samples"
if _samples_dir.exists():
    app.mount("/samples", StaticFiles(directory=str(_samples_dir)), name="samples")
