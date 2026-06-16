"""路由专项测试 — 验证 triage 关键词预检"""

import pytest
import re


# 模拟 triage_node 的关键词预检逻辑
CODE_REVIEW_KW = [
    r"代码审计", r"代码审查", r"审查代码", r"代码质量", r"code review",
    r"代码打分", r"代码评分", r"审计代码", r"review code",
    r"审计.*项目.*代码", r"审查.*项目.*质量",
    r"项目.*代码.*审查", r"审查.*打分",
]

DEV_KW = [
    r"写.*(?:api|函数|代码|程序|脚本|模块|类|接口)",
    r"实现.*(?:功能|方法|算法|逻辑)",
    r"创建.*(?:api|项目|服务|应用)",
    r"重构", r"修复.*(?:bug|问题)", r"优化.*(?:代码|性能)",
    "implement", "refactor", "build a", "create a",
]

DEPLOY_KW = ["部署", "deploy", "docker", "kubernetes", "k8s", "ci/cd"]
TEST_KW = ["测试", "test", "pytest", "单测", "单元测试"]
RESEARCH_KW = ["调研", "竞品", "research", "compare", "对比"]
MARKETING_KW = ["文案", "推广", "营销", "公众号", "广告"]


def classify_fast(task: str) -> str | None:
    """Fast keyword pre-check (mirrors triage_node logic)."""
    task_lower = task.lower()
    for kw in CODE_REVIEW_KW:
        if re.search(kw, task_lower):
            return "developer"
    for kw in DEV_KW:
        if re.search(kw, task_lower, re.IGNORECASE):
            return "developer"
    for kw in DEPLOY_KW:
        if kw in task_lower:
            return "devops"
    for kw in TEST_KW:
        if kw in task_lower:
            return "qa"
    for kw in RESEARCH_KW:
        if kw in task_lower:
            return "researcher"
    for kw in MARKETING_KW:
        if kw in task_lower:
            return "marketer"
    return None


class TestTriageFastPath:
    def test_code_review_goes_to_developer(self):
        assert classify_fast("审查代码质量并打分") == "developer"
        assert classify_fast("代码审计：检查项目") == "developer"
        assert classify_fast("做一次 code review") == "developer"
        assert classify_fast("请对项目代码进行审查和打分") == "developer"

    def test_self_audit_prompt_routes_to_developer(self):
        prompt = "请对 AI-Company 项目自身代码进行代码审查和打分"
        assert classify_fast(prompt) == "developer", \
            f"Self-audit prompt must route to developer!"

    def test_deploy_goes_to_devops(self):
        assert classify_fast("部署到 Docker") == "devops"
        assert classify_fast("配置 CI/CD pipeline") == "devops"

    def test_test_goes_to_qa(self):
        assert classify_fast("写单元测试") == "qa"
        assert classify_fast("运行 pytest") == "qa"

    def test_research_goes_to_researcher(self):
        assert classify_fast("竞品调研分析") == "researcher"
        assert classify_fast("market research") == "researcher"

    def test_marketing_goes_to_marketer(self):
        assert classify_fast("写一篇公众号推广文案") == "marketer"

    def test_code_task_not_code_review(self):
        """Code implementation tasks should still go to developer via new dev fast-path."""
        assert classify_fast("写一个 Python API") == "developer"
        assert classify_fast("实现用户登录功能") == "developer"

    def test_pure_dev_tasks_fast_path(self):
        """Pure development tasks should be caught by new dev fast-path."""
        assert classify_fast("写一个Flask API") == "developer"
        assert classify_fast("重构代码") == "developer"
        assert classify_fast("修复一个bug") == "developer"
        assert classify_fast("优化代码性能") == "developer"
        assert classify_fast("创建API接口") == "developer"
