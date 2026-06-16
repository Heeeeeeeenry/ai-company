# AI-Company 路由质检报告

> **审计时间**: 2026-06-12 11:37 CST
> **审计范围**: `src/ceo/graph.py` → `triage_node` 路由决策系统
> **审计方法**: 静态代码分析 + 动态单元测试 + 集成测试 + 关键词矩阵分析

---

## 📊 总体评分

| 指标 | 值 |
|------|-----|
| **路由质量总分** | **56/100** |
| 评级 | 🔴 D |
| Fast-path 基础准确率 | 97.8% |
| 快速路径通过/失败/警告 | 44/0/1 |
| 架构与覆盖扣分 | -42 |

---

## 🧪 1. Fast-Path 关键词路由测试

**测试用例数**: 45
**通过**: 44 | **失败**: 0 | **警告**: 1

### ⚠️ 优先级冲突警告

| 任务 | 期望 | 实际 | 分析 |
|------|------|------|------|
| 测试代码质量 | qa | developer | ⚠️ 可能并非用户意图：'测试' 先命中，但可能是想审查代码质量 |

### 正则有效性

✅ 所有 `CODE_REVIEW_KW` 中的正则表达式均有效。

### 短英文词误匹配（无词边界）

- ⚠️ Short English keyword without word-boundary check caused incorrect routing
- ⚠️ Short English keyword without word-boundary check caused incorrect routing
- ⚠️ Short English keyword without word-boundary check caused incorrect routing
- ⚠️ Short English keyword without word-boundary check caused incorrect routing

### Fast-Path 覆盖矩阵

| 类别 | 目标部门 | 关键词数 | 覆盖任务 |
|------|----------|---------|---------|
| 代码审查 | developer | 13 | ✅ 审查代码、Code Review |
| 部署运维 | devops | 6 | ✅ 部署、Docker、K8s |
| 测试 | qa | 5 | ✅ 单测、Pytest |
| 调研 | researcher | 5 | ✅ 竞品、Research |
| 营销 | marketer | 5 | ✅ 公众号、文案 |
| **纯开发** | developer | **0** | ❌ 写API、实现功能、重构、修复bug |

---

## 🔬 2. 关键词稀释问题（CRITICAL）

### 背景

`RoleRegistry.best_match()` 使用 `hits/len(keywords)` 计算匹配分数。
Developer 拥有 25 个关键词（远超其他角色 5-10 个），导致即使匹配了 2 个关键词，得分也只有 0.08，低于 `min_score=0.1` 阈值。

### 各角色关键词数对比

| 角色 | 关键词数 | 匹配1个得分 | 匹配2个得分 | 匹配3个得分 |
|------|---------|-----------|-----------|-----------|
| developer | 25 | 0.040 | 0.080 | 0.120 |
| devops | 12 | 0.083 | 0.167 | 0.250 |
| marketer | 12 | 0.083 | 0.167 | 0.250 |
| qa | 10 | 0.100 | 0.200 | 0.300 |
| researcher | 10 | 0.100 | 0.200 | 0.300 |

### 具体影响

| 任务 | 匹配关键词 | 得分 | 通过 min_score=0.1? |
|------|-----------|------|---------------------|
| 写一个 Flask API | 写, API, Flask (3个) | 0.120 | ✅ |
| 实现用户登录功能 | 实现 (1个) | 0.040 | ❌ |
| 修复 bug：崩溃 | 修复, bug (2个) | 0.080 | ❌ |
| 重构数据库查询 | 重构, 代码 (2个) | 0.080 | ❌ |

### 后果

- **best_match 对 developer 任务几乎失效**：多数开发任务只匹配 1-2 个关键词，被 min_score 排除
- **完全依赖 LLM 路由**：triage_node 中 `if best and score > 0.15` 检查对这些任务形同虚设
- **对比其他角色**：qa(8词)匹配1个得 0.125，researcher(10词)匹配1个得 0.100，都能通过阈值
- **建议**: 将 min_score 从 0.1 降至 0.03，或改用 `hits/max(len(self._roles), 1)` 按最大值归一化

---

## ⛓️ 3. Fallback 链完整性审查

### 当前路由决策链

```
用户输入
  │
  ├── [L1] Fast-path 关键词预检
  │    └── 命中 → 直接路由，跳过 L2-L4
  │
  ├── [L2] LLM 意图检测 (DeepSeek API)
  │    ├── 匹配执行角色 → 路由
  │    ├── GENERAL → developer 默认
  │    └── 无法识别 → 进入 L3
  │
  ├── [L3] RoleRegistry.best_match (评分阈值 > 0.15)
  │    ├── score > 0.15 → 路由到最佳角色
  │    └── score ≤ 0.15 → 进入 L4
  │
  └── [L4] 兜底 → developer
```

### 评估

**🟡 best_match 和 triage_node 之间的阈值间隙**
2 个任务落在 0.1-0.15 间隙中，best_match 找到匹配但 triage_node 拒绝

- `写一个 Flask API` → developer (score=0.12) Passes best_match(min_score=0.1) but fails triage_node(score>0.15)
- `帮我分析一下这个需求` → researcher (score=0.1) Passes best_match(min_score=0.1) but fails triage_node(score>0.15)

**🟡 空输入触发 LLM**
空字符串 '' 无 fast-path 匹配，直接进入 LLM 意图检测 → 浪费 API 调用。triage_node 缺少输入前置验证: len(task.strip()) < 2 → 直接拒绝。

**🟡 英文任务 fast-path 覆盖率仅 3/10**
Fast-path 关键词以中文为主，缺少英文通用开发/测试/营销关键词

---

## 💸 4. LLM 调用浪费分析

**✅ Fast-path 命中后不调用 LLM (if/else 结构)**
graph.py:151-184: fast-path 和 LLM 在 if/else 分支中，命中 fast-path 后直接返回，不执行 await llm.ainvoke()。架构设计正确。

**🟡 日常开发请求中约 10/16 需 LLM 路由**
纯开发任务(写/实现/重构/修复)占日常请求的大部分，但均未命中 fast-path。

### 集成测试验证

- 集成测试: 6 通过, 0 失败
- LLM 调用触发: ['写一个 Flask API']

---

## 🐛 5. 问题清单（按严重程度）

### 🔴 HIGH

1. **Developer 关键词数是 qa 的 2.5 倍，导致匹配评分畸低**
   Developer: 25 keywords, qa: 10 keywords.
best_match 使用 hits/len(keywords) 计算评分，开发者需要匹配 3 个关键词 (score=0.12) 才能超过 min_score=0.1 阈值，而 qa 只需 1 个。
导致 '写一个 React 组件' (hit '写'+'React'=0.08) 被 best_match 排除。

2. **Developer 关键词数是 devops 的 2.1 倍，导致匹配评分畸低**
   Developer: 25 keywords, devops: 12 keywords.
best_match 使用 hits/len(keywords) 计算评分，开发者需要匹配 3 个关键词 (score=0.12) 才能超过 min_score=0.1 阈值，而 devops 只需 1 个。
导致 '写一个 React 组件' (hit '写'+'React'=0.08) 被 best_match 排除。

3. **Developer 关键词数是 researcher 的 2.5 倍，导致匹配评分畸低**
   Developer: 25 keywords, researcher: 10 keywords.
best_match 使用 hits/len(keywords) 计算评分，开发者需要匹配 3 个关键词 (score=0.12) 才能超过 min_score=0.1 阈值，而 researcher 只需 1 个。
导致 '写一个 React 组件' (hit '写'+'React'=0.08) 被 best_match 排除。

4. **Developer 关键词数是 marketer 的 2.1 倍，导致匹配评分畸低**
   Developer: 25 keywords, marketer: 12 keywords.
best_match 使用 hits/len(keywords) 计算评分，开发者需要匹配 3 个关键词 (score=0.12) 才能超过 min_score=0.1 阈值，而 marketer 只需 1 个。
导致 '写一个 React 组件' (hit '写'+'React'=0.08) 被 best_match 排除。

### 🟡 MEDIUM

1. **best_match 和 triage_node 之间的阈值间隙**
   2 个任务落在 0.1-0.15 间隙中，best_match 找到匹配但 triage_node 拒绝

2. **空输入触发 LLM**
   空字符串 '' 无 fast-path 匹配，直接进入 LLM 意图检测 → 浪费 API 调用。triage_node 缺少输入前置验证: len(task.strip()) < 2 → 直接拒绝。

3. **英文任务 fast-path 覆盖率仅 3/10**
   Fast-path 关键词以中文为主，缺少英文通用开发/测试/营销关键词

4. **日常开发请求中约 10/16 需 LLM 路由**
   纯开发任务(写/实现/重构/修复)占日常请求的大部分，但均未命中 fast-path。

### 🟢 LOW

1. **False positive: 'contest' → qa via 'Test(test)'**
   Short English keyword without word-boundary check caused incorrect routing

2. **False positive: 'protest' → qa via 'Test(test)'**
   Short English keyword without word-boundary check caused incorrect routing

3. **False positive: 'latest news' → qa via 'Test(test)'**
   Short English keyword without word-boundary check caused incorrect routing

4. **False positive: 'testament' → qa via 'Test(test)'**
   Short English keyword without word-boundary check caused incorrect routing

### ✅ PASS

1. **Fast-path 命中后不调用 LLM (if/else 结构)**
   graph.py:151-184: fast-path 和 LLM 在 if/else 分支中，命中 fast-path 后直接返回，不执行 await llm.ainvoke()。架构设计正确。

---

## 💡 6. 改进建议（按优先级）

### 🔴 P0 — 必须修复

1. **补充纯开发任务 fast-path 关键词**

   ```python
   # 在 CODE_REVIEW_KW 检查前新增:
   GENERAL_DEV_KW = ["写", "实现", "开发", "重构", "修复", "bugfix",
                     "编写", "编程", "新建", "创建", "添加功能"]
   # 命中后: fast_department = "developer"
   ```

2. **修复关键词稀释问题**

   ```python
   # Option A: 降低 min_score 阈值
   def best_match(self, task: str) -> tuple[Optional[Role], float]:
       matches = self.match(task, min_score=0.03)  # 从 0.1 → 0.03

   # Option B: 改用绝对命中数
   matches = self.match_absolute(task, min_hits=1)  # 匹配1个关键词即返回
   ```

### 🟡 P1 — 建议修复

3. **增加空输入前置验证**

   ```python
   if len(state["user_request"].strip()) < 2:
       return {"department": "developer", "phase": "deliver",
               "execution_log": ["[TRIAGE] Empty input → rejected"],
               "final_output": "请输入有效的请求内容"}
   ```

4. **补充英文 fast-path 关键词**

   ```python
   # Developer: implement, write code, build, create, fix, refactor
   # QA: unit test, integration test, e2e test
   # DevOps: CI/CD pipeline (already covered), infrastructure
   # Researcher: competitor, market research, benchmark
   # Marketer: marketing, content, copywriting, seo
   ```

5. **修复 fast-path 关键词优先级对齐**

   - 当前顺序: code_review > deploy > test > research > marketing
   - 建议: 将纯开发置顶，或用更精确的关键词（如 `写.*单元测试` 替代 `测试`）
   - 对 "审查部署配置" 等歧义输入，路由到 devops 是合理的，但需注意 "审查代码" 的优先级

### 🟢 P2 — 可选优化

6. **使用词边界匹配**

   ```python
   # Before: if kw in task_lower
   # After: if re.search(r'\\b' + re.escape(kw) + r'\\b', task_lower)
   ```

7. **调整 triage_node 中 L3 阈值以消除间隙**

   ```python
   # Before: if best and score > 0.15:
   # After:  if best and score > 0.05:  # 对齐 best_match 的 min_score=0.1
   # 或统一为: matches = self.match(task, min_score=0.05); score > 0.05
   ```

8. **添加路由审计日志字段**

   ```python
   # 在 execution_log 中增加:
   # [TRIAGE] method=fast_path|LLM|best_match|default, confidence=X.XX, llm_called=True|False
   ```

---

## 📋 7. 集成测试结果

| 指标 | 值 |
|------|-----|
| 通过 | 6 |
| 失败 | 0 |
| LLM 调用触发 | 写一个 Flask API |

---

## 📝 总结

### 评分: 56/100 🔴 D

| 维度 | 评分 | 说明 |
|------|------|------|
| Fast-path 基本路由 | ✅ 98% | 已有场景路由准确 |
| 覆盖率 | ⚠️ 仅50% | 纯开发/英文/边界输入未覆盖 |
| 关键词稀释 | 🔴 P0 | Developer 关键词稀释导致 best_match 失效 |
| Fallback 链 | ⚠️ 可行 | 优先级正确，但 L3 在实践中对 dev 任务不可达 |
| LLM 浪费 | ⚠️ 中等 | 约40-60%请求触发 LLM，fast-path 命中后不浪费 |
