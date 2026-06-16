# 打分机制质检报告 (Scoring Mechanism Audit)

> 审计日期: 2026-06-12 | 审计范围: AI-Company 全量评分体系 | 测试: 43 项全通过

---

## 1. Auditor 维度验证

### 1.1 部门维度权重之和

| 部门 | 维度数 | 权重合计 | 状态 |
|------|--------|----------|------|
| developer | 6 | 1.00 | ✅ |
| qa | 5 | 1.00 | ✅ |
| devops | 5 | 1.00 | ✅ |
| research | 5 | 1.00 | ✅ |
| marketing | 5 | 1.00 | ✅ |
| operation | 5 | 1.00 | ✅ |
| DEFAULT | 3 | 1.00 | ✅ |

### 1.2 Developer 维度逐项确认

| 维度 | 权重 | 验证 |
|------|------|------|
| 正确性 | 0.30 | ✅ |
| 完整性 | 0.20 | ✅ |
| 可维护性 | 0.15 | ✅ |
| 安全性 | 0.15 | ✅ |
| 性能 | 0.10 | ✅ |
| 测试覆盖 | 0.10 | ✅ |
| **合计** | **1.00** | ✅ |

### 1.3 分数范围验证

- 所有维度分数在 `AuditReport` 构造时通过 `max(0, min(100, score))` 钳制到 [0, 100]
- `overall_score` 同理钳制 → ✅

### 1.4 devops vs operation 维度差异

| devops | operation |
|--------|-----------|
| 正确性 (0.25) | 响应速度 (0.25) |
| 安全性 (0.25) | 资源效率 (0.25) |
| 可靠性 (0.20) | 稳定性 (0.20) |
| 成本效率 (0.15) | 可观测性 (0.15) |
| 可维护性 (0.15) | 自动化程度 (0.15) |

✅ **已修正**：devops 关注配置安全与可靠性，operation 关注运行效率与可观测性，两者维度完全独立。

### 1.5 所有执行角色维度覆盖

5 个核心执行角色 (developer, qa, devops, researcher, marketer) 均在 `SCORING_DIMENSIONS` 中有对应维度定义。未注册的动态角色回退到 `DEFAULT_DIMENSIONS`。

---

## 2. 加权计算验证

### 2.1 聚合公式

```python
final_score = round(auditor_score * 0.7 + pmo_score * 0.3, 1)
```

| 场景 | Auditor | PMO | Final | 预期 |
|------|---------|-----|-------|------|
| 完美 | 100 | 100 | 100.0 | ✅ |
| 均衡良好 | 80 | 80 | 80.0 | ✅ |
| 及格线 | 60 | 60 | 60.0 | ✅ |
| Auditor偏高 | 90 | 50 | 78.0 | ✅ |
| PMO偏高 | 50 | 90 | 62.0 | ✅ |

权重和: 0.7 + 0.3 = 1.0 ✅

### 2.2 默认回退值

当 `score_card` 或 `pmo_result` 为空时：

| 缺失项 | 默认值 | 影响 |
|--------|--------|------|
| score_card 为空 | auditor_score=60 | 及格线，偏向保守 |
| pmo_result 为空 | pmo_score=70 | 稍高于及格，偏向宽容 |
| 两者都空 | final=63.0 | 低于 practical threshold |

### 2.3 裁决优先级

```
REJECT（Auditor REJECT 或 PMO FAIL）
  ↓
REVISE（Auditor REVISE 或 PMO < 60）
  ↓
Gate Check（final >= GATE_FINAL_SCORE ? APPROVE : REVISE）
```

✅ **优先级正确**：Auditor 和 PMO 拥有否决权，防止高分覆盖定性判断。

### 2.4 重试逻辑

```
max_retries = 3
if next_action != "deliver" and retry_count >= max_retries:
    decision = "FORCE_APPROVE"
    next_action = "deliver"
```

| retry_count | 行为 | 验证 |
|-------------|------|------|
| 0 | 正常裁决 ✅ | |
| 1 | 正常裁决 ✅ | |
| 2 | 正常裁决 ✅ | |
| 3 | **FORCE_APPROVE** ✅ | |
| ≥3 | **FORCE_APPROVE** ✅ | |

✅ **重试上限正确**：第 4 次尝试（retry_count=3）触发强制放行，避免死循环。

---

## 3. PMO 合规检查

### 3.1 验收标准优先级链

```
1. PM 的 PRD.acceptance_criteria（主要来源）
     ↓ 为空时
2. Plan.steps[N].acceptance_criteria（计划步骤定义）
     ↓ 为空时
3. 硬编码默认标准（兜底）
```

✅ 优先级设计合理，逐级回退。

### 3.2 默认验收标准

```text
1. 代码能正常运行，无明显逻辑错误
2. 错误处理和边界情况完善
3. 命名规范、注释清晰、结构合理
4. 无明显安全漏洞（注入、硬编码密钥等）
5. 包含必要的测试用例
6. 输出格式符合要求，可直接使用
```

覆盖维度：

| 质量维度 | 覆盖 | 标准条目 |
|----------|------|---------|
| 正确性 | ✅ | #1 |
| 错误处理 | ✅ | #2 |
| 可维护性 | ✅ | #3 |
| 安全性 | ✅ | #4 |
| 测试 | ✅ | #5 |
| 可用性 | ✅ | #6 |

✅ 6 条标准覆盖了代码质量的核心维度。

⚠️ **局限性**：默认标准完全面向代码场景。对于 researcher、marketer 等非开发部门，如无 PRD 定义验收标准，PMO 将用代码标准去检查调研报告或营销文案，不合理。**建议**：对非 developer 部门提供部门级默认标准，或在没有 PRD 时跳过 PMO 检查（仅依赖 Auditor）。

### 3.3 解析异常处理

PMO 返回不可解析 JSON 时：
```python
return {
    "criteria_met": ["自动通过"],
    "criteria_failed": [],
    "compliance_score": 70,
    "verdict": "PASS",
}
```

✅ 异常时默认放行，避免因工具故障阻断流程。

---

## 4. 门禁阈值

### 4.1 所有门禁值

| 门禁 | 默认值 | 运行时(.env) | 范围 |
|------|--------|-------------|------|
| GATE_PRD_SCORE | 70 | **60** | [0,100] ✅ |
| GATE_ARCH_SCORE | 75 | 75 | [0,100] ✅ |
| GATE_CODE_SCORE | 70 | **60** | [0,100] ✅ |
| **GATE_FINAL_SCORE** | **80** | **65** | [0,100] ✅ |

> 运行时值由 `.env` 覆盖，当前环境中 GATE_FINAL_SCORE=65。

### 4.2 GATE_FINAL_SCORE 分析

**代码默认 80**：对应 Auditor 系统中 "良好" 档位 (80=良好)。
- auditor=80, pmo=80 → final=80.0 ✅ APPROVE
- auditor=75, pmo=90 → final=79.5 ❌ REVISE
- 要求两者均分 ≥ 80（或一高一低但总和达标）

**运行时 65**：偏宽松。
- auditor=60(及格), pmo=80 → final=66 ✅ APPROVE
- auditor=55(不及格), pmo=90 → final=65.5 ✅ APPROVE
- 允许 Auditor 不及格但靠 PMO 高分拉过阈值

### 4.3 建议

| 环境 | 推荐值 | 理由 |
|------|--------|------|
| 开发/实验 | 60-65 | 允许快速迭代，不因评分卡流程 |
| 预发布/CI | 70-75 | 需要基本质量保障 |
| 生产/正式上线 | 80 | Auditor 系统 "良好" 档位，确保交付质量 |

---

## 5. 发现的问题

### 🔴 P1: auditor_node 未传递验收标准给 Auditor

**位置**: `src/ceo/graph.py:auditor_node()`

```python
# 当前代码
report = await auditor.audit(
    department=department,
    task=task,
    output=output,
)
# acceptance_criteria 参数未传递 → 始终为 ""
```

**影响**: Auditor 始终看到 "验收标准: 未指定"，无法根据具体需求评估产出物。PM 辛苦写的验收标准白费了。

**修复**:
```python
prd = state.get("prd") or {}
criteria = "\n".join(prd.get("acceptance_criteria", [])) or ""
report = await auditor.audit(
    department=department,
    task=task,
    output=output,
    acceptance_criteria=criteria,
)
```

### 🟡 P2: AuditReport.overall_score 未从维度加权计算

**位置**: `src/verification/auditor.py:AuditReport`

**现状**: `overall_score` 直接使用 LLM 返回值，不通过 `sum(d.score * d.weight)` 验证一致性。

**风险**: LLM 可能返回与各维度分数不一致的 `overall_score`（例如维度评分普遍 50 但 overall=90）。

**建议**: 添加一致性检查——若 LLM 的 overall 与加权计算结果偏差超过 15 分，使用加权计算结果覆盖，并记录 warning。

### 🟡 P3: PMO 默认验收标准偏向开发场景

**位置**: `src/ceo/graph.py:pmo_node()`

硬编码的 6 条默认标准全部面向代码质量。对于 researcher（调研报告）、marketer（营销文案）、devops（部署配置）等非代码产出，这些标准不完全适用。

**建议**: 为各执行部门提供领域特定的默认验收标准字典，或对非 developer 部门在无 PRD 时跳过 PMO 检查。

### 🟢 P4: 维度权重查找的回退值为固定 0.25

**位置**: `src/verification/auditor.py:audit()`

```python
weight=dim.get("weight", 0.25) if (dim := next(...)) else 0.25
```

如果 LLM 返回的维度名称无法匹配预定义维度，权重固定为 0.25。若同时多个维度无法匹配，权重总和将偏离 1.0。

**建议**: 对未匹配的维度使用 `1.0 / len(raw_dimensions)` 均分权重，或记录 warning。

### 🟢 P5: operation 维度存在但无对应核心角色

`SCORING_DIMENSIONS["operation"]` 已定义（运维运行态维度），但与 devops 区分良好。目前无 `operation` 核心角色——作为预留设计，非缺陷。

---

## 6. 测试执行摘要

```text
tests/test_scoring_audit.py - 43 tests

TestDimensionWeights       8/8  ✅  所有权重和 1.0
TestDimensionScoreRanges   2/2  ✅  分数钳制验证
TestDevopsVsOperation      3/3  ✅  维度差异化
TestWeightedCalculation    4/4  ✅  加权公式 + 边界
TestRetryLogic             4/4  ✅  FORCE_APPROVE 逻辑
TestVerdictPriority        3/3  ✅  裁决优先级
TestAuditorNodeCriteria    2/2  ✅  接口签名验证
TestPMOFallback            3/3  ✅  Fallback 覆盖
TestPMOResponseParsing     1/1  ✅  解析异常
TestGateThresholds         5/5  ✅  门禁阈值
TestEndToEndScoring        4/4  ✅  端到端模拟
TestDimensionWeightLookup  2/2  ✅  权重查找
TestAuditReportIntegrity   1/1  ✅  数据完整性 (已知 gap)
TestAllExecutionRoles      1/1  ✅  角色覆盖
─────────────────────────────────
TOTAL                     43/43 ✅  ALL PASSED
```

---

## 7. 结论

| 审计项 | 评级 | 说明 |
|--------|------|------|
| Auditor 维度权重 | ✅ 通过 | 6 部门全 1.0，devops/operation 已分化 |
| 加权计算公式 | ✅ 通过 | auditor*0.7 + pmo*0.3，权重和=1 |
| 裁决优先级 | ✅ 通过 | REJECT > REVISE > Gate Check |
| 重试/FORCE_APPROVE | ✅ 通过 | 3 次重试后强制放行 |
| PMO 合规检查 | ⚠️ 通过 | Fallback 覆盖核心维度，但偏向代码场景 |
| PMO 解析异常处理 | ✅ 通过 | 安全回退，默认放行 |
| 门禁阈值 | ⚠️ 通过 | 运行时 65 偏宽松，代码默认 80 合理 |
| 验收标准传递 | 🔴 缺陷 | auditor_node 未传 criteria 给 Auditor |

**总体评价**: 评分体系设计合理，核心计算逻辑（维度权重、加权聚合、裁决优先级、重试上限）全部正确。存在 1 个 P1 缺陷（验收标准未传递）和 2 个 P2 改进项（overall_score 一致性校验、PMO 部门默认标准）。修复 P1 后即可投入正式使用。
