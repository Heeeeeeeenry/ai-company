# ai-company 嵌入式部署运行手册（民意智感中心管理端）

把 **ai-company（狗蛋儿）** 作为 AI 能力层，嵌进「衡水市民意智感中心」管理端
（dev_admin，Django 6.1 + 原生 ES-module SPA），形态是**全站常驻的悬浮 AI 对话窗**。

```
浏览器 ──cookie(session_key)──► dev_admin (uvicorn :15173)
                                   │  已登录 → request.session_user
                                   │  /api/ai/*  (SessionAuthMiddleware 兜住登录态)
                                   ▼
                             ai_company 接入件 (Django app, 纯 stdlib urllib)
                                   │  user_id = admin:<police_users.id>
                                   ▼
                             ai-company 容器 (docker, 127.0.0.1:8020)
                                   │
                                   ▼
                             api.deepseek.com (deepseek-v4-flash)
```

---

## 0. 结论速查

| 项 | 值 |
|---|---|
| 目标机 | `my@222.223.144.110:60022`（x86_64，公钥免密，在 docker 组） |
| 管理端 | `~/dev_admin`，uvicorn 端口取自 `~/dev_admin/.env` 的 `WEB_PORT` |
| 入口地址 | `http://dev-admin.hsmyzg.com`（DNS → .110） |
| 服务目录 | `~/ai-company/`（compose + env + data） |
| 容器端口 | `127.0.0.1:8020`（**只绑本机，不对外**） |
| 镜像 | `crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com/letter/ai-company:latest` |
| 模型 | `deepseek-v4-flash`（官方端点；该机**到不了**百度内网 OneAPI） |
| 嵌入件端点 | `/api/ai/...`（必须带 `api/` 前缀才吃得上登录校验） |

> ⚠️ 该机 **Docker Hub 不可达**，镜像必须走阿里云 ACR。

---

## 1. 构建镜像（在开发机，非目标机）

目标机是 x86_64，开发机是 arm64 → 必须交叉构建 `linux/amd64`。

```bash
cd <repo>
docker buildx build --platform linux/amd64 --load \
  -f deploy/Dockerfile \
  -t crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com/letter/ai-company:latest \
  .
```

Dockerfile 关键点：
* `deploy/requirements.docker.txt` —— **去掉了 `gnureadline`**（仅 macOS），**补了
  `fastapi`/`uvicorn`**（原 requirements.txt 漏了），并加了 `numpy`
  （vendored engram-router 的**模块级**硬依赖）。
* `deploy/vendor/engram-router/` —— engram-router 是**私有仓库**且原依赖写成
  `file:///Users/...` 绝对路径，镜像里用不了 → 直接把 `src/engram_router`
  vendor 进构建上下文，镜像内 `pip install ./vendor/engram-router`。
  只 vendor 源码（712K / 46 文件），其余 faiss/torch/sentence_transformers/hanlp
  等全是函数内惰性 import，**不装**（装了镜像要涨好几 G）。
* 密钥**不进镜像**：`.dockerignore` 排除 `.env`；密钥运行时用 env 注入。

本地冒烟（可选，arm64 跑 amd64 靠 qemu，慢但能验）：

```bash
docker run -d --name aic-test --platform linux/amd64 -p 127.0.0.1:18020:8020 \
  -e AI_COMPANY_SERVICE_MODE=1 -e AI_COMPANY_MODEL_PROVIDER=deepseek \
  -e AI_COMPANY_DEEPSEEK_API_KEY=dummy \
  crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com/letter/ai-company:latest
curl -s http://127.0.0.1:18020/ai/health   # 应回 status=ok + provider=deepseek
```

## 2. 把镜像送到目标机

目标机 `.110` **没有 ACR 登录态**（`~/.docker/config.json` 不存在），也**打不通
Docker Hub**，所以它有两条路，推荐第一条：

### 2a. 免凭据流式直传（推荐，已实测）

```bash
docker save crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com/letter/ai-company:latest \
  | gzip -1 -c \
  | ssh -o BatchMode=yes -p 60022 my@222.223.144.110 'gunzip -c | docker load'
```

`compose` 里已设 `pull_policy: never`，所以起容器时不会去联网拉。

### 2b. 走 ACR（可选，需要先在目标机登录）

```bash
docker push crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com/letter/ai-company:latest   # 开发机
ssh -p 60022 my@222.223.144.110 \
  'docker login crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com'   # 目标机，用户名 StupidHenry
```

要走 2b 的话，记得把 compose 里的 `pull_policy: never` 改成 `missing`。

## 3. 在目标机起服务

```bash
bash deploy/deploy-service.sh
```

等价的手工步骤：

```bash
# 3.1 目录与环境变量
mkdir -p ~/ai-company/data
cd ~/ai-company
# 把 deploy/docker-compose.yml 和 deploy/ai-company.env.example 传上来
cp ai-company.env.example ai-company.env
# 密钥直接复用 dev_admin 那把（脚本会做，且不回显明文）：
python3 init_service_env.py --dir ~/ai-company

# 3.2 起容器
docker compose up -d

# 3.3 自检
curl -s http://127.0.0.1:8020/ai/health
docker logs --tail 30 ai-company
```

`init_service_env.py` 会从 `~/dev_admin/.env` 里读 `LLM_API_KEY` 写进
`~/ai-company/ai-company.env`（权限 600），**全程不回显明文**，且幂等——已填过就不覆盖。

`ai-company.env` 里**必须**设的两类东西：

1. `AI_COMPANY_SERVICE_MODE=1` —— 容器里没有本地 CLI，这是服务模式的开关。
   （本机开发时这个变量**绝不能**出现在 `.env` 活行里，否则会把 `goudan` 也拖进服务模式。）
2. 角色级 `*_MODEL=deepseek-v4-flash` —— 不设会回落到 `config.py` 里 `gpt-5.5`
   的默认值，本环境打不通。

## 4. 接入 dev_admin

一键：

```bash
bash deploy/dev_admin/deploy.sh
```

它做四件事：rsync 暂存 → 目标机跑 `patch_dev_admin.py` → 重启管理端后端 → 冒烟。

手动等价（目标机上）：

```bash
python3 patch_dev_admin.py --stage /tmp/ai-company-stage
# 然后重启：kill $(cat ~/dev_admin/.run/backend.pid); ~/dev_admin/start.sh
```

安装器**幂等**，只碰这些地方（改动前自动备份成 `*.bak.ai-company`）：

| 文件 | 改动 |
|---|---|
| `backend_django/backend_django/settings.py` | `INSTALLED_APPS += "ai_company"`；追加 `AI_COMPANY_*` 配置块 |
| `backend_django/backend_django/urls.py` | 在 `path("api/", ...)` **之前**插 `path("api/ai/", include("ai_company.urls_host"))` |
| `backend_django/templates/views/WorkplaceLayout/index.html` | `</body>` 前引入 `widget.css` / `widget.js` |
| `backend_django/ai_company/` | 新增，接入件包 |
| `backend_django/static/src/ai_company/` | 新增，悬浮窗前端的 js/css |

### 为什么是这些位置（踩过的坑）

* **端点必须挂 `/api/` 下**：`SessionAuthMiddleware._requires_auth` 只对 `/api/` 前缀
  生效。把嵌入件挂在 `/api/ai/` 才能白拿「登录校验 + `is_active` + 注入
  `request.session_user`」。挂别处就等于裸奔。
* **`include` 必须插在 `path("api/", include("api.urls"))` 之前**，否则被它先吃掉。
* **必须免 CSRF**：管理端前端 `static/src/api/http.js` 只带 cookie、**从不发 CSRF
  token**（`/api/llm/` 本身就是 `csrf_exempt`）。接入件用 `maybe_csrf_exempt`
  包装 `sessions`/`session_detail`/`chat` 三个视图。
* **身份只信服务端**：`conf._host_user(request)` 优先读 `SessionAuthMiddleware` 注入的
  `request.session_user`（含 `id` = `police_users` 主键），拼成 `admin:<id>`。
  dev_admin **没有** `request.user`。前端传什么都不信。
* **嵌入件 JSON 不带 `order`** → `permission_center.enforce_request` 直接 return，
  不会被单据级鉴权误拦。
* **悬浮窗挂 `WorkplaceLayout/index.html`**：它是全站每页的 SPA 外壳
  （`boot.js` 渲染进 `#app`，未知路径也回落到它），挂这一层 = 全站常驻。
* **前端走 `/src/*`**：`frontend_view` 对 `/src/` 前缀做 import 重写（把 `@/` → `/src/`、
  内联 `.css` import）。悬浮窗因此用 `/src/ai_company/widget.js` 引入。
  import map 里已有 `"marked"` → 动态 `import('marked')` 直接可用。

## 5. 验证清单（真实 HTTP）

```bash
# 5.1 容器活着
curl -s http://127.0.0.1:8020/ai/health

# 5.2 中间件在管（浏览器里不带 cookie 打这个应 401/403）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<WEB_PORT>/api/ai/health/

# 5.3 悬浮窗静态资源
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<WEB_PORT>/src/ai_company/widget.js
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<WEB_PORT>/src/ai_company/widget.css

# 5.4 公网可达（从开发机）
curl -s -o /dev/null -w '%{http_code}\n' http://dev-admin.hsmyzg.com/src/ai_company/widget.js

# 5.5 浏览器里：登录后右下角出现悬浮球 → 打开 → 发一句话 → 有回复
#     再开两个浏览器/账号，各自会话互不可见（隔离）
```

## 6. 回滚

```bash
# 6.1 撤接入件
cd ~/dev_admin/backend_django
for f in backend_django/settings.py backend_django/urls.py \
         templates/views/WorkplaceLayout/index.html; do
  [ -f "$f.bak.ai-company" ] && cp "$f.bak.ai-company" "$f"
done
rm -rf ai_company static/src/ai_company
kill "$(cat ~/dev_admin/.run/backend.pid)"; ~/dev_admin/start.sh

# 6.2 停容器
cd ~/ai-company && docker compose down
```

## 7. 常见故障

| 现象 | 原因 / 处理 |
|---|---|
| `/api/ai/*` 全 404 | `urls.py` 的 include 落在 `path("api/", ...)` 之后了 —— 挪到前面 |
| POST 全 403 CSRF | `AI_COMPANY_CSRF_EXEMPT` 没生效，或跑的是旧代码（没重启 uvicorn） |
| 悬浮球不出现 | `index.html` 没引入，或 `/src/ai_company/widget.js` 404；看浏览器 console |
| 回复一直是「服务暂时不可用」 | 容器没起 / `8020` 不通 / key 没填；先看 `docker logs ai-company` |
| 回复 401 上游 | `AI_COMPANY_DEEPSEEK_API_KEY` 为空或过期 |
| 改了 settings 但行为没变 | **必须重启 uvicorn**（`start.sh` 不热加载） |
| 镜像拉不动 | 该机到不了 Docker Hub；确认走的是 ACR 地址 |
| 换账号后回复「conversation not owned by user」 | 上一位用户的 `conversation_id` 还留在 `sessionStorage` 里被带了过来。见 §8 |
| 改了 `widget.js` 但浏览器行为没变 | URL 没带版本号 → 吃旧缓存。安装器会自动写 `?v=<widget.js 短哈希>`；若仍不对，硬刷新一次 |

## 8. 换账号后报 `conversation not owned by user`

**根因**（已用 000000/000001 实测复现）：悬浮窗把 `conversation_id` 存在
`sessionStorage`。`sessionStorage` 是**按标签页**存的，同一个标签页里退出登录取
另一账号登录，它**不会失效** —— 于是 A 的 cid 被 B 带了进去。服务端按 `session_key`
推出的是 `admin:<B 的 police_users.id>`，拿它去查 A 的会话 → 归属校验失败 → 403。
**这不是 AI 出错，也不是权限配错，是前端残留状态。**

三道防线（都在 `widget.js`，已生效）：

1. `syncIdentity()`：启动时用 `/sessions/` 返回的 `user_id` 和 `sessionStorage`
   里记的上一位用户比对，**换人就丢掉 cid**。身份只认服务端返回值，前端不推导。
2. `send()` 撞到 403 时**丢掉 cid 原地重试一次**，用户无感、不用刷新。
3. `openSession()` 历史接口撞到 403 时清空并回落空态。

复现 / 验证脚本（在目标机跑，用真实登录接口建两条登录态，跑完自动清理）：

```bash
python3 ~/ai-company/verify_user_switch.py --ua 000000 --ub 000001 --password <密码>
```

它会把 [0] 建会话 → [1] 跨用户带 cid 得到 403（复现）→ [2] 丢 cid 后 200（兜底）
→ [3] 两边 `user_id` 不同 → [4] 跨用户读历史 403（隔离）逐条打出来。

**要点**：任何前端持久化「属于某个用户」的东西（会话指针、草稿、缓存），都必须
在登录人变化时失效。只按浏览器/标签页存、不绑身份，是个通用坑。

