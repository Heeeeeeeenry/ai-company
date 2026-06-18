"""Capability Registry — fully pluggable agent capability system (P0.2).

Design:
  Capability: named, self-contained unit of functionality with tools and
              optional pre-requisites. Agents consume capabilities.
  CapabilityRegistry: dynamic registry where capabilities can be
                      registered/unregistered at runtime without code changes.
  Intent → Capabilities: the registry maps user intents (COMMAND, SEARCH, …)
                         to the capabilities they require.

Usage:
  >>> from src.capability.registry import capability_registry, Capability
  >>> capability_registry.resolve("SEARCH")
  [Capability(name='web_search', ...)]
  >>> capability_registry.get_agent_for_intent("CODING")
  'CodingAgent'

Compatibility:
  - get_agent_for_intent(intent) → agent_name (compatible with old callers)
  - export_tools_config() → dict[str, list[str]] (compatible with ROLE_TOOLS)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════════════════════════════════
# Capability
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Capability:
    """A named, self-contained unit of agent functionality.

    Attributes:
        name: Unique identifier, e.g. "web_search", "vision", "wechat".
        description: Human-readable description of what this does.
        agent: Default agent that fulfills this capability.
        tools: Associated tool names.
        requires: List of capability names that must be present first
                  (e.g. vision requires screenshot).
    """
    name: str
    description: str = ""
    agent: str = ""
    tools: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Capability):
            return self.name == other.name
        return NotImplemented

    def to_dict(self) -> dict:
        """Export as plain dict (for serialization / debug)."""
        return {
            "name": self.name,
            "description": self.description,
            "agent": self.agent,
            "tools": list(self.tools),
            "requires": list(self.requires),
        }

    def matches_keyword(self, keyword: str) -> bool:
        """True if this capability's name or description contains the keyword."""
        kw = keyword.lower()
        return kw in self.name.lower() or kw in self.description.lower()


# ═══════════════════════════════════════════════════════════════════
# Default Capabilities
# ═══════════════════════════════════════════════════════════════════

DEFAULT_CAPABILITIES: dict[str, Capability] = {
    "web_search": Capability(
        name="web_search",
        description="网络搜索和信息获取",
        agent="ResearchAgent",
        tools=["web_search", "web_fetch", "market_series"],
    ),
    # Compat alias — planner.py uses "research" but registry canonical name is
    # "web_search".  This alias prevents silent tool loss when resolve_tools()
    # receives capability names from the old planner.
    "research": Capability(
        name="research",
        description="网络搜索和信息获取 (兼容别名 → web_search)",
        agent="ResearchAgent",
        tools=["web_search", "web_fetch", "market_series"],
    ),
    "vision": Capability(
        name="vision",
        description="图像分析和UI理解",
        agent="VisionAgent",
        tools=["vision_analyze", "screenshot"],
    ),
    "shell": Capability(
        name="shell",
        description="Shell命令执行",
        agent="SystemAgent",
        tools=["run_command"],
    ),
    "wechat": Capability(
        name="wechat",
        description="微信消息收发",
        agent="WechatAgent",
        tools=["wechat_send", "wechat_read"],
    ),
    "coding": Capability(
        name="coding",
        description="代码编写和调试",
        agent="CodingAgent",
        tools=["read_file", "write_file", "run_python", "patch", "terminal"],
    ),
    "file_io": Capability(
        name="file_io",
        description="文件读写（无目录浏览）",
        agent="SystemAgent",
        tools=["read_file", "write_file"],
    ),
    "filesystem": Capability(
        name="filesystem",
        description="文件系统操作（含目录浏览）",
        agent="SystemAgent",
        tools=["read_file", "write_file", "list_dir"],
    ),
    "browser": Capability(
        name="browser",
        description="浏览器自动化",
        agent="BrowserAgent",
        tools=["browser_navigate", "browser_click"],
    ),
    "memory": Capability(
        name="memory",
        description="跨会话记忆",
        agent="MemoryAgent",
        tools=["memory_search", "memory_save"],
    ),
    "market_data": Capability(
        name="market_data",
        description="金融市场数据",
        agent="ResearchAgent",
        tools=["market_series"],
    ),
    "messaging": Capability(
        name="messaging",
        description="消息发送",
        agent="WechatAgent",
        tools=["wechat_send"],
    ),
    "vcs": Capability(
        name="vcs",
        description="版本控制",
        agent="CodingAgent",
        tools=["git_commit"],
    ),
}


# ═══════════════════════════════════════════════════════════════════
# Intent → Capability Mapping
# ═══════════════════════════════════════════════════════════════════

INTENT_CAPABILITY_MAP: dict[str, list[str]] = {
    "COMMAND": ["shell"],
    "SEARCH": ["web_search"],
    "RESEARCH": ["web_search", "file_io"],
    "VISION": ["vision"],
    "SOCIAL": ["wechat", "vision"],
    "MEMORY": ["memory"],
    "CODING": ["coding", "filesystem", "web_search"],
    "SYSTEM": ["shell", "filesystem"],
    "FILE": ["file_io"],
    "AUTOMATION": ["shell", "web_search"],
    "GENERAL_CHAT": [],
}


# ═══════════════════════════════════════════════════════════════════
# CapabilityRegistry
# ═══════════════════════════════════════════════════════════════════

class CapabilityRegistry:
    """Pluggable registry of agent capabilities.

    Capabilities can be added/removed at runtime.  Intent resolution
    returns the capabilities required for a given user intent.

    Singleton access via module-level ``capability_registry``.
    """

    def __init__(self):
        self._capabilities: dict[str, Capability] = {}
        self._intent_map: dict[str, list[str]] = dict(INTENT_CAPABILITY_MAP)
        self._register_defaults()

    # ── Core CRUD ──────────────────────────────────────────────

    def register(self, cap: Capability) -> None:
        """Register a new capability (or overwrite an existing one).

        Raises:
            ValueError: If *cap* is not a Capability instance.
        """
        if not isinstance(cap, Capability):
            raise ValueError(
                f"Expected Capability instance, got {type(cap).__name__}"
            )
        self._capabilities[cap.name] = cap

    def unregister(self, name: str) -> None:
        """Remove a capability by name.  No-op if not found."""
        self._capabilities.pop(name, None)

    def get(self, name: str) -> Optional[Capability]:
        """Return a single capability by name, or None."""
        return self._capabilities.get(name)

    def list_all(self) -> list[Capability]:
        """Return every registered capability."""
        return list(self._capabilities.values())

    # ── Intent resolution ──────────────────────────────────────

    def resolve(self, intent: str) -> list[Capability]:
        """Map an intent string to the list of Capability objects.

        Args:
            intent: One of COMMAND, SEARCH, RESEARCH, VISION, SOCIAL,
                    MEMORY, CODING, SYSTEM, FILE, AUTOMATION, GENERAL_CHAT.

        Returns:
            Resolved capabilities.  Unknown intents return an empty list.
        """
        cap_names = self._intent_map.get(intent.upper(), [])
        return [self._capabilities[n] for n in cap_names if n in self._capabilities]

    def get_agent_for_capability(self, name: str) -> str:
        """Capability name → default agent name."""
        cap = self._capabilities.get(name)
        return cap.agent if cap else ""

    def get_tools_for_intent(self, intent: str) -> set[str]:
        """Intent → flat set of all tool names needed."""
        caps = self.resolve(intent)
        tools: set[str] = set()
        for c in caps:
            tools.update(c.tools)
        return tools

    # ── Intent map management ──────────────────────────────────

    def set_intent_capabilities(self, intent: str, capability_names: list[str]) -> None:
        """Associate (or replace) capabilities for a given intent."""
        self._intent_map[intent.upper()] = list(capability_names)

    def get_intent_capability_names(self, intent: str) -> list[str]:
        """Return the *names* (not objects) for an intent."""
        return list(self._intent_map.get(intent.upper(), []))

    # ── Compatibility adapters ─────────────────────────────────

    def get_agent_for_intent(self, intent: str) -> str:
        """Compatibility: return the primary agent for a given intent.

        Uses the first capability in the intent's capability list.
        """
        caps = self.resolve(intent)
        return caps[0].agent if caps else ""

    def export_tools_config(self) -> dict[str, list[str]]:
        """Export capability→tools mapping compatible with old ROLE_TOOLS format.

        Returns a dict where keys are agent names and values are tool lists.

        Example:
          >>> capability_registry.export_tools_config()
          {'ResearchAgent': ['web_search', 'web_fetch', 'market_series'], ...}
        """
        config: dict[str, list[str]] = {}
        for cap in self._capabilities.values():
            if not cap.agent:
                continue
            agent = cap.agent
            if agent not in config:
                config[agent] = []
            for tool in cap.tools:
                if tool not in config[agent]:
                    config[agent].append(tool)
        return config

    def resolve_tools(self, capability_names: list[str]) -> list[str]:
        """Given capability names, return deduplicated tool list.

        Compatible with the old ``resolve_tools`` from planner.py.

        Args:
            capability_names: e.g. ["web_search", "coding", "file_io"]

        Returns:
            Flat, deduplicated tool name list.
        """
        tools: list[str] = []
        seen: set[str] = set()
        for cn in capability_names:
            cap = self._capabilities.get(cn)
            if cap:
                for t in cap.tools:
                    if t not in seen:
                        tools.append(t)
                        seen.add(t)
        return tools

    # ── Internal ───────────────────────────────────────────────

    def _register_defaults(self) -> None:
        """Register all DEFAULT_CAPABILITIES."""
        for cap in DEFAULT_CAPABILITIES.values():
            self.register(cap)


# ═══════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════

capability_registry = CapabilityRegistry()


def get_capability_registry() -> CapabilityRegistry:
    """Return the module-level singleton CapabilityRegistry."""
    return capability_registry


# ═══════════════════════════════════════════════════════════════════
# Module-level convenience re-export (compatible with old resolve_tools)
# ═══════════════════════════════════════════════════════════════════

# Provide a drop-in replacement for resolve_tools() that reads from
# the capability registry instead of the old CAPABILITIES dict.
def resolve_tools(capability_names: list[str]) -> list[str]:
    """Resolve capability names → flat tool list (compatibility shim).

    Prefer this over the old departments.agents.CAPABILITIES path.
    """
    return capability_registry.resolve_tools(capability_names)
