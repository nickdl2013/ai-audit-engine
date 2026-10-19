"""大模型客户端。

只用 OpenAI 兼容协议，所以换模型 = 改一行 .env。
支持 mock 模式：不联网、不需要 Key，用来跑通链路和给客户演示界面。

生产环境注意：
  - 温度必须为 0，否则同一张单据两次审核结论不同，财务会直接否掉这个系统
  - 必须支持本地部署（ollama / vLLM），中大型客户不会允许报销数据出内网
  - 所有请求和响应都要留痕，审核结论必须可追溯
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import Settings
from .models import TokenUsage

# --------------------------------------------------------------------------
# 返回结构
# --------------------------------------------------------------------------


@dataclass
class LLMResponse:
    data: dict[str, Any]
    raw: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    mock: bool = False
    elapsed_ms: int = 0
    model: str = ""
    error: str | None = None


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# JSON 解析：大模型返回的 JSON 经常带 markdown 包裹或前后废话，必须容错
# --------------------------------------------------------------------------


def extract_json(text: str) -> dict[str, Any]:
    if not text:
        raise LLMError("模型返回内容为空")

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise LLMError(f"无法解析模型返回的 JSON：{exc}；原始内容前 300 字：{cleaned[:300]}") from exc

    raise LLMError(f"模型返回内容中找不到 JSON 对象：{cleaned[:300]}")


# --------------------------------------------------------------------------
# Mock 后端：不联网的假模型，用于链路验证和演示
# --------------------------------------------------------------------------


def _group_by(items: list[dict], keys: tuple[str, ...]) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        key = tuple(item.get(k) for k in keys)
        groups.setdefault(key, []).append(item)
    return groups


def mock_judge(context: dict[str, Any]) -> dict[str, Any]:
    """内置假模型。

    它的判断逻辑是几个关键词规则，只为了让演示链路跑通，
    不代表真实模型能力。真实能力请把 LLM_PROVIDER 换成 deepseek / qwen / ollama。
    """
    items: list[dict] = context.get("items", [])
    rules: list[dict] = context.get("rules", [])
    verdicts: list[dict] = []

    for rule in rules:
        rule_id = rule.get("rule_id", "")
        name = rule.get("name", "")

        # 语义一致性
        if "一致性" in name:
            mismatches, vague = [], []
            for item in items:
                desc = str(item.get("description", ""))
                category = str(item.get("category", ""))
                dining_words = ("晚宴", "餐叙", "用餐", "就餐", "酒水", "宴请")
                if any(w in desc for w in dining_words) and category not in (
                    "招待费",
                    "餐饮费",
                    "业务招待费",
                ):
                    mismatches.append(item)
                if len(desc.strip()) < 6:
                    vague.append(item)
            if mismatches:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "warn",
                        "reason": "存在费用类型与事由描述不匹配的明细：事由描述为餐饮性质，但费用类型填报为其他科目，"
                        "需确认是否为科目选择错误或存在规避招待费标准的意图。",
                        "evidence": "；".join(
                            f"{m['item_id']}：费用类型「{m['category']}」，事由「{m['description']}」"
                            for m in mismatches
                        ),
                        "confidence": 0.82,
                    }
                )
            elif vague:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "uncertain",
                        "reason": "部分明细事由描述过于笼统，无法判断费用性质与所选科目是否一致。",
                        "evidence": "；".join(f"{v['item_id']}：事由「{v['description']}」" for v in vague),
                        "confidence": 0.45,
                    }
                )
            else:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "pass",
                        "reason": "全部明细的事由描述与所选费用类型语义一致。",
                        "evidence": f"共核查 {len(items)} 条明细，未发现不匹配项",
                        "confidence": 0.9,
                    }
                )

        # 拆单嫌疑
        elif "拆单" in name:
            suspects = []
            for (vendor, day), group in _group_by(items, ("vendor", "date")).items():
                if vendor and day and len(group) >= 2:
                    suspects.append((vendor, day, group))
            if suspects:
                # 取金额最大的一组，演示时更有说服力
                vendor, day, group = max(
                    suspects, key=lambda s: sum(float(g.get("amount", 0)) for g in s[2])
                )
                total = sum(float(g.get("amount", 0)) for g in group)
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "warn",
                        "reason": f"存在同一供应商、同一日期的多笔消费，合计 {total:.2f} 元，"
                        "符合拆单规避限额的典型特征，建议要求申请人提供说明或合并单据审核。",
                        "evidence": f"{day} 于「{vendor}」共 {len(group)} 笔："
                        + "、".join(f"{g['item_id']}（{float(g['amount']):.2f} 元）" for g in group),
                        "confidence": 0.75,
                    }
                )
            else:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "pass",
                        "reason": "未发现同一供应商同一日期的异常多笔消费。",
                        "evidence": f"共核查 {len(items)} 条明细的供应商与日期组合",
                        "confidence": 0.88,
                    }
                )

        # 可报销范围
        elif "报销范围" in name or "可报销" in name:
            banned = [
                "会员充值",
                "充值",
                "罚款",
                "滞纳金",
                "礼品卡",
                "私人",
                "个人",
                "游戏",
                "家庭",
            ]
            hits = []
            for item in items:
                desc = str(item.get("description", ""))
                matched = [w for w in banned if w in desc]
                if matched:
                    hits.append((item, matched))
            if hits:
                item, matched = hits[0]
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "fail",
                        "reason": "存在明显属于个人消费、不属于公司经营性支出的明细，按制度不得报销。",
                        "evidence": "；".join(
                            f"{h[0]['item_id']}：事由「{h[0]['description']}」，命中禁止性关键词 {h[1]}"
                            for h in hits
                        ),
                        "confidence": 0.92,
                    }
                )
            else:
                verdicts.append(
                    {
                        "rule_id": rule_id,
                        "result": "pass",
                        "reason": "全部明细的事由均属于公司经营性支出范围。",
                        "evidence": f"共核查 {len(items)} 条明细事由",
                        "confidence": 0.87,
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


# --------------------------------------------------------------------------
# 真实模型客户端
# --------------------------------------------------------------------------


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def is_mock(self) -> bool:
        return self.settings.mock

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        mock_context: dict[str, Any] | None = None,
        mock_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()

        if self.settings.mock:
            await asyncio.sleep(0.2)  # 模拟网络耗时，让演示更接近真实体感
            data = (mock_fn or mock_judge)(mock_context or {})
            return LLMResponse(
                data=data,
                raw=json.dumps(data, ensure_ascii=False, indent=2),
                usage=TokenUsage(),
                mock=True,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                model=self.settings.model,
            )

        if not self.settings.is_configured:
            raise LLMError(
                "模型未配置完整。请检查 .env 中的 LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY，"
                "或把 LLM_PROVIDER 设为 mock 先跑通链路。"
            )

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.temperature,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.settings.base_url}/chat/completions"

        last_error: Exception | None = None
        force_json_off = False
        timeout = httpx.Timeout(self.settings.timeout, connect=10.0)

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(self.settings.max_retries + 1):
                body = dict(payload)
                if force_json_off:
                    body.pop("response_format", None)
                try:
                    resp = await client.post(url, json=body, headers=headers)

                    # 有些服务商不支持 response_format，降级重试一次
                    if resp.status_code == 400 and not force_json_off:
                        text = resp.text
                        if "response_format" in text or "json_object" in text:
                            force_json_off = True
                            continue

                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_error = LLMError(f"模型服务返回 {resp.status_code}：{resp.text[:200]}")
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue

                    resp.raise_for_status()
                    raw_body = resp.json()
                    content = raw_body["choices"][0]["message"]["content"]
                    usage_raw = raw_body.get("usage") or {}
                    usage = TokenUsage(
                        prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
                        completion_tokens=int(usage_raw.get("completion_tokens", 0)),
                        total_tokens=int(usage_raw.get("total_tokens", 0)),
                    )
                    return LLMResponse(
                        data=extract_json(content),
                        raw=content,
                        usage=usage,
                        mock=False,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        model=self.settings.model,
                    )

                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    await asyncio.sleep(1.5 * (attempt + 1))
                except (KeyError, IndexError) as exc:
                    raise LLMError(f"模型返回结构异常：{exc}") from exc

        raise LLMError(f"调用模型失败（已重试 {self.settings.max_retries} 次）：{last_error}")
