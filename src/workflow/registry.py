"""Capability Registry — pluggable agent capabilities with auto-discovery.

Agents register their capabilities. WorkflowPlanner queries the registry
to find agents that can fulfill required capabilities.

Existing agents from src/departments/roles.py are auto-imported.
New agents (WeChat, Vision, etc.) register here.
"""

from typing import Optional
from dataclasses import dataclass, field


@dataclass
class AgentCapability:
    name: str           # e.g. "wechat_send"
    display: str        # e.g. "WeChat Message"
    description: str    # What this capability does
    agent: str          # Which agent provides it: "devops", "wechat", etc.
    tools: list[str]    # Tools used: ["wechat_send"]
    category: str = "action"  # "perception", "action", "verification", "research"


class CapabilityRegistry:
    """Registry of all available agent capabilities."""
    
    def __init__(self):
        self._capabilities: dict[str, AgentCapability] = {}
        self._register_builtin()
    
    def _register_builtin(self):
        """Register all built-in capabilities."""
        builtins = [
            # Perception
            AgentCapability("web_search", "Web Search", "Search the internet", "researcher", ["web_search", "web_fetch"], "perception"),
            AgentCapability("screen_capture", "Screen Capture", "Take screenshots for visual analysis", "devops", ["run_python"], "perception"),
            AgentCapability("vision_analyze", "Vision Analysis", "Analyze screenshots with Qwen-VL", "devops", ["run_python"], "perception"),
            
            # Action
            AgentCapability("wechat_send", "WeChat Send", "Send WeChat messages via AppleScript", "devops", ["wechat_send"], "action"),
            AgentCapability("code_gen", "Code Generation", "Write and modify code", "developer", ["write_file", "run_python"], "action"),
            AgentCapability("file_ops", "File Operations", "Read/write/list files", "developer", ["read_file", "write_file", "list_dir"], "action"),
            AgentCapability("shell_exec", "Shell Execution", "Run shell commands", "devops", ["run_python"], "action"),
            
            # Research
            AgentCapability("market_data", "Market Data", "Query financial market data", "researcher", ["market_series"], "research"),
            AgentCapability("web_fetch", "Web Fetch", "Fetch web page content", "researcher", ["web_fetch"], "research"),
            
            # Verification
            AgentCapability("code_review", "Code Review", "Review code quality and security", "qa", ["read_file", "lint_code"], "verification"),
            AgentCapability("vision_verify", "Vision Verify", "Verify actions via screenshot analysis", "devops", ["run_python"], "verification"),
            AgentCapability("fact_check", "Fact Check", "Verify factual claims via web search", "researcher", ["web_search"], "verification"),
            
            # Document
            AgentCapability("pdf_gen", "PDF Generation", "Generate PDF documents", "developer", ["write_file", "run_python"], "action"),
        ]
        for c in builtins:
            self.register(c)
    
    def register(self, cap: AgentCapability):
        self._capabilities[cap.name] = cap
    
    def get(self, name: str) -> Optional[AgentCapability]:
        return self._capabilities.get(name)
    
    def find_by_agent(self, agent: str) -> list[AgentCapability]:
        return [c for c in self._capabilities.values() if c.agent == agent]
    
    def find_by_category(self, category: str) -> list[AgentCapability]:
        return [c for c in self._capabilities.values() if c.category == category]
    
    def match_task(self, task: str) -> list[AgentCapability]:
        """Match capabilities needed for a task (keyword-based fast path)."""
        task_lower = task.lower()
        matches = []
        
        keyword_map = {
            "wechat_send": ["微信", "wechat", "发送消息", "发消息"],
            "screen_capture": ["截图", "屏幕", "screenshot", "查看.*屏幕", "检测.*软件"],
            "vision_analyze": ["视觉", "识别.*屏幕", "分析.*截图", "vision"],
            "vision_verify": ["验证.*发送", "检查.*消息", "verify"],
            "web_search": ["搜索", "查", "搜", "search", "最新", "新闻"],
            "market_data": ["股价", "股票", "金价", "汇率", "market"],
            "code_gen": ["写代码", "开发", "实现", "修复", "bug", "fix"],
            "pdf_gen": ["pdf", "报告", "文档", "导出"],
            "fact_check": ["验证", "核实", "fact.?check"],
        }
        
        for cap_name, keywords in keyword_map.items():
            import re
            for kw in keywords:
                if re.search(kw, task_lower):
                    cap = self.get(cap_name)
                    if cap and cap not in matches:
                        matches.append(cap)
                    break
        
        return matches
    
    def list_all(self) -> list[AgentCapability]:
        return list(self._capabilities.values())


# Singleton
_registry: Optional[CapabilityRegistry] = None

def get_capability_registry() -> CapabilityRegistry:
    global _registry
    if _registry is None:
        _registry = CapabilityRegistry()
    return _registry
