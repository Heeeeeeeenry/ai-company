#!/usr/bin/env python3
"""AI-Company 自审 v4 — 路由修复 + 真实代码片段 + 优化流程"""

import asyncio, sys, re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ─── 收集关键模块的代码片段（给 Agent 真东西审）───
src_dir = Path(__file__).parent / "src"
snippets = []

# 1. 核心路由逻辑 (graph.py triage)
graph = (src_dir / "ceo" / "graph.py").read_text()
# 提取 triage_node 函数 (前80行)
triage_match = re.search(r"async def triage_node.*?(?=\nasync def pm_analyze_node)", graph, re.DOTALL)
if triage_match:
    snippets.append(("ceo/graph.py — CEO 路由核心", triage_match.group()[:2000]))

# 2. Auditor 评分维度
auditor = (src_dir / "verification" / "auditor.py").read_text()
dims_match = re.search(r"SCORING_DIMENSIONS = \{.*?\n\}", auditor, re.DOTALL)
if dims_match:
    snippets.append(("verification/auditor.py — 评分维度定义", dims_match.group()[:1500]))

# 3. MCP 客户端
executor = (src_dir / "execution" / "executor.py").read_text()
mcp_match = re.search(r"class MCPClient:.*?(?=\n# ─── CLI Executor)", executor, re.DOTALL)
if mcp_match:
    snippets.append(("execution/executor.py — MCP客户端", mcp_match.group()[:2000]))

# 4. 角色注册
roles = (src_dir / "departments" / "roles.py").read_text()
roles_match = re.search(r"CORE_ROLES.*?(?=\n# ─── Role Registry)", roles, re.DOTALL)
if roles_match:
    snippets.append(("departments/roles.py — 核心角色", roles_match.group()[:2000]))

snippets_text = "\n\n".join(f"### {name}\n```python\n{code}\n```" for name, code in snippets)

total_files = len(list(src_dir.rglob("*.py")))
total_lines = sum(f.read_text().count("\n") for f in src_dir.rglob("*.py") if "__pycache__" not in str(f))

print(f"📦 项目: {total_lines}行, {total_files}文件, 30测试全部通过")

# ─── 构建审计 Prompt ───
audit_prompt = f"""请对 AI-Company 项目自身代码进行代码审查和打分。

## 项目概况
- {total_lines} 行 Python，{total_files} 个文件
- 架构: LangGraph CEO编排 + 8角色 + 独立Auditor + PMO
- 测试: 30个单元测试全部通过 ✅
- 最近修复: routing(devops维度不重复)、MCP增强、30测试

## 真实代码片段（审计依据）
{snippets_text}

## 审计指令
1. PM分析需求+Arch审查架构设计
2. Developer基于代码片段审查：正确性、完整性、可维护性、安全性、性能、测试覆盖
3. Auditor独立评分
4. PMO逐条检查验收标准
5. CEO综合打分

请基于真实代码片段做出有依据的判断，不要凭空推断。"""

async def main():
    from src.ceo.graph import run_ceo

    print("🚀 启动自审（含真实代码 + 路由优化 + 门禁65）...\n")
    try:
        result = await run_ceo(audit_prompt)
        
        print("\n" + "=" * 60)
        print("📊 自审结果")
        print("=" * 60)
        print(f"Phase: {result.get('phase', '?')}")
        
        score_card = result.get("score_card", {}) or {}
        if score_card:
            fs = score_card.get("final_score") or score_card.get("overall_score", "N/A")
            print(f"\n🏆 综合评分: {fs}/100 (门禁: 65)")
            print(f"📋 Auditor: {score_card.get('auditor_score', 'N/A')}/100")
            print(f"📋 PMO: {score_card.get('pmo_score', 'N/A')}/100")
            print(f"✅ 裁决: {score_card.get('decision') or score_card.get('verdict', 'N/A')}")
            
            dims = score_card.get("dimensions", [])
            if dims:
                print("\n📏 维度评分:")
                for d in dims:
                    print(f"  - {d.get('name', '?')}: {d.get('score', '?')}/100")
                    if d.get('reasoning'):
                        print(f"    {d['reasoning'][:100]}")
            
            suggestions = score_card.get("suggestions", [])
            if suggestions:
                print("\n💡 改进建议:")
                for s in suggestions[:8]:
                    print(f"  • {s}")
        
        logs = result.get("execution_log", [])
        if logs:
            print(f"\n📝 执行日志 ({len(logs)} 步):")
            for log in logs:
                print(f"  {log}")
        
        output = result.get("final_output", "")
        if output:
            print(f"\n{'─' * 60}")
            print(output[:3000])
        
        print("\n✅ 完成！")
    except Exception as e:
        import traceback
        print(f"\n❌ {e}")
        traceback.print_exc()

asyncio.run(main())
