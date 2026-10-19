"""配置加载：全部通过环境变量控制，方便私有化部署时改。

默认 provider=mock，无需任何 API Key 即可跑通全链路。
换成真实模型只需要改 .env 里的两行。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 各家 OpenAI 兼容协议的接入点预设。
# 想换模型时优先看这里，不用去翻各自的 SDK 文档。
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "mock": {"base_url": "", "model": "mock-audit-v1", "api_key": "not-needed"},
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
    },
    # 豆包走火山方舟，model 必须填你自己的推理接入点 ID（ep-xxxx）
    "doubao": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",
    },
    # 本地部署：数据不出域，这是中大型客户唯一能接受的形态
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:14b",
        "api_key": "ollama",
    },
}


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()
        preset = PROVIDER_PRESETS.get(self.provider, {})

        self.base_url = (os.getenv("LLM_BASE_URL") or preset.get("base_url", "")).rstrip("/")
        self.model = os.getenv("LLM_MODEL") or preset.get("model", "")
        self.api_key = os.getenv("LLM_API_KEY") or preset.get("api_key", "")

        # 审核场景温度必须为 0，否则同一张单据两次审核结论可能不同，财务无法接受
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0"))
        self.timeout = float(os.getenv("LLM_TIMEOUT", "60"))
        self.max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))

        # 低于该置信度的判定自动转人工，绝不硬判
        self.confidence_threshold = float(os.getenv("AUDIT_CONFIDENCE_THRESHOLD", "0.7"))

        self.rules_path = BASE_DIR / os.getenv("RULES_PATH", "config/rules.yaml")
        self.log_dir = BASE_DIR / os.getenv("LOG_DIR", "logs")
        self.static_dir = BASE_DIR / "static"

        self.mock = self.provider == "mock" or _env_bool("MOCK_LLM")

    @property
    def is_configured(self) -> bool:
        if self.mock:
            return True
        return bool(self.base_url and self.model and self.api_key)

    def describe(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url or "-",
            "mock": self.mock,
            "temperature": self.temperature,
            "confidence_threshold": self.confidence_threshold,
        }


settings = Settings()
