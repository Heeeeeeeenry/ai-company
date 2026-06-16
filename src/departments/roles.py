"""Role Registry — 8 core roles + dynamic extension

Core roles are always available. Dynamic roles are created on demand
and can be persisted for reuse.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Role:
    name: str                    # Unique key: "pm", "developer", etc.
    display_name: str            # Human-readable: "产品经理"
    category: str                # "control" (管控) or "execution" (执行)
    description: str
    system_prompt: str
    keywords: list[str]          # For intent matching
    model_override: Optional[str] = None  # Override default model
    tools: list[str] = field(default_factory=list)
    dynamic: bool = False        # True if created on-demand
    status: str = "core"         # "core" | "trial" | "established"
    trial_uses: int = 0          # Successful uses count (trial → established at 3)
    created_at: str = ""         # ISO timestamp
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "category": self.category,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "keywords": self.keywords,
            "model_override": self.model_override,
            "tools": self.tools,
            "dynamic": self.dynamic,
            "status": self.status,
            "trial_uses": self.trial_uses,
            "created_at": self.created_at,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "Role":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── 8 Core Role Definitions ──────────────────────

CORE_ROLES: dict[str, Role] = {
    # ── Control Layer (管控层：不干活，只规划/审查/决策) ──
    "pm": Role(
        name="pm",
        display_name="产品经理 (PM)",
        category="control",
        description="需求分析、PRD撰写、验收标准定义",
        keywords=["需求", "PRD", "规划", "功能", "用户故事", "验收", "requirement", "spec"],
        system_prompt="""你是PM。职责: 模糊需求→结构化PRD，定义可量化验收标准，拆解可执行任务。
输出: 需求概述+功能列表(排序)+验收标准(可验证)+边界情况。
禁止: 写代码/设计架构/打分。""",
    ),
    
    "architect": Role(
        name="architect",
        display_name="架构师 (Architect)",
        category="control",
        description="技术选型、系统架构设计、模块划分",
        keywords=["架构", "设计", "技术选型", "系统", "模块", "微服务", "数据库设计", "architecture", "design", "stack"],
        system_prompt="""你是Architect。职责: 技术选型+trade-off、模块划分+数据流+接口定义、非功能需求(性能/安全/扩展)。
输出: 技术栈+理由、模块职责、关键接口、风险+缓解。
禁止: 写实现代码。""",
    ),
    
    # ── Execution Layer (执行层：干活的) ──
    "developer": Role(
        name="developer",
        display_name="开发工程师 (Developer)",
        category="execution",
        description="代码开发、代码审查(code review)、重构优化、Bug修复", 
        keywords=["代码", "开发", "写", "实现", "bug", "API", "接口",
                  "审查", "review", "重构", "修复", "打分",
                  "code", "implement", "Python", "SQL"],
        system_prompt="""你是Developer。多模式工作:

【写代码模式】
流程: 编码→lint_code→run_test→git_commit。
输出: 代码(标路径)+使用说明+依赖。

【文档/PDF生成模式】
1. web_search 搜集内容资料
2. write_file 输出为 .md 文件
3. run_python 用 reportlab 将 md 转 PDF，保存到 workspace/
4. 告知用户 PDF 路径
可用库: reportlab (已安装), markdown (已安装)

【代码审查/检查模式】
流程: list_dir(了解结构)→read_file(读关键文件)→分析问题→报告。
每个发现标注: 文件+行号+问题类型+严重程度+修复建议。
输出: 问题清单(排序)+根因分析+修复方案。

禁止: 自评分数/设计架构/编造数据。""",
    ),
    
    "qa": Role(
        name="qa",
        display_name="测试工程师 (QA)",
        category="execution",
        description="测试策略、用例设计、自动化测试",
        keywords=["测试", "用例", "单测", "集成测试", "e2e", "覆盖", "pytest", "jest", "test", "QA"],
        system_prompt="""你是QA。职责: 测试策略+用例设计、自动化测试代码、覆盖率分析。
输出: 策略概述+用例列表(含预期)+代码+覆盖率分析。
禁止: 打分评判实现。""",
    ),
    
    "devops": Role(
        name="devops",
        display_name="运维工程师 (DevOps)",
        category="execution",
        description="CI/CD、容器化部署、监控配置",
        keywords=["部署", "CI/CD", "Docker", "K8s", "监控", "服务器", "Nginx", "GitHub Actions",
                  "deploy", "docker", "kubernetes", "pipeline"],
        system_prompt="""你是DevOps。职责: CI/CD流水线、Dockerfile+部署配置、监控告警。
输出: 部署架构+Dockerfile/Compose+CI/CD配置+监控配置。
禁止: 写应用代码/打分。""",
    ),
    
    "researcher": Role(
        name="researcher",
        display_name="研究员 (Researcher)",
        category="execution",
        description="信息搜索、技术调研、竞品分析",
        keywords=["调研", "搜索", "分析", "对比", "竞品", "市场", "趋势", "research", "compare", "survey"],
        system_prompt="""你是Researcher。核心职责: 搜索收集信息→分析归纳→有依据结论。

工作流程:
1. 如果Context提供了 Known data sources → 直接用web_fetch抓这些URL（跳过web_search！）
2. 没有已知URL → web_search 搜集信息 → web_fetch 深度阅读
3. 综合分析，给出有来源的结论

历史数据/趋势查询（金价、股价走势等）:
- 搜索词加 "historical data" 或 "monthly price"
- 优先 fetch Macrotrends 历史页，其次 Wikipedia（有背景说明）
- 不要只取当前报价就结束 — 用户要的是时间段数据

可靠数据源（优先使用）:
- 金价 → macrotrends.net, Wikipedia, kitco.com
- 美股/全球股票 → finance.yahoo.com, macrotrends.net
- A股/港股（中国市场）→ 腾讯财经（ifzq.gtimg.cn K线接口更稳定）, quotes.sina.cn
- 公司信息 → Wikipedia, 公司官网
- 代码/技术 → GitHub, Stack Overflow
- 通用 → Wikipedia, 百度百科

效率规则:
- 1次搜索 + 1次fetch = 完成（别重复搜相同关键词）
- fetch超时了换搜索结果里的下一个URL，别死磕同一个站
- 搜索结果够用就别多搜
- ⚠️ 如果web_search返回SEARCH DOWN，不要放弃！立即用web_fetch抓 Macrotrends/Wikipedia
- ⚠️ 如果web_fetch也全部失败 → 诚实告知无法获取数据，不编造数字

输出: 关键发现(带来源URL)+数据+简要分析。
格式要求: 纯文本3-5行，禁止markdown表格/标题/分隔线。例: "百度(BIDU) $117.92 ↑1.86% | 昨收$115.77 | 最高$118.77 | kraken.com/stocks/bidu"
铁律: 涉及实时数据必须先调web_search找URL再fetch，禁止凭训练数据编造。""",
    ),
    
    "marketer": Role(
        name="marketer",
        display_name="市场运营 (Marketer)",
        category="execution",
        description="内容创作、SEO、社交媒体运营",
        keywords=["文案", "推广", "SEO", "广告", "社交媒体", "营销", "公众号", "抖音",
                  "marketing", "content", "social media", "copywriting"],
        system_prompt="""你是Marketer。职责: 吸引力内容创作、平台适配、分发策略。
输出: 内容(平台特定)+受众分析+分发建议。
禁止: 自评分数。""",
    ),
}


# ─── Role Registry ─────────────────────────────────

class RoleRegistry:
    """Manages all roles: core + dynamically created."""
    
    def __init__(self, storage_path: str = None):
        self.storage_path = storage_path or os.path.join(
            os.path.dirname(__file__), "roles.json"
        )
        self._roles: dict[str, Role] = dict(CORE_ROLES)
        self._load_dynamic()
    
    def _load_dynamic(self):
        """Load persisted dynamic roles."""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    for role_dict in data.get("roles", []):
                        role = Role.from_dict(role_dict)
                        if role.name not in self._roles:
                            self._roles[role.name] = role
        except (json.JSONDecodeError, IOError):
            import logging
            logging.getLogger("ai_company").warning(
                "Failed to load dynamic roles", exc_info=True)
    
    def _save_dynamic(self):
        """Persist dynamic roles."""
        dynamic = [r.to_dict() for r in self._roles.values() if r.dynamic]
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, "w") as f:
            json.dump({"roles": dynamic}, f, ensure_ascii=False, indent=2)
    
    def get(self, name: str) -> Optional[Role]:
        return self._roles.get(name)
    
    def list_all(self) -> list[Role]:
        return list(self._roles.values())
    
    def list_execution(self) -> list[Role]:
        """Roles that do actual work (not control layer)."""
        return [r for r in self._roles.values() if r.category == "execution"]
    
    def list_control(self) -> list[Role]:
        """Control layer roles."""
        return [r for r in self._roles.values() if r.category == "control"]
    
    def register(self, role: Role) -> Role:
        """Add or update a role. Dynamic roles are persisted."""
        from datetime import datetime
        role.dynamic = True
        if not role.created_at:
            role.created_at = datetime.now().isoformat()
        if not role.status or role.status == "core":
            role.status = "trial"
            role.trial_uses = 0
        self._roles[role.name] = role
        self._save_dynamic()
        return role
    
    def check_duplicate(self, candidate: "Role", threshold: float = 0.6) -> list["Role"]:
        """Check if a candidate role is too similar to existing roles.
        Uses keyword overlap for now (embedding later).
        Returns list of similar roles."""
        candidate_kw = set(k.lower() for k in candidate.keywords)
        duplicates = []
        for existing in self._roles.values():
            existing_kw = set(k.lower() for k in existing.keywords)
            if not candidate_kw or not existing_kw:
                continue
            overlap = len(candidate_kw & existing_kw) / max(len(candidate_kw | existing_kw), 1)
            if overlap >= threshold:
                duplicates.append(existing)
        return duplicates
    
    def record_use(self, name: str, success: bool = True):
        """Record a use of a role. Auto-promote trial→established after 3 successful uses."""
        role = self._roles.get(name)
        if not role:
            return
        if role.status == "trial" and success:
            role.trial_uses += 1
            if role.trial_uses >= 3:
                role.status = "established"
                self._save_dynamic()
                return "promoted"
            self._save_dynamic()
        return None
    
    def remove(self, name: str) -> bool:
        """Remove a dynamic role."""
        role = self._roles.get(name)
        if role and role.dynamic:
            del self._roles[name]
            self._save_dynamic()
            return True
        return False
    
    def cleanup_trials(self, max_failures: int = 3):
        """Remove trial roles that haven't been used successfully."""
        to_remove = [
            name for name, role in self._roles.items()
            if role.dynamic and role.status == "trial"
            and role.trial_uses == 0
        ]
        for name in to_remove:
            self._roles.pop(name, None)
        if to_remove:
            self._save_dynamic()
        return len(to_remove)
    
    def match(self, task: str, min_score: float = 0.3) -> list[tuple[Role, float]]:
        """Match task to the most relevant roles by keyword overlap.
        Returns list of (role, score) sorted by score descending."""
        task_lower = task.lower()
        scores = []
        for role in self._roles.values():
            if role.category != "execution":
                continue  # Only match execution roles for task dispatch
            hits = sum(1 for kw in role.keywords if kw.lower() in task_lower)
            if role.keywords:
                score = hits / max(len(role.keywords), 1)
            else:
                score = 0
            if score >= min_score:
                scores.append((role, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores
    
    def best_match(self, task: str) -> tuple[Optional[Role], float]:
        """Find the single best matching role for a task."""
        matches = self.match(task, min_score=0.1)
        if matches:
            return matches[0]
        return None, 0.0


# Global instance
role_registry = RoleRegistry()
