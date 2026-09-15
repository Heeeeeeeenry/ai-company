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

- 安装器生成 `secrets.token_hex(24)`（48 位 hex），**写入两处且必须一致**：
  - 宿主 `backend_django/backend_django/settings.py` → `AI_COMPANY_INTERNAL_TOKEN`（校验方）
  - 容器 `~/ai-company/ai-company.env` → `AI_COMPANY_HOST_INTERNAL_TOKEN`（发起方，600）
- **重复安装绝不重新生成**：宿主侧优先复用 settings.py 里已有的值，其次是容器 env 里的。
  否则每装一次就把已经跑起来的容器踢成 401（回归测试 `installer_regression.py` 专门守这条）。
- 端点挂在 `/api/` **之外**：`GET /ai-internal/tools/`、`POST /ai-internal/tools/<name>/`，
  头 `X-AI-Internal-Token`，体 `{"user_id": "admin:<pk>", "args": {...}}`。
  中间件只校验 `/api/` 前缀（见 `api/middleware/session_auth.py`），而这条通道的调用方
  是 ai-company 进程（没有浏览器 cookie），所以不能挂进 `/api/`。
- 宿主侧校验 token 后，**按 pk 从 police_users 取用户 dict**（服务端解析，绝不信 body
  里的其它字段），再走宿主自己的 service 层与权限中心。
- 网络方向：容器里的 `127.0.0.1` 是容器自己，所以走 docker 网关
  （compose 已加 `extra_hosts: host.docker.internal:host-gateway`，容器 env 里
  `AI_COMPANY_HOST_BASE_URL=http://host.docker.internal:15173`）。
  实测：网关 `172.19.0.1:15173` 从容器内 TCP 可连（宿主 uvicorn 绑 `0.0.0.0`）。
- 该前缀不应被公网反代暴露；host 上 `/ai-internal/` 只由 nginx 内部转发或直连本机。

## 5. ai-company 侧编排（真实统计的硬保证）

新增 `src/tools/orchestrator.py`：

1. **预筛**：查询命中 `统计|多少|几件|数量|趋势|分析|对比|报表|导出|查一下` 或该会话此前用过工具 → 进入工具编排；否则走原快路径（避免闲聊多花一次 LLM）。
2. **选工具**：prompt 里塞工具目录（名字+说明+参数 schema），要求返回 `{"tool":..., "args":{...}}` 或 `{"tool":null}`；用现成 `extract_json` 解析（不依赖网关 native function calling —— 现有代码刻意如此）。
3. **执行 → 回灌**：最多 4 步；每步结果原样进 `data_context`。
4. **成文**：prompt 明确「只能使用 <data_context> 中的数字；引用格式保留原始值；须给出统计口径（时间范围/可见范围/状态定义）与样本量」。
5. **数字溯源校验 `verify_numbers(answer, data_context)`**：抽出答案里的数字，逐个在 `data_context` 里找（容忍千分位/单位后缀/百分比换算）；有对不上的 → 带「这些数字没有出处」重写一次；仍不过 → 在答案后附诚实说明（不静默放过）。

## 6. 流式与导出

- 新增 `POST /ai/chat/stream`（容器）→ 宿主 `POST /api/ai/chat/stream/`（SSE 透传）。
  事件契约（`widget.js` 按 `type` 分派，只增字段不改语义）：

  | type | 载荷 | 用途 |
  |------|------|------|
  | `meta` | `conversation_id`, `title` | 会话落盘（首次提问即建会话） |
  | `status` | `text` | 「正在查询数据…」 |
  | `tool` | `name`, `label` | 「正在统计信件总量…」——**取数过程对用户可见** |
  | `files` | `[{url,name}]` | 导出文件卡片 |
  | `delta` | `text` | 逐字增量（只推成文那一跳） |
  | `correction` | `unsupported[]` | 成文里出现无出处数字（罕见，留痕） |
  | `error` / `done` | `detail` / `reply` | 终止 |

- 旧 `/ai/chat` 保持整段 JSON 不变（兼容 + 评测脚本继续可用）。
- 宿主侧转发要点：**先拿到上游状态码再开始流**（非 2xx 直接回 403/500 JSON，
  不吐半截 200）；`X-Accel-Buffering: no` + `Cache-Control: no-cache`，
  否则 nginx 会把整段缓冲掉、逐字效果消失。
- 导出：`export_letters` → 宿主生成 CSV 到 `~/dev_admin/.run/ai_exports/<handle>.csv`，
  返回句柄 `handle = <payload>.<HMAC(secret, payload)>`（签名里绑 `user_pk`）。
  下载走 **`/api/ai/files/<handle>/`**（不是 `/ai-internal/`）——这样能白拿
  管理端的 cookie 登录校验，且句柄只允许本人。

## 7. 前端（widget.js）

- `send()` 改走 `postStream('/chat/stream/')`：`fetch` + `res.body.getReader()` 解 SSE。
  **不用 `EventSource`** —— 它只能 GET、塞不了 JSON body，也读不到 403 响应体，
  而「会话归属失效就原地重开」正好依赖后者。
- 分帧：按 `\n\n` 切帧、`data:` 前缀取值，**半包留在缓冲里等下一块**
  （真实代理会在 JSON 中间切包）。单帧解析失败只丢那一帧，不炸整轮。
- 渲染：先建空气泡承接 `delta`（`text` 累加后 `mdToHtml` 重渲染，带 `is-streaming` 光标）；
  `tool`/`status` 事件把打字气泡文案换成工具文案；`files` 事件渲染 `<a class="aicw-file">` 卡片。
- 403 自愈：首次带旧 `cid` 拿到 403 → 清掉 `cid` 重发一次（`forgetSession()`），
  用户侧看不到报错。
- 错误兜底：401 提示重新登录；已经出了半截文字则追加「（回答中断：…）」而不是清屏。
- **测试 fetch 桩必须先加 `body:{getReader()}`**，否则流式改造会让 widget 断言全挂
  （旧桩只认 `/chat/` 且不返回 body）。桩里还故意把一个 `tool` 事件的 JSON 从中间切开，
  用来守分帧逻辑。

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
