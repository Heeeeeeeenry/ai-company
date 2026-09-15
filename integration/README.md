# ai-company 可插拔嵌入件（民意智感中心管理端）

把 ai-company(狗蛋儿) 作为「AI 助手」嵌进管理端：**每个登录用户有自己独立的多会话
AI 对话**，用户之间完全隔离，凭据过期也不丢历史。

---

## 1. 架构：三层解耦（重要）

```
管理端登录态  ──①──▶  user_id  ──②──▶  若干 conversation_id  ──③──▶  永久历史
   (Django)              (ai-company)          (ai-company)            (磁盘)
```

| 层 | 谁负责 | 说明 |
|---|---|---|
| ① 认证 | **管理端**（Django session / JWT） | ai-company 不管认证，也不知道密码/cookie |
| ② 归属 | ai-company | user_id → 会话列表；越权访问一律 403/404 |
| ③ 历史 | ai-company | 历史挂在 conversation_id 上，**不挂凭据** → 退出登录、换 token 都不丢 |

这么切的好处：管理端的认证方式怎么改都不影响 ai-company；ai-company 换模型、换服务器
也不影响管理端。

---

## 2. 后端准备（ai-company 侧）

### 2.1 配置自己的 DeepSeek 模型

ai-company 用**自己的一套**凭证，不会复用宿主机或别进程的环境变量。编辑项目根
`.env`（可从 `.env.example` 复制）：

**方式 A —— 走内网 OneAPI（默认）**
```ini
AI_COMPANY_MODEL_PROVIDER=oneapi
AI_COMPANY_MODEL_BASE_URL=https://oneapi-comate.baidu-int.com/v1
AI_COMPANY_MODEL=DeepSeek-V4-Flash
AI_COMPANY_ONEAPI_API_KEY=sk-xxxx
```

**方式 B —— 走 DeepSeek 官方端点**
```ini
AI_COMPANY_MODEL_PROVIDER=deepseek
AI_COMPANY_DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
AI_COMPANY_DEEPSEEK_MODEL=deepseek-chat
AI_COMPANY_DEEPSEEK_API_KEY=sk-xxxx
```

凭证解析链（按端点分开，绝不串用）：
`AI_COMPANY_ONEAPI_API_KEY` → `ONEAPI_API_KEY`，`AI_COMPANY_DEEPSEEK_API_KEY` → `DEEPSEEK_API_KEY`。

角色级覆盖照样有效（`CEO_MODEL` / `DEVELOPER_MODEL` …），不填就用 `AI_COMPANY_MODEL`。

### 2.2 起服务

```bash
./goudan-api                      # 默认 127.0.0.1:8020
AI_COMPANY_API_PORT=8020 ./goudan-api
```

启动器会强制 `AI_COMPANY_SERVICE_MODE=1`——**对外服务必须开着**，它保证服务进程
不去激活本机 CLI 的会话、会话记忆落在独立目录。

### 2.3 自检

```bash
curl -s http://127.0.0.1:8020/ai/health
# {"status":"ok","model":"DeepSeek-V4-Flash","provider":"oneapi",
#  "base_url":"https://oneapi-comate.baidu-int.com/v1","api_key_set":true,...}
```

`model` / `provider` / `base_url` / `api_key_source` 是**实际生效值**，配没配对一眼看出。

生产建议用 systemd/supervisor 托管，并让 nginx 只反代到 `127.0.0.1:8020`（不要开公网）。

---

## 3. 接入 Django：三步

### 步骤 1 —— 放包

把 `integration/ai_company/` 整个目录（包名就是 `ai_company`）拷到管理端项目根
（与 `manage.py` 同级），或放进任意已在 `PYTHONPATH` 的目录。

```
<你的项目>/
├── manage.py
├── ai_company/          ← 拷贝进来
│   ├── apps.py  conf.py  client.py  views.py  urls.py
│   ├── templates/ai_company/{chat.html,_widget.html}
│   └── static/ai_company/{ai_company.css,ai_company.js}
```

### 步骤 2 —— settings.py

```python
INSTALLED_APPS = [
    ...,
    "ai_company",
]

# ai-company 后端地址（同机就是这一行）
AI_COMPANY_BASE_URL = "http://127.0.0.1:8020"
# 单轮最长等待秒数（模型思考链较长，别设太小）
AI_COMPANY_TIMEOUT = 120
# 前面挂了鉴权网关才需要
AI_COMPANY_TOKEN = ""
# user_id 命名空间前缀，见第 4 节
AI_COMPANY_USER_PREFIX = "admin"
```

### 步骤 3 —— urls.py

```python
from django.urls import include, path

urlpatterns = [
    ...,
    path("ai/", include("ai_company.urls")),
]
```

**完成。** 访问 `/ai/page/` 直接得到完整对话页；也可以只把组件嵌进你已有的页面：

```django
{% load static %}
<link rel="stylesheet" href="{% static 'ai_company/ai_company.css' %}">

<div style="height: calc(100vh - 140px)">
  {% include "ai_company/_widget.html" %}
</div>
```

组件高度跟随容器，父容器需要有确定高度。

### 路由一览

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/ai/page/` | 自带对话页 |
| GET | `/ai/api/health/` | 后端连通性 + 生效模型 |
| GET/POST | `/ai/api/sessions/` | 列表 / 新建会话 |
| GET/DELETE | `/ai/api/sessions/<cid>/` | 历史 / 删除会话 |
| POST | `/ai/api/chat/` | 发消息（`message`，可选 `conversation_id`） |

---

## 4. 身份透传规则（安全红线）

- **前端永远不传 user_id。** `ai_company.js` 里没有任何 user_id 字段，视图也不读它。
- user_id 由 `conf.resolve_user_id(request)` 从 `request.user` 推导，默认
  `f"{AI_COMPANY_USER_PREFIX}:{user.pk}"` → 例如 `admin:37`。
- **为什么要加前缀**：同一个 ai-company 实例以后可能同时服务市民端，两边的 pk 会撞号，
  前缀把命名空间分开。
- 想用员工号/邮箱代替 pk，给 settings 一个 callable：

```python
def _uid(request):
    return f"admin:{request.user.username}"

AI_COMPANY_USER_ID_RESOLVER = _uid
```

- 未登录访问任何 API → 403；访问别人的会话 → 403/404。这两条有测试覆盖。

---

## 5. 隔离是怎么保证的（已实测）

| 层 | 机制 | 测试 |
|---|---|---|
| 会话之间 | 请求进入后端即绑定"会话作用域"，工作区产物、记忆都按会话分目录 | `scripts/test_isolation_scope.py` |
| 用户之间 | 每个会话带 `user_id` 归属，读/写/删都校验归属 | `integration/test_django_client.py` |
| 服务与 CLI | 服务模式不复用本机 CLI 会话；记忆根目录独立 `service_sessions/` | `scripts/test_isolation_scope.py` |

命令：

```bash
./goudan-api &                                   # 先起服务
python3 scripts/test_isolation_scope.py          # 隔离不变量（12 项）
python3 integration/test_django_client.py        # 接入契约 + 跨用户越权（14 项）
python3 integration/test_django_views.py         # 视图/模板/静态/越权（26 项，需 Django）
```

三者都是**真打后端**的端到端测试，不是 mock。最近一次结果：12/12、14/14、26/26 全绿。
`test_django_views.py` 用 Django 自带 test client，会临时把身份推导换成"从请求头取用户"
的 resolver（这样不必建 auth 表），因此它同时验证了"身份只能服务端推导"这条链路。

---

## 6. 常见问题

**Q：history 返回 404 / 403？**
404 = 会话不存在或不属于你；403 = 存在但属于别人。都别把它当 500 处理。

**Q：前端报 CSRF 失败？**
组件走的是 Django 会话认证，POST 需要 CSRF token。`ai_company.js` 从 `csrftoken`
cookie 读取并放进 `X-CSRFToken`。确保页面渲染过 `{% csrf_token %}` 或视图带了
`@ensure_csrf_cookie`（自带页面已带）。

**Q：回复很慢 / 超时？**
模型带思考链，单轮可能十几秒。把 `AI_COMPANY_TIMEOUT` 调大（后端是最长 120s 量级）。
前端已做"思考中"动画，不会假死。

**Q：多人用时会不会串话？**
不会，见第 5 节。上一版曾出现过两个真实缺陷（全局产物文件共读写、CLI 会话被误激活），
均已修复并加了回归测试。

**Q：历史存在哪？**
`~/.ai-company/multiuser.db`（会话归属）+ `~/.ai-company/service_sessions/<cid>/memory.json`
（对话内容）。备份这两个即可。

**Q：能换模型 / 加流式吗？**
换模型改 `.env` 重启服务即可，管理端不用动。流式（SSE）目前后端未提供，需要时再加
一个 `/ai/chat/stream` 端点 + 前端 EventSource。

---

## 7. 还没做 / 需要你确认

- **接入目标未确认**：本机与现有可达的 SSH 上都找不到管理端仓库（`81.70.230.137:40022`
  连接被拒）。所以本包按**通用 Django 结构**交付，未改动任何管理端文件。
  给我管理端路径/地址，我可以就地接好并跑通"登录 → 建会话 → 发消息 → 越权拒绝"。
