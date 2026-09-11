# MEMORY.md — AI-Company 项目记忆

> 最后更新: 2026-07-02
> 项目路径: `~/openclaw/workspace/ai-company/`

## 最近重大改动 (2026-07-02)

### 薄CEO + Escalation 回路架构
- **新建 `src/ceo/dispatcher.py`** — 薄CEO调度中心 (~400行)
  - 三步流程: quick_triage → role_match → dispatch
  - escalation 回路: 角色搞不定→CEO重新匹配→重新分派(最多2轮)
  - 复杂任务自动走PM规划，简单任务直接分派
  - 兜底: 2轮后fallback到developer
- **`src/ceo/graph.py`** — run_ceo 改为委托 dispatcher, 保留 run_ceo_legacy
- **角色匹配全面升级** — 两阶段域匹配 + 通用关键词清洗 + 混合评分 + tie-breaking
- **233个动态角色不再全量匹配** — 先域检测(17类) → 域内评分, 候选池从239降到10-30

## 项目概述
一个 12-agent 多智能体虚拟公司系统，CEO 编排 + LangGraph 工作流 + 打分门禁。

## 已完成优化 (2026-06-12)

### 记忆层重构 (`src/memory/store.py`)
- EpisodeMemory: 文件持久化 (`data/episodes.json`)、Jaccard 去重、Token Overlap 评分搜索
- AgentState: 文件持久化 (`data/agent_states/{id}.json`)、智能工作记忆管理
- ChromaVectorStore: 新增 Chroma 向量存储，语义搜索（Chroma 不可用时降级）
- CLI 命令: `--memory` / `--memory-compact` / `--memory-clear`

### Bug 修复
- `evolution/engine.py`: `get_experience_store()` 函数意外重复，导致 SyntaxError

### Evolution 引擎 (`src/evolution/engine.py`)
- ExperienceStore: 文件持久化 + 自动压缩
- PatternAnalyzer: 部门表现/重试模式/任务类型/分数趋势分析
- AdaptationEngine: 自动路由修正/提示词优化/阈值建议
- Guardrails: 禁止自动改代码/模型/角色删除/路由变更

### CEO Graph 优化 (`src/ceo/graph.py`)
- Triage 快速路径: 关键词预检查跳过 LLM（代码审查/开发/部署/测试/研究/营销）
- PM+Architect 合并为一个节点省 API 调用
- 任务类型感知 (DEVELOPMENT/CODE_REVIEW/RESEARCH/CREATIVE)
- Auditor 引入 PM 验收标准
- PMO 兜底标准（代码质量六维度）

## 待办 / 可优化方向

- [ ] Multi-Agent 协作审查实际应用（技能 `multi-agent-consult` 存在但未深入用于 ai-company）
- [ ] 进化方向/维度限制进一步调优
- [ ] 记忆性能监控（episode 压缩策略是否合理）
- [ ] 生产环境 Chroma/Graphiti/Letta 部署
- [ ] 评分偏低问题根治（模型选择 vs prompt 质量 vs 流程设计）

## 关键决策记录
- DeepSeek 做主力模型（性价比），Claude Sonnet 做审查/架构
- 打分低于 80 需重试，最多 3 次，3 次后 FORCE_APPROVE
- PM + Architect 合并节省成本，等规模上去再拆分
- 角色自动创建需 Auditor 评分 ≥60，试用 3 次成功后晋升正式
