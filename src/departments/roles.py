"""Role Registry — 8 core roles + domain-filtered dynamic extension

Core roles are always available. Dynamic roles are loaded from roles.json
and matched via a two-stage process:
  1. Domain detection (fast keyword scan → narrow candidate set)
  2. Keyword scoring within the narrowed set

This prevents the "too many skills" distortion where 233+ roles with
generic keywords ("expert", "specialized") pollute matching scores.
"""

import json
import os
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("ai_company.roles")


# ─── Generic keywords to strip from dynamic roles ──
# These appear in 10+ roles and provide no discrimination signal.
_GENERIC_KEYWORDS = {
    "expert", "specialist", "specialized", "masters", "strategist",
    "engineer", "developer", "architect", "manager", "analyst",
    "focuses", "specializes", "builds", "creates", "provides",
    "senior", "premium", "the", "not",
    # Chinese generics
    "开发者", "程序员", "设计师",
}

# ─── Domain detection rules ───────────────────────
# Fast keyword-based domain detection for two-stage matching.
# Ordered by specificity — more specific domains checked first.
_DOMAIN_RULES: list[tuple[str, set[str]]] = [
    ("game", {"游戏", "game", "godot", "unity", "unreal", "roblox", "blender", "shader"}),
    ("gis", {"GIS", "gis", "地图", "地理", "arcgis", "qgis", "geoserver", "空间分析", "遥感"}),
    ("security", {"安全", "渗透", "漏洞", "security", "penetration", "firewall", "加密"}),
    ("blockchain", {"区块链", "solidity", "智能合约", "web3", "crypto", "defi", "nft"}),
    ("academic", {"学术", "论文", "anthropolog", "histor", "geograph", "psycholog", "narratolog"}),
    ("marketing", {"营销", "市场", "SEO", "推广", "广告", "社交媒体", "公众号", "抖音",
                   "marketing", "content", "social media", "copywriting", "brand",
                   "推文", "文案", "小红书", "微博", "快手", "bilibili", "知乎"}),
    ("design", {"UI", "UX", "设计", "design", "visual", "brand", "storytell", "whimsy",
                "prompt engineer", "inclusive"}),
    ("engineering", {"代码", "开发", "API", "接口", "后端", "前端", "数据库", "SQL",
                     "devops", "部署", "CI/CD", "docker", "kubernetes", "测试",
                     "Python", "JavaScript", "TypeScript", "Rust", "Go", "Java",
                     "programming", "coding", "debug", "编译", "运行",
                     "算法", "排序", "爬虫", "写代码", "编程", "重构优化", "修复bug",
                     "engineering", "code", "backend", "frontend", "full-stack"}),
    ("research", {"搜", "搜索", "查一下", "查查", "调研", "了解", "查",
                   "research", "天气", "股价", "金价", "新闻", "最新"}),
    ("finance", {"财务", "金融", "会计", "审计", "税务", "finance", "accounting"}),
    ("sales", {"销售", "sales", "pipeline", "CRM", "crm"}),
    ("testing", {"测试", "QA", "test", "pytest", "jest", "coverage", "benchmark",
                  "单元测试", "集成测试", "e2e", "用例"}),
    ("project", {"项目", "管理", "project", "scrum", "agile", "sprint"}),
    ("support", {"客服", "支持", "support", "ticket", "helpdesk"}),
    ("data", {"数据", "data", "ETL", "etl", "pipeline", "spark", "warehouse"}),
    ("ai_ml", {"AI", "ai", "ML", "ml", "机器学习", "人工智能", "LLM", "llm", "prompt",
               "model", "training", "inference"}),
    ("embedded", {"嵌入式", "firmware", "ESP32", "STM32", "arduino", "RTOS", "rtos"}),
    ("mobile", {"APP", "app", "iOS", "android", "mobile", "移动端", "手机", "flutter", "react native"}),
    ("network", {"网络", "network", "cisco", "router", "switch", "firewall"}),
]

# Domain → minimum keyword hits to trigger (2 for broad domains, 1 for specific)
_DOMAIN_MIN_HITS = {
    "design": 2,       # "设计" appears in too many non-design contexts
    "data": 2,         # "数据" is common in all tech contexts
    "ai_ml": 2,        # "AI"/"model" can match non-AI contexts
    "mobile": 2,       # "app" is too broad
}


@dataclass
class Role:
    name: str                    # Unique key: "pm", "developer", etc.
    display_name: str            # Human-readable: "产品经理"
    category: str                # "control" (管控) or "execution" (执行)
    description: str
    system_prompt: str
    keywords: list[str]          # For intent matching
    domain: str = ""             # Top-level domain for two-stage matching
    model_override: Optional[str] = None
    tools: list[str] = field(default_factory=list)
    dynamic: bool = False
    status: str = "core"
    trial_uses: int = 0
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "category": self.category,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "keywords": self.keywords,
            "domain": self.domain,
            "model_override": self.model_override,
            "tools": self.tools,
            "dynamic": self.dynamic,
            "status": self.status,
            "trial_uses": self.trial_uses,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Role":
        # Auto-extract domain if missing
        if not d.get("domain") and d.get("name"):
            d["domain"] = _extract_domain(d["name"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── 8 Core Role Definitions ──────────────────────

CORE_ROLES: dict[str, Role] = {
    # ── Control Layer ──
    "pm": Role(
        name="pm",
        display_name="产品经理 (PM)",
        category="control",
        domain="core",
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
        domain="core",
        description="技术选型、系统架构设计、模块划分",
        keywords=["架构", "设计", "技术选型", "系统", "模块", "微服务", "数据库设计", "architecture", "design", "stack"],
        system_prompt="""你是Architect。职责: 技术选型+trade-off、模块划分+数据流+接口定义、非功能需求(性能/安全/扩展)。
输出: 技术栈+理由、模块职责、关键接口、风险+缓解。
禁止: 写实现代码。""",
    ),
    # ── Execution Layer ──
    "developer": Role(
        name="developer",
        display_name="开发工程师 (Developer)",
        category="execution",
        domain="core",
        description="代码开发、代码审查、重构优化、Bug修复",
        keywords=["代码", "开发", "写代码", "编程", "实现", "bug", "null", "API", "接口",
                  "审查", "review", "重构", "修复", "爬虫", "spider", "scraper",
                  "算法", "排序", "查找", "数据结构", "函数", "类", "模块",
                  "code", "coding", "implement", "Python", "SQL", "脚本", "程序"],
        system_prompt="""你是Developer。多模式工作:

【写代码模式】
流程: 编码→lint_code→run_test→git_commit。
输出: 代码(标路径)+使用说明+依赖。

【文档/PDF生成模式】
1. web_search 搜集内容资料
2. write_file 输出为 .md 文件
3. run_python 用 reportlab 将 md 转 PDF，保存到 workspace/
4. 告知用户 PDF 路径

【代码审查/检查模式】
流程: list_dir(了解结构)→read_file(读关键文件)→分析问题→报告。

【代码质量闭环】
生成代码后自动执行语法检查+测试，失败自动修复最多3轮。

禁止: 自评分数/设计架构/编造数据。""",
    ),
    "qa": Role(
        name="qa",
        display_name="测试工程师 (QA)",
        category="execution",
        domain="core",
        description="测试策略、用例设计、自动化测试",
        keywords=["测试", "用例", "单测", "单元测试", "集成测试", "e2e", "覆盖",
                  "pytest", "jest", "test", "QA", "测试用例", "test case"],
        system_prompt="""你是QA。职责: 测试策略+用例设计、自动化测试代码、覆盖率分析。
输出: 策略概述+用例列表(含预期)+代码+覆盖率分析。
禁止: 打分评判实现。""",
    ),
    "devops": Role(
        name="devops",
        display_name="运维工程师 (DevOps)",
        category="execution",
        domain="core",
        description="CI/CD、容器化部署、监控配置",
        keywords=["部署", "CI/CD", "Docker", "K8s", "监控", "服务器", "Nginx",
                  "deploy", "docker", "kubernetes", "pipeline"],
        system_prompt="""你是DevOps。职责: CI/CD流水线、Dockerfile+部署配置、监控告警。
输出: 部署架构+Dockerfile/Compose+CI/CD配置+监控配置。
禁止: 写应用代码/打分。""",
    ),
    "researcher": Role(
        name="researcher",
        display_name="研究员 (Researcher)",
        category="execution",
        domain="core",
        description="信息搜索、技术调研、竞品分析",
        keywords=["调研", "搜索", "搜", "查", "分析", "对比", "竞品", "趋势", "定价", "策略",
                  "research", "compare", "survey", "行业报告", "市场分析", "资讯",
                  "股价", "金价", "天气", "新闻", "最新", "数据",
                  "农历", "阴历", "黄历", "节日", "是什么"],
        system_prompt="""你是Researcher。核心职责: 搜索收集信息→分析归纳→有依据结论。

工作流程:
1. 有Known data sources → 直接web_fetch（跳过web_search）
2. 没有URL → web_search → web_fetch深度阅读
3. 综合分析，给出有来源的结论

效率规则:
- 1次搜索+1次fetch=完成
- fetch超时换下一个URL
- 搜索够用不多搜
- web_search DOWN → 立即用web_fetch抓Macrotrends/Wikipedia
- 都失败 → 诚实告知无法获取，不编造数字

输出: 关键发现(带来源URL)+数据+简要分析。纯文本3-5行，禁止表格/标题。
铁律: 实时数据必须先搜索再fetch，禁止凭训练数据编造。""",
    ),
    "marketer": Role(
        name="marketer",
        display_name="市场运营 (Marketer)",
        category="execution",
        domain="core",
        description="内容创作、SEO、社交媒体运营",
        keywords=["文案", "推广", "SEO", "广告", "社交媒体", "营销", "公众号", "抖音",
                  "推文", "内容创作", "小红书", "微博", "bilibili", "增长",
                  "marketing", "content", "social media", "copywriting"],
        system_prompt="""你是Marketer。职责: 吸引力内容创作、平台适配、分发策略。
输出: 内容(平台特定)+受众分析+分发建议。
禁止: 自评分数。""",
    ),
}


# ─── Domain & Keyword Utilities ────────────────────

def _extract_domain(name: str) -> str:
    """Extract domain from role name prefix (e.g. 'engineering-backend-architect' → 'engineering')."""
    parts = name.split("-", 1)
    return parts[0] if len(parts) > 1 else "other"


def _clean_keywords(keywords: list[str], domain: str) -> list[str]:
    """Strip generic noise keywords, keep only domain-discriminating ones.

    Generic keywords like "expert", "specialized" appear in 50+ roles
    and provide zero matching signal — they only add noise.
    """
    cleaned = []
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if kw_lower in _GENERIC_KEYWORDS:
            continue
        # Also skip keywords that are just the domain name (too generic)
        if kw_lower == domain:
            continue
        cleaned.append(kw)
    return cleaned


def _detect_domains(task: str) -> list[str]:
    """Fast domain detection from task text.

    Returns list of domain names sorted by confidence (most hits first).
    This narrows the candidate role set from 233+ to ~10-30 roles.
    """
    task_lower = task.lower()
    domain_scores = []

    for domain, keywords in _DOMAIN_RULES:
        hits = sum(1 for kw in keywords if kw.lower() in task_lower)
        min_hits = _DOMAIN_MIN_HITS.get(domain, 1)
        if hits >= min_hits:
            # Score = hits weighted by total domain keywords (normalize)
            confidence = hits / max(len(keywords), 1)
            domain_scores.append((domain, confidence))

    # Sort by confidence descending
    domain_scores.sort(key=lambda x: x[1], reverse=True)
    return [d for d, _ in domain_scores]


# ─── Role Registry ─────────────────────────────────

class RoleRegistry:
    """Manages all roles: core + domain-filtered dynamic.

    Two-stage matching prevents the "233 role distortion" problem:
      1. Domain detection narrows candidates
      2. Keyword scoring within narrowed set
    Core roles always participate (they're the fallback).
    """

    def __init__(self, storage_path: str = None):
        self.storage_path = storage_path or os.path.join(
            os.path.dirname(__file__), "roles.json"
        )
        self._roles: dict[str, Role] = dict(CORE_ROLES)
        # Domain index: domain → list of role names (built lazily)
        self._domain_index: dict[str, list[str]] = {}
        self._load_dynamic()
        self._build_domain_index()

    def _load_dynamic(self):
        """Load persisted dynamic roles with keyword cleanup."""
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r") as f:
                    data = json.load(f)
                    for role_dict in data.get("roles", []):
                        role = Role.from_dict(role_dict)
                        if role.name in self._roles:
                            continue
                        # Auto-set domain from name if not present
                        if not role_dict.get("domain"):
                            role.domain = _extract_domain(role.name)
                        # Clean noise keywords
                        role.keywords = _clean_keywords(
                            role.keywords,
                            role.domain,
                        )
                        self._roles[role.name] = role
        except (json.JSONDecodeError, IOError):
            logger.warning("Failed to load dynamic roles", exc_info=True)

    def _build_domain_index(self):
        """Build domain → role names index for fast filtering."""
        self._domain_index = {}
        for name, role in self._roles.items():
            domain = role.domain or "other"
            if domain not in self._domain_index:
                self._domain_index[domain] = []
            self._domain_index[domain].append(name)

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
        return [r for r in self._roles.values() if r.category == "execution"]

    def list_control(self) -> list[Role]:
        return [r for r in self._roles.values() if r.category == "control"]

    def register(self, role: Role) -> Role:
        """Add or update a role with auto domain extraction."""
        from datetime import datetime
        role.dynamic = True
        if not role.created_at:
            role.created_at = datetime.now().isoformat()
        if not role.status or role.status == "core":
            role.status = "trial"
            role.trial_uses = 0
        if not role.domain:
            role.domain = _extract_domain(role.name)
        # Clean keywords on registration
        role.keywords = _clean_keywords(role.keywords, role.domain)
        self._roles[role.name] = role
        # Update domain index
        if role.domain not in self._domain_index:
            self._domain_index[role.domain] = []
        if role.name not in self._domain_index[role.domain]:
            self._domain_index[role.domain].append(role.name)
        self._save_dynamic()
        return role

    def check_duplicate(self, candidate: "Role", threshold: float = 0.5) -> list["Role"]:
        """Check keyword overlap with existing roles.

        If candidate has a domain, search within that domain first.
        Falls back to all roles if domain is unknown.
        """
        candidate_kw = set(k.lower() for k in candidate.keywords)
        duplicates = []
        # Narrow search to domain if known; otherwise all roles
        if candidate.domain and candidate.domain in self._domain_index:
            search_names = self._domain_index[candidate.domain]
        else:
            search_names = list(self._roles.keys())

        for name in search_names:
            existing = self._roles.get(name)
            if not existing or existing.name == candidate.name:
                continue
            existing_kw = set(k.lower() for k in existing.keywords)
            if not candidate_kw or not existing_kw:
                continue
            overlap = len(candidate_kw & existing_kw) / max(
                len(candidate_kw | existing_kw), 1
            )
            if overlap >= threshold:
                duplicates.append(existing)
        return duplicates

    def record_use(self, name: str, success: bool = True):
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
        role = self._roles.get(name)
        if role and role.dynamic:
            del self._roles[name]
            # Remove from domain index
            domain = role.domain or "other"
            if domain in self._domain_index:
                self._domain_index[domain] = [
                    n for n in self._domain_index[domain] if n != name
                ]
            self._save_dynamic()
            return True
        return False

    # ─── Two-Stage Matching ────────────────────────

    def match(
        self,
        task: str,
        min_score: float = 0.2,
        max_results: int = 5,
    ) -> list[tuple[Role, float]]:
        """Two-stage role matching: domain filter → keyword scoring.

        Stage 1: Detect domains from task text → narrow candidate pool
        Stage 2: Keyword scoring within candidates + core roles always included

        Core roles get a 1.15x score multiplier to prefer them for ambiguous tasks.
        Dynamic roles need stronger keyword evidence to outrank core roles.

        Args:
            task: The task description text
            min_score: Minimum score to include (lower for broader matching)
            max_results: Maximum number of results to return (prevents overload)
        """
        task_lower = task.lower()

        # ── Stage 1: Domain detection ──
        detected_domains = _detect_domains(task)

        # ── Build candidate pool ──
        # Always include core roles as fallback
        candidate_names: set[str] = set(CORE_ROLES.keys())

        # Add domain-matched dynamic roles
        for domain in detected_domains:
            for name in self._domain_index.get(domain, []):
                if name not in CORE_ROLES:
                    candidate_names.add(name)

        # If no domain detected, try ALL execution roles with moderate threshold
        if not detected_domains:
            for name, role in self._roles.items():
                if role.category == "execution":
                    candidate_names.add(name)
            effective_min_score = max(min_score, 0.20)
        else:
            effective_min_score = min_score

        # ── Stage 2: Keyword scoring within candidates ──
        scores = []
        for name in candidate_names:
            role = self._roles.get(name)
            if not role or role.category != "execution":
                continue

            keywords = role.keywords
            if not keywords:
                continue

            # Hit count with hybrid scoring:
            #   breadth: hits/capped_len — rewards matching many keywords
            #   depth: min(hits/3, 0.5) — raises floor for valid single-keyword matches
            hits = sum(1 for kw in keywords if kw.lower() in task_lower)
            capped_len = min(len(keywords), 10)
            breadth_score = hits / max(capped_len, 1)
            depth_score = min(hits / 3.0, 0.5) if hits > 0 else 0

            # Multi-word keyword bonus (higher confidence)
            bonus = 0
            for kw in keywords:
                kw_lower = kw.lower()
                if len(kw) >= 4 and kw_lower in task_lower:
                    bonus += 0.03

            score = min(breadth_score + depth_score + bonus, 1.0)

            # Core role preference multiplier (with final cap at 1.0)
            if role.status == "core":
                score *= 1.15
            elif role.status == "established":
                score *= 1.0
            elif role.status == "trial":
                score *= 0.85  # Penalty for unproven roles

            score = min(score, 1.0)  # Final cap
            if score >= effective_min_score:
                scores.append((role, round(score, 3)))

        # Sort by score descending, with tie-breaking:
        #   1. Core roles beat dynamic roles
        #   2. Established beats trial
        #   3. Alphabetical by name (deterministic)
        scores.sort(key=lambda x: (
            -x[1],                          # Higher score first
            0 if x[0].status == "core" else 1,  # Core before dynamic
            0 if x[0].status == "established" else 1,  # Established before trial
            x[0].name,                       # Alphabetical
        ))

        # ── Smart truncation ──
        if len(scores) > max_results:
            # Check for a significant score gap after top result
            if len(scores) >= 2 and scores[0][1] >= scores[1][1] * 1.5:
                # Clear winner → return just the top one
                scores = scores[:1]
            else:
                scores = scores[:max_results]

        return scores

    def best_match(self, task: str) -> tuple[Optional[Role], float]:
        """Find the single best matching role for a task."""
        matches = self.match(task, min_score=0.1, max_results=1)
        if matches:
            return matches[0]
        return None, 0.0

    # ─── Domain-aware listing ──────────────────────

    def list_by_domain(self, domain: str) -> list[Role]:
        """List all roles in a specific domain."""
        names = self._domain_index.get(domain, [])
        return [self._roles[n] for n in names if n in self._roles]

    def list_domains(self) -> list[str]:
        """List all known domains."""
        return sorted(self._domain_index.keys())

    def domain_stats(self) -> dict:
        """Return per-domain role counts for diagnostics."""
        core_count = sum(1 for r in self._roles.values() if r.status == "core")
        dynamic_count = sum(1 for r in self._roles.values() if r.dynamic)
        return {
            "total_roles": len(self._roles),
            "core_roles": core_count,
            "dynamic_roles": dynamic_count,
            "domains": {
                d: len(names) for d, names in self._domain_index.items()
            },
        }


# ─── Match Quality Diagnostics ────────────────────

def diagnose_matching(task: str, top_n: int = 10) -> str:
    """Debug tool: show what domains were detected and which roles matched.

    Usage:
        from src.departments.roles import diagnose_matching
        print(diagnose_matching("帮我写一个Python爬虫"))
    """
    domains = _detect_domains(task)
    matches = role_registry.match(task, max_results=top_n)

    lines = [
        f"Task: {task[:80]}",
        f"Detected domains: {domains or '(none — using all roles)'}",
        f"---",
    ]
    for role, score in matches:
        marker = "★" if role.status == "core" else " "
        dom = role.domain or "?"
        lines.append(
            f"  {marker} [{dom}] {role.display_name} "
            f"({role.name}) score={score:.3f}"
        )
    return "\n".join(lines)


# Global instance
role_registry = RoleRegistry()
