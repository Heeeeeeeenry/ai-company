# 衡水定制：AI 受控取数分析（hsmyzgzx 分支）

> 分支：`hsmyzgzx`（衡水民意智感中心定制）。主分支 `main` 保持通用嵌入件线。
> 本文是该定制的设计定稿，代码实现一律以本文为准；实现过程中发现口径变化必须回改本文。

## 1. 需求原文与拆解

用户原话：

> 能通过 AI 来调用接口获取用户可以查看的数据，然后对用户可观测到的数据进行分析。
> 不能绕过程序直接获取数据库结果。同时需要保障统计的真实性、合理性。
> 然后能够在对话框中导出文件等等的功能，并且输出答案的时候也要用文字挨个输出的样式。

拆成五条硬约束：

| # | 约束 | 落地手段 |
|---|------|----------|
| 1 | 取数走**程序接口** | AI 只持有 HTTP 工具目录，没有任何数据库连接 |
| 2 | 范围 = 用户**可观测**数据 | 每个工具强制复用宿主 `PermissionContext` 可见性口径 |
| 3 | **不得绕过程序直连库** | 目标机 MySQL 容器不对 AI 暴露；工具层 SQL 也必须拼官方可见性 WHERE |
| 4 | 统计**真实、合理** | 数字全部来自工具返回值；成文后做「数字溯源校验」，对不上就重写 |
| 5 | **导出文件** + **逐字输出** | 复用宿主既有 `export_letters_csv`；前端改 SSE 流式逐字渲染 |

## 2. 三层架构

```
┌─ 浏览器悬浮窗 (widget.js) ────────────────────────────────────┐
│  SSE 流式逐字渲染 / 「正在查询数据…」中间态 / 导出文件下载卡片      │
└──────────────┬───────────────────────────────────────────────┘
               │ cookie(宿主登录态)  POST /api/ai/chat/stream/
┌──────────────▼─ 宿主 dev_admin (Django) ──────────────────────┐
│  ① 身份：request.session_user  →  admin:<pk>                  │
│  ② 代理到 ai-company（流式转发）                                │
│  ③ 【受控工具目录】/api/ai/internal/tools/  ← 本定制核心         │
│     host_tools.py：白名单函数，逐个复用宿主 service 层口径        │
│     - letter/service.get_letter_list / get_letter / ...        │
│     - config/service.*_payload（字典）                         │
│     - PermissionContext.letter_visibility_sql()（唯一可见性）    │
│  ④ /api/ai/internal/files/<handle>/  导出文件下载（cookie 鉴权） │
└──────────────┬───────────────────────────────────────────────┘
               │ 内部回调：X-AI-Internal-Token + user_id
┌──────────────▼─ ai-company (FastAPI) ────────────────────────┐
│  tools/orchestrator.py                                        │
│   选工具(LLM+extract_json) → 调宿主工具 → 回灌 → 成文          │
│   verify_numbers()：答案里每个数字必须能在工具结果里找到          │
│  api_server /ai/chat/stream  SSE: status|tool|delta|files|done │
└───────────────────────────────────────────────────────────────┘
```

**边界铁律**

- ai-company 进程**没有**任何数据库连接串；它只能发 HTTP 调宿主工具目录。
- 宿主工具目录的每个函数，第一件事就是拿 `user` 走 `PermissionContext`；工具层内不允许出现「不带可见性 WHERE 的 letters 查询」。
- AI 不允许接受前端传来的身份；`user_id` 只由服务端推导（沿用 `conf.resolve_user_id`）。

## 3. 宿主侧受控工具目录（v1）

新增 `integration/ai_company/host_tools.py`：一张白名单表 + 一个执行器。

| 工具名 | 入参 | 底层实现（复用） | 权限前置 |
|--------|------|------------------|----------|
| `whoami_scope` | — | `permission_center.get_context(user)` | 仅需登录；返回角色/可见范围说明 |
| `dict_lookup` | `kind` ∈ status/channel/category/attribute/rating | `config.service.get_*_payload()` | 仅需登录 |
| `letter_overview` | `scope`(visible/current_unit) | `letter.service.get_letter_status_counts` | 可见性口径内 |
| `letter_search` | `keyword/status/created_from/created_to/unit_code/page/limit/sort/fields` | `letter.service.get_letter_list` | 同上 |
| `letter_detail` | `letter_no` | `letter.service.get_letter` | 同上（自动套可见性） |
| `letter_stats_by_unit` | 同 overview | `get_letter_status_counts(group_by_unit=True)` | 同上 |
| `letter_trend` | `created_from/created_to/granularity=month\|day` | 工具层内聚合，**SQL 强制拼 `build_visibility_where(user)`** | 同上 |
| `export_letters` | 同 `letter_search` | `letter.service.export_letters_csv(user,args)` | 同上；产出下载句柄 |

返回统一包一层信封，便于 AI 自述口径、也便于前端展示：

```json
{ "tool": "letter_overview",
  "scope": {"visible_rule": "本单位(current_unit_code=...)", "user_role": "普通民警",
            "time_range": "2026-01-01~2026-09-15", "note": "统计范围=上述可见信件"},
  "data": { ... 原始返回 ... },
  "summary_hint": "共 N 件；其中处理中 x 件" }
```

> `letter_trend` 是唯一在工具层新写 SQL 的工具：它把 `build_visibility_where(user)` 拼进去（`letter/service.py:346`），不做任何自有过滤 —— 口径与列表页一致。

## 4. 内部回调鉴权

宿主已有 `AI_COMPANY_TOKEN`（宿主 → ai-company 方向）。本定制新增反向通道：

- 安装器生成随机 32 字节 `AI_COMPANY_INTERNAL_TOKEN`，写入宿主 `~/dev_admin/.env`（已 600）并注入容器 env（600）。
- `POST /api/ai/internal/tools/<name>/`，头 `X-AI-Internal-Token`，体 `{"user_id": "admin:<pk>", "args": {...}}`。
- 宿主侧校验 token 后，**按 pk 从 police_users 取用户 dict**（服务端解析，绝不信 body 里的其它字段），再执行工具。
- 仅监听 127.0.0.1，不出公网；token 不回显、不进日志。

## 5. ai-company 侧编排（真实统计的硬保证）

新增 `src/tools/orchestrator.py`：

1. **预筛**：查询命中 `统计|多少|几件|数量|趋势|分析|对比|报表|导出|查一下` 或该会话此前用过工具 → 进入工具编排；否则走原快路径（避免闲聊多花一次 LLM）。
2. **选工具**：prompt 里塞工具目录（名字+说明+参数 schema），要求返回 `{"tool":..., "args":{...}}` 或 `{"tool":null}`；用现成 `extract_json` 解析（不依赖网关 native function calling —— 现有代码刻意如此）。
3. **执行 → 回灌**：最多 4 步；每步结果原样进 `data_context`。
4. **成文**：prompt 明确「只能使用 <data_context> 中的数字；引用格式保留原始值；须给出统计口径（时间范围/可见范围/状态定义）与样本量」。
5. **数字溯源校验 `verify_numbers(answer, data_context)`**：抽出答案里的数字，逐个在 `data_context` 里找（容忍千分位/单位后缀/百分比换算）；有对不上的 → 带「这些数字没有出处」重写一次；仍不过 → 在答案后附诚实说明（不静默放过）。

## 6. 流式与导出

- 新增 `POST /ai/chat/stream`（SSE），事件：`status`(正在查询数据…) / `tool`(工具名) / `delta`(文本增量) / `files`(下载句柄) / `meta`(conversation_id) / `done`。旧 `/ai/chat` 保持整段 JSON 不变（兼容 + 评测脚本继续可用）。
- 只推**最终成文**那一跳的 token；选工具阶段不外泄。
- 导出：`export_letters` → 宿主生成 CSV 到 `~/dev_admin/.run/ai_exports/<handle>.csv`，返回句柄 `handle = HMAC(secret, user_pk|args|ts)`；前端拿 `/api/ai/internal/files/<handle>/` 下载（cookie 鉴权 + 句柄只允许本人）。

## 7. 前端（widget.js）

- `streamChat()`：`fetch` + `res.body.getReader()` 解 SSE；先建空气泡，`delta` 到达即 `bubble.textContent += chunk`，结束再 `mdToHtml` 重渲染。
- 中间态：复用 `appendTyping()`，加 `label`（收到 `tool` 事件换成「正在统计信件…」）。
- 文件卡片：`appendMessage` 支持 `files:[{url,name}]` → `<a class="aicw-file">`（新增 CSS，蓝系 #eff6ff/#2563eb）。
- **测试 fetch 桩必须先加 `body:{getReader()}`**，否则流式改造会让 14 条 widget 断言全挂（当前桩不返回 body）。

## 8. 分阶段与验收

| 阶段 | 内容 | 验收 |
|------|------|------|
| P1 | 宿主工具目录 + 内部端点 + 安装器带 token | 宿主上 ad-hoc 脚本：以真实登录用户直调 8 个工具，断言权限范围（A 用户拿不到 B 单位信件） |
| P2 | ai-company 编排层 + 数字校验 | 本地假宿主端到端：问「本月各状态信件数量」→ 答案数字与假宿主返回逐一对齐；编造场景（假宿主返回空）不得出现虚构数字 |
| P3 | SSE + 导出 | curl -N 看到 delta 分片；导出文件能下载且内容=可见信件 |
| P4 | widget 流式/中间态/文件卡片 | `tests/run.sh` 全绿（含新增断言） |
| P5 | 部署 + 真登录端到端 | 目标机 15173 上真实账号走通，公网资源可达，里程碑 commit |

## 9. 明确不做

- 不给 ai-company 任何数据库连接或 SQL 通道。
- 不改宿主业务模块（`api/modules/**`）：工具层只用其现有公开函数，新增 SQL 只存在于 `ai_company` 包内且强制拼官方可见性。
- 不改主分支行为：全部改动落在 `hsmyzgzx`。
- 不 push（按既定规则，19:00 左右例外）。
