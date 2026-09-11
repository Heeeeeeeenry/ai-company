"""AI Company - Configuration System"""
import os
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Optional
from dotenv import load_dotenv

# HuggingFace tokenizers can deadlock/noise after multiprocessing fork if
# parallelism was used before forking. Make the runtime choice explicit before
# any tokenizer users import.
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Suppress noisy third-party debug logs
for _noisy in ("jieba", "jieba.cache", "jieba.cutter"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
    logging.getLogger(_noisy).propagate = False  # Don't inherit root level
logging.captureWarnings(True)  # Capture jieba's UserWarning as log

load_dotenv()


def _load_mcp_servers() -> Dict:
    """Load MCP server config and ensure local project access is available.

    Older .env files constrained @modelcontextprotocol/server-filesystem to
    /tmp only, which made self-check/self-repair agents unable to inspect or
    modify the ai-company project. The filesystem server accepts multiple root
    directories, so preserve any configured roots and append the project root.
    """
    try:
        servers = json.loads(os.getenv("MCP_SERVERS", "{}"))
    except json.JSONDecodeError:
        logging.getLogger("ai_company.config").warning(
            "Invalid MCP_SERVERS JSON; ignoring MCP server config"
        )
        return {}

    fs = servers.get("filesystem")
    if isinstance(fs, dict):
        args = list(fs.get("args") or [])
        if any("@modelcontextprotocol/server-filesystem" in str(arg) for arg in args):
            project_root = Path(os.getenv("AI_COMPANY_HOME", Path(__file__).resolve().parents[1])).resolve()
            project_root_s = str(project_root)
            if project_root.exists() and project_root_s not in args:
                args.append(project_root_s)
                fs["args"] = args
    return servers


@dataclass
class ModelConfig:
    provider: str
    model: str

    @classmethod
    def from_string(cls, s: str) -> "ModelConfig":
        if ":" in s:
            provider, _, model = s.partition(":")
            return cls(provider=provider, model=model)
        # No prefix → default to OneAPI proxy.
        return cls(provider="oneapi", model=s)


@dataclass
class Config:
    # LLM Routing
    ceo_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("CEO_MODEL", "gpt-5.5")))
    pm_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("PM_MODEL", "gpt-5.5")))
    architect_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("ARCHITECT_MODEL", "gpt-5.5")))
    developer_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("DEVELOPER_MODEL", "gpt-5.5")))
    qa_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("QA_MODEL", "gpt-5.5")))
    devops_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("DEVOPS_MODEL", "gpt-5.5")))
    research_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("RESEARCH_MODEL", "gpt-5.5")))
    marketer_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("MARKETER_MODEL", "gpt-5.5")))
    review_model: ModelConfig = field(default_factory=lambda: ModelConfig.from_string(os.getenv("REVIEW_MODEL", "gpt-5.5")))

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
    mcp_servers: Dict = field(default_factory=_load_mcp_servers)

    # Gates
    gate_prd_score: int = int(os.getenv("GATE_PRD_SCORE", "70"))
    gate_arch_score: int = int(os.getenv("GATE_ARCH_SCORE", "75"))
    gate_code_score: int = int(os.getenv("GATE_CODE_SCORE", "70"))
    gate_final_score: int = int(os.getenv("GATE_FINAL_SCORE", "80"))

    # API Keys (repr=False to prevent accidental exposure in logs/errors)
    openai_api_key: str = field(default=os.getenv("OPENAI_API_KEY", ""), repr=False)
    anthropic_api_key: str = field(default=os.getenv("ANTHROPIC_API_KEY", ""), repr=False)
    deepseek_api_key: str = field(default=os.getenv("DEEPSEEK_API_KEY", ""), repr=False)
    oneapi_api_key: str = field(default=os.getenv("ONEAPI_API_KEY", os.getenv("DEEPSEEK_API_KEY", "")), repr=False)
    oneapi_base_url: str = field(default=os.getenv("ONEAPI_BASE_URL", os.getenv("ENGRA_LLM_BASE_URL", "https://oneapi-comate.baidu-int.com/v1")), repr=False)
    tavily_api_key: str = field(default=os.getenv("TAVILY_API_KEY", ""), repr=False)
    qwen_api_key: str = field(default=os.getenv("QWEN_API_KEY", ""), repr=False)
    qwen_base_url: str = field(default=os.getenv("QWEN_BASE_URL", ""), repr=False)
    qwen_vision_model: str = field(default=os.getenv("QWEN_VISION_MODEL", "qwen-vl-max"), repr=False)

    # Model tier routing (cost-performance optimization)
    model_tiny: str = field(default=os.getenv("MODEL_TINY", "gpt-5.5"), repr=False)
    model_standard: str = field(default=os.getenv("MODEL_STANDARD", "gpt-5.5"), repr=False)
    model_premium: str = field(default=os.getenv("MODEL_PREMIUM", "gpt-5.5"), repr=False)

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

    def get_model_for_tier(self, tier: str) -> str:
        """Resolve a model tier to the configured model name.

        Args:
            tier: "tiny", "standard", or "premium"

        Returns:
            Model name string for passing to ChatOpenAI.
        """
        tier_map = {
            "tiny": self.model_tiny,
            "standard": self.model_standard,
            "premium": self.model_premium,
        }
        return tier_map.get(tier, self.model_standard)


config = Config()
