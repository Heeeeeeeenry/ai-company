"""AI Company - Configuration System"""
import os
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional
from dotenv import load_dotenv

# Suppress noisy third-party debug logs
for _noisy in ("jieba", "jieba.cache", "jieba.cutter"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
    logging.getLogger(_noisy).propagate = False  # Don't inherit root level
logging.captureWarnings(True)  # Capture jieba's UserWarning as log

load_dotenv()


@dataclass
class ModelConfig:
    provider: str
    model: str

    @classmethod
    def from_string(cls, s: str) -> "ModelConfig":
        if ":" in s:
            provider, _, model = s.partition(":")
            return cls(provider=provider, model=model)
        # No prefix → default to deepseek
        return cls(provider="deepseek", model=s)


@dataclass
class Config:
    # LLM Routing
    ceo_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("CEO_MODEL", "deepseek-chat")))
    pm_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("PM_MODEL", "deepseek-chat")))
    architect_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("ARCHITECT_MODEL", "deepseek-chat")))
    developer_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("DEVELOPER_MODEL", "deepseek-chat")))
    qa_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("QA_MODEL", "deepseek-chat")))
    devops_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("DEVOPS_MODEL", "deepseek-chat")))
    research_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("RESEARCH_MODEL", "deepseek-chat")))
    marketer_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("MARKETER_MODEL", "deepseek-chat")))
    review_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("REVIEW_MODEL", "deepseek-chat")))

    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_allowed_users: list[str] = field(default_factory=lambda: os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if os.getenv("TELEGRAM_ALLOWED_USERS") else [])

    # Memory
    memory_backend: str = os.getenv("MEMORY_BACKEND", "graphiti")
    memory_max_episodes: int = int(os.getenv("MEMORY_MAX_EPISODES", "1000"))
    memory_dedup_threshold: float = float(os.getenv("MEMORY_DEDUP_THRESHOLD", "0.85"))
    graphiti_url: str = os.getenv("GRAPHITI_URL", "http://localhost:8000")
    letta_base_url: str = os.getenv("LETTA_BASE_URL", "http://localhost:8283")
    chroma_url: str = os.getenv("CHROMA_URL", "http://localhost:8001")

    # Sandbox
    sandbox_provider: str = os.getenv("SANDBOX_PROVIDER", "local")
    e2b_api_key: str = os.getenv("E2B_API_KEY", "")

    # MCP Servers
    mcp_servers: Dict = field(default_factory=lambda: json.loads(os.getenv("MCP_SERVERS", "{}")))

    # Gates
    gate_prd_score: int = int(os.getenv("GATE_PRD_SCORE", "70"))
    gate_arch_score: int = int(os.getenv("GATE_ARCH_SCORE", "75"))
    gate_code_score: int = int(os.getenv("GATE_CODE_SCORE", "70"))
    gate_final_score: int = int(os.getenv("GATE_FINAL_SCORE", "80"))

    # API Keys (repr=False to prevent accidental exposure in logs/errors)
    openai_api_key: str = field(default=os.getenv("OPENAI_API_KEY", ""), repr=False)
    anthropic_api_key: str = field(default=os.getenv("ANTHROPIC_API_KEY", ""), repr=False)
    deepseek_api_key: str = field(default=os.getenv("DEEPSEEK_API_KEY", ""), repr=False)
    tavily_api_key: str = field(default=os.getenv("TAVILY_API_KEY", ""), repr=False)
    qwen_api_key: str = field(default=os.getenv("QWEN_API_KEY", ""), repr=False)
    qwen_base_url: str = field(default=os.getenv("QWEN_BASE_URL", ""), repr=False)
    qwen_vision_model: str = field(default=os.getenv("QWEN_VISION_MODEL", "qwen-vl-max"), repr=False)

    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def get_model_for(self, role: str) -> ModelConfig:
        mapping = {
            "ceo": self.ceo_model,
            "pm": self.pm_model,
            "architect": self.architect_model,
            "developer": self.developer_model,
            "qa": self.qa_model,
            "devops": self.devops_model,
            "research": self.research_model,
            "researcher": self.research_model,
            "marketing": self.marketer_model,
            "marketer": self.marketer_model,
            "review": self.review_model,
        }
        return mapping.get(role, self.ceo_model)


config = Config()
