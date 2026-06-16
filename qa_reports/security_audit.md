# 🔐 AI-Company 代码安全质检报告

> **审计日期**: 2026-06-12  
> **审计范围**: `src/` 目录下全部 10 个 Python 源文件（3043 行）  
> **审计方法**: 静态分析 + 自动化检测脚本  
> **总体评分**: 72/100 — 🟡 需改进

---

## 📊 总览

| 维度 | 评分 | 状态 |
|------|------|------|
| 安全性 | 65/100 | 🟡 |
| 代码质量 | 75/100 | 🟡 |
| 依赖安全 | 60/100 | 🟡 |
| 鲁棒性 | 70/100 | 🟡 |
| 类型覆盖率 | 87% | 🟢 |

---

## 1. 🔒 安全性 (65/100)

### ✅ 通过项

| 检查项 | 结果 |
|--------|------|
| API 密钥硬编码 | ✅ **通过** — 所有密钥通过环境变量管理 (`os.getenv()`)，无硬编码 |
| 裸 except | ✅ **通过** — 未发现无异常类型的 `except:` |
| 敏感数据日志 | ✅ **通过** — 错误日志使用 `logger.exception()`，不打印密钥 |

### ⚠️ 风险项

#### 1.1 命令注入风险 — `src/execution/executor.py` (HIGH)

**第 257 行** — `_local_exec()` 使用 `asyncio.create_subprocess_shell()`:

```python
proc = await asyncio.create_subprocess_shell(
    command,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=workdir,
)
```

**第 282 行** — `_docker_exec()` 直接拼接 shell 命令:

```python
docker_cmd = (
    f"docker run --rm -v {workdir}:/workspace "
    f"-w /workspace {self.SANDBOX_IMAGE} "
    f"bash -c {json.dumps(command)}"
)
```

**风险分析**:
- 当前 `execute_tool()` 未在正常业务流程中被调用（TOOL_REGISTRY 为死代码），但 `ExternalExecutor` 将用户任务传入 shell。
- 虽然 `json.dumps()` 提供部分转义保护，但 `create_subprocess_shell` 本质上是危险的。
- `workdir` 参数未做路径校验，可能被传入包含空白字符或 shell 元字符的路径。

**建议**: 
- 统一改用 `asyncio.create_subprocess_exec(cmd, *args)` 分离命令与参数
- 对 `workdir` 做路径白名单校验
- 对 `ExternalExecutor` 的 task 参数做输入清洗

#### 1.2 错误信息泄露至用户 — `src/telegram_bot.py` (MEDIUM)

**第 225-228 行**:

```python
except Exception as e:
    logger.exception("CEO workflow failed")
    await status_msg.edit_text(
        f"❌ **出错了**\n\n```\n{str(e)[:500]}\n```\n\n请重试或检查日志。",
```

**风险**: `str(e)[:500]` 直接将异常详情发送给 Telegram 用户。若异常消息包含内部路径、API 端点或配置信息，将造成信息泄露。

**建议**: 改为通用错误消息，内部详情仅记录日志。

#### 1.3 MCP 客户端缺少输入校验 — `src/execution/executor.py` (LOW)

- `_ensure_server()` 从环境变量 `MCP_SERVERS` JSON 中读取 `cmd` 和 `args`，未做校验即传入 `create_subprocess_exec`。
- 无认证机制：外部 MCP 服务器连接无 token/密钥认证。
- 风险等级较低（管理员控制环境变量），但应作为纵深防御的一环。

---

## 2. 📝 代码质量 (75/100)

### ✅ 通过项

- 函数、方法类型注解覆盖率 **87%**（86/99 个函数有返回类型或参数注解）
- 命名规范统一，遵循 PEP 8
- 模块职责划分清晰（CEO/Verification/Execution/Memory/Departments）

### ⚠️ 改进项

#### 2.1 高复杂度函数（>50 行） — 8 个

| 文件 | 行号 | 函数名 | 行数 |
|------|------|--------|------|
| `src/ceo/graph.py` | 144 | `triage_node` | 83 |
| `src/ceo/graph.py` | 513 | `audit_role_node` | 61 |
| `src/ceo/graph.py` | 614 | `execute_department_node` | 55 |
| `src/ceo/graph.py` | 747 | `verify_aggregate_node` | 80 |
| `src/ceo/graph.py` | 910 | `build_ceo_graph` | 65 |
| `src/execution/executor.py` | 55 | `_ensure_server` | 54 |
| `src/main.py` | 26 | `run_cli` | 53 |
| `src/telegram_bot.py` | 138 | `handle_message` | 86 |

**建议**: `triage_node`(83行) 和 `handle_message`(86行) 建议拆分为多个子函数。`triage_node` 中的快速路径关键词匹配可独立为 `_fast_path_triage()`。

#### 2.2 死代码

**a) `operation` 评分维度 — `src/verification/auditor.py`**

`SCORING_DIMENSIONS` 中定义了 `"operation"` 维度的评分标准（5 个维度），但 `triage_node` 将所有运维/部署类任务路由到 `"devops"` 部门，使用 `"devops"` 维度评分。`"operation"` 维度从未被引用。

```python
# 定义了但永远不会被使用的评分维度
"operation": [
    {"name": "响应速度", ...},
    {"name": "资源效率", ...},
    {"name": "稳定性", ...},
    {"name": "可观测性", ...},
    {"name": "自动化程度", ...},
],
```

**b) `TOOL_REGISTRY` — `src/execution/executor.py`**

定义了 11 个工具（`web_search`, `web_fetch`, `run_python`, `run_test`, `lint_code`, `format_code`, `git_commit`, `read_file`, `write_file`, `list_dir`），但 `execute_tool()` 从未在 `graph.py` 或 `agents.py` 中被调用。当前 `ExecutionRouter.route()` 直接使用 LLM 执行，完全绕过工具注册表。

**建议**: 删除死代码，或将 `operation` 合并到 `devops` 维度中。

#### 2.3 类型覆盖率缺口

| 文件 | 已注解/总函数 | 覆盖率 |
|------|---------------|--------|
| `src/main.py` | 0/3 | **0%** ⚠️ |
| `src/execution/executor.py` | 13/17 | 76% |
| `src/verification/auditor.py` | 5/6 | 83% |
| `src/departments/roles.py` | 14/16 | 88% |
| `src/memory/store.py` | 17/19 | 89% |
| `src/telegram_bot.py` | 11/12 | 92% |

`main.py` 中的 `run_cli()`, `main()`, `one_shot()` 完全缺少类型注解。

---

## 3. 📦 依赖安全 (60/100)

### ✅ 核心依赖

| 依赖 | 状态 |
|------|------|
| `langgraph` | ✅ 使用中 |
| `langchain` | ✅ 使用中 |
| `langchain-openai` | ✅ 使用中 |
| `python-telegram-bot` | ✅ 使用中 |
| `python-dotenv` | ✅ 使用中 |
| `rich` | ✅ 使用中 |

### ❌ 未使用的依赖（应移除）

| 依赖 | 状态 | 说明 |
|------|------|------|
| `pydantic>=2.0` | ❌ 未使用 | 代码中无 `pydantic` 导入或使用（使用 `dataclasses` 替代） |
| `structlog>=24.0` | ❌ 未使用 | 代码中使用标准 `logging` 而非 `structlog` |
| `tenacity>=8.0` | ❌ 未使用 | 重试库已安装但从未导入使用 |

**影响**: 
- 增大依赖攻击面
- 增加安装时间和镜像体积
- `tenacity` 的缺失意味着 LLM 调用无自动重试机制（见鲁棒性部分）

---

## 4. 🛡️ 鲁棒性 (70/100)

### ⚠️ 关键问题

#### 4.1 LLM API 调用无超时 — HIGH

所有 5 处 `ainvoke()` 调用均未设置超时：

| 文件 | 函数 | 风险 |
|------|------|------|
| `src/ceo/graph.py` `triage_node` | 意图分析 LLM | 可能永久挂起 |
| `src/ceo/graph.py` `pm_analyze_node` | PM 分析 | 同上 |
| `src/ceo/graph.py` `plan_node` | 计划生成 | 同上 |
| `src/verification/auditor.py` `audit()` | 审计评分 | 同上 |
| `src/verification/auditor.py` `pmo_gate_check()` | PMO 检查 | 同上 |

**建议**: 所有 LLM 调用添加超时参数：`await llm.ainvoke(..., timeout=60)`

#### 4.2 无 LLM 调用重试机制

尽管 `requirements.txt` 包含 `tenacity>=8.0`，代码中未使用。当 DeepSeek/OpenAI API 返回 429/5xx 时，无重试逻辑。

LangGraph workflow 有最高 3 次的质量门重试（业务重试），但缺少 API 层面的瞬时故障重试。

**建议**: 使用 `tenacity` 装饰器包裹 `ainvoke` 调用，实现 exponential backoff。

#### 4.3 资源泄漏风险

| 资源 | 风险 | 详情 |
|------|------|------|
| MCP 子进程 | 🟡 泄漏 | `MCPClient.shutdown()` 已定义但**从未被调用**。`start_bot()` 的 `finally` 块只清理 Telegram 连接，未清理 MCP 进程。 |
| Graphiti 客户端 | 🟡 泄漏 | `EpisodeMemory` 初始化 Graphiti 客户端后从未关闭。 |
| `asyncio.subprocess` | 🟢 安全 | `_local_exec` 使用 `proc.communicate()` 确保资源回收 |

#### 4.4 异常恢复路径覆盖

| 组件 | 状态 | 详情 |
|------|------|------|
| `_extract_json()` 调用 | 🟡 | 4 处调用中 3 处有 try/except 包裹，`pm_analyze_node` 中第 409 行已正确包裹。✅ |
| `auditor_node` LLM 失败降级 | ✅ | 解析失败返回默认 60 分通过 |
| `pmo_gate_check` 失败降级 | ✅ | 解析失败返回 PASS |
| `dispatch_to_department` 未知部门 | ✅ | 返回明确错误，不崩溃 |
| `telegram_bot` 消息处理 | ✅ | 外部 `try/except` 包裹 |
| Docker 沙箱不可用 | ✅ | 降级到 `_local_exec` |

---

## 5. 🧪 验证测试

### 测试结果

```
=== 裸 except 检查 ===
  ✅ PASS: 0 个裸 except

=== 硬编码密钥检查 ===
  ✅ PASS: 0 个硬编码密钥

=== 命令注入风险点 ===
  ⚠️  4 个风险点（见上文）

=== 高复杂度函数 ===
  ⚠️  8 个 >50 行函数

=== 死代码检查 ===
  ⚠️  "operation" 评分维度 (auditor.py)
  ⚠️  TOOL_REGISTRY 11 个工具 (executor.py)

=== 依赖使用检查 ===
  ⚠️  3 个未使用依赖: pydantic, structlog, tenacity

=== 类型注解覆盖率 ===
  ✅ 87% (86/99 函数)

=== API 超时检查 ===
  ⚠️  5/5 ainvoke() 调用无超时

=== 资源清理检查 ===
  ⚠️  MCPClient.shutdown() 未调用
  ⚠️  Graphiti 客户端未关闭
```

---

## 6. 📋 改进优先级建议

| 优先级 | 问题 | 影响 | 修复成本 |
|--------|------|------|----------|
| 🔴 P0 | 命令注入风险 (`create_subprocess_shell`) | 高 | 中 |
| 🔴 P0 | LLM 调用无超时 | 高 | 低 |
| 🟡 P1 | 错误信息泄露 | 中 | 低 |
| 🟡 P1 | 未使用依赖清理 | 低 | 低 |
| 🟡 P1 | 添加 API 重试逻辑 | 中 | 中 |
| 🟢 P2 | MCP 进程资源泄漏 | 中 | 低 |
| 🟢 P2 | 死代码清理 | 低 | 低 |
| 🟢 P2 | 拆分高复杂度函数 | 低 | 中 |
| 🟢 P3 | main.py 添加类型注解 | 低 | 低 |
| 🟢 P3 | Graphiti 客户端清理 | 低 | 低 |

---

## 7. 总体评价

代码整体具有较好的安全基础：API 密钥管理规范，无明显的硬编码凭据，异常处理路径基本完整。核心架构采用 LangGraph 编排，职责分离清晰。

主要风险集中在 **执行层**（`executor.py`）：使用 `create_subprocess_shell` 的命令注入模式，以及 LLM 调用缺少超时和重试的鲁棒性缺失。此外存在 **死代码** 和 **未使用依赖**，增加了维护负担。

**总体评分 72/100** — 可以发布，但建议在上生产前修复 P0/P1 项。

---

*报告由 AI-Company 安全质检自动生成*
