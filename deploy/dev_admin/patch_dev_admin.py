#!/usr/bin/env python3
"""把 ai-company 嵌入件装进民意智感中心管理端（dev_admin）。

设计要点
--------
* **幂等**：重复执行不会重复插入，也不会改坏已打好的补丁。
* **只碰 3 个文件 + 2 个新目录**：
    - 改 backend_django/backend_django/settings.py   (INSTALLED_APPS + 配置块)
    - 改 backend_django/backend_django/urls.py        (挂 /api/ai/)
    - 改 backend_django/templates/views/WorkplaceLayout/index.html (悬浮窗)
    - 增 backend_django/ai_company/                   (接入件包)
    - 增 backend_django/static/src/ai_company/        (悬浮窗前端的 js/css)
  改动前**不生成 .bak** —— dev_admin 本身就是 git 仓库，回滚走
  ``git checkout``，安装结束会打印对应命令。若目标文件不在仓库内或没被
  跟踪，会显式告警（不制造「以为能回滚、其实不能」的假象）。

用法（在 222.223.144.110 上以 my 用户执行）::

    python3 patch_dev_admin.py --stage /tmp/ai-company-stage

    --stage 目录里应有：ai_company/（包）、widget.js、widget.css
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARK = "ai-company 嵌入式 AI 助手"
URL_LINE = '    path("api/ai/", include("ai_company.urls_host")),'
ANCHOR_URL = '    path("api/", include("api.urls")),'
ANCHOR_APP = 'INSTALLED_APPS = ['
# AI 取数工具层的内网端点：挂在 /api/ **之外**。
# 中间件只校验 /api/ 前缀，而这条通道的调用方是 ai-company 进程（它没有浏览器
# cookie），因此改由共享 token 鉴权，见 ai_company/urls_internal.py。
INTERNAL_URL_LINE = '    path("ai-internal/", include("ai_company.urls_internal")),'
# 共享 token 存两处、必须一致：宿主 settings.py（校验方）+ 容器 env（发起方）。
# **重复安装绝不能重新生成** —— 否则容器不重启就一直 401。
ANCHOR_TOKEN = 'AI_COMPANY_USER_PREFIX = "admin"'
TOKEN_RE = re.compile(r'AI_COMPANY_INTERNAL_TOKEN\s*=\s*"([0-9a-f]{32,})"')
ENV_TOKEN_RE = re.compile(r'^AI_COMPANY_HOST_INTERNAL_TOKEN=([0-9a-f]{32,})\s*$', re.M)

SETTINGS_BLOCK = '''

# ── ai-company 嵌入式 AI 助手 ────────────────────────────────────────
# 悬浮对话窗的后端（同机 docker 容器，只绑 127.0.0.1，不对外暴露）
AI_COMPANY_BASE_URL = os.environ.get("AI_COMPANY_BASE_URL", "http://127.0.0.1:8020")
# user_id 前缀：最终形如 admin:<police_users.id>，由服务端从 session_key 推导
AI_COMPANY_USER_PREFIX = "admin"
# 单轮对话超时（秒）：LLM + 多智能体编排，给足时间
AI_COMPANY_TIMEOUT = 180
# 管理端前端（static/src/api/http.js）只带 cookie、从不发 CSRF token，
# 因此嵌入件必须同样豁免 CSRF，否则所有 POST 都会被 403 挡掉。
AI_COMPANY_CSRF_EXEMPT = True
'''

# {v} = widget.js 的短哈希。带在 URL 上是为了防浏览器缓存：改完前端如果还挂着
# 旧 JS，用户会继续看到老 bug，很容易误判成「没修好」。
WIDGET_HTML = '''    <!-- AI 助手悬浮窗（全站常驻，见 static/src/ai_company/widget.js）-->
    <link rel="stylesheet" href="/src/ai_company/widget.css?v={v}" />
    <script type="module" src="/src/ai_company/widget.js?v={v}"></script>
'''

# 版本号一律当不透明字符串处理：写死成 [0-9a-f]+ 时，只要版本串里出现非十六进制
# 字符（或大写），正则就不匹配 -> 静默走「版本一致，跳过」-> 前端永远升不上去。
VER_RE = re.compile(r'(/src/ai_company/widget\.(?:js|css))\?v=[^"]*')
# 已引入但还没带版本号时，把 ?v= 补上（老版本安装器留下的产物就是这种）
BARE_RE = re.compile(r'(/src/ai_company/widget\.(?:js|css))"')


def stamp_version(text: str, version: str) -> str:
    """把 widget.js/css 的 URL 标注成 ?v=<version>：已有则改写，没有则补上。"""
    new = VER_RE.sub(rf"\1?v={version}", text)
    if new != text:
        return new
    return BARE_RE.sub(rf'\1?v={version}"', new)

report: list[str] = []


def log(msg: str) -> None:
    report.append(msg)
    print(msg)


def git_root(path: Path) -> Path | None:
    """向上找所属 git 仓库根（回滚要用）。"""
    for d in (path, *path.parents):
        if (d / ".git").exists():
            return d
    return None


def note_rollback(path: Path) -> None:
    """不写 .bak：dev_admin 是 git 仓库，回滚交给 git。

    但「能被 git 回滚」这个前提得成立 —— 目标文件若不在仓库里、或没被
    跟踪，就显式告警，别制造「以为能回滚」的假象。
    """
    repo = git_root(path)
    if repo is None:
        log(f"  [!] {path} 不在 git 仓库内 —— 本次改动无法回滚")
        return
    r = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", str(path.relative_to(repo))],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log(f"  [!] {path} 未被 git 跟踪 —— 本次改动无法回滚")


def resolve_internal_token(settings_py: Path, env_file: Path) -> str:
    """取（或生成）AI 取数工具层的共享 token。

    优先复用磁盘上已有的：settings.py > 容器 env > 新生成。**绝不无脑重生成**，
    否则每次安装都会把已经跑起来的容器踢成 401。
    """
    try:
        m = TOKEN_RE.search(settings_py.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        if env_file.is_file():
            m = ENV_TOKEN_RE.search(env_file.read_text(encoding="utf-8"))
            if m:
                return m.group(1)
    except OSError:
        pass
    return secrets.token_hex(24)


def patch_settings(path: Path, token: str) -> bool:
    text = path.read_text(encoding="utf-8")
    changed = False

    if "import os" not in text:
        text = "import os\n" + text
        changed = True

    if '"ai_company"' not in text:
        if ANCHOR_APP not in text:
            raise SystemExit(f"[x] 在 {path} 里找不到锚点 {ANCHOR_APP!r}")
        text = text.replace(ANCHOR_APP, ANCHOR_APP + '\n    "ai_company",', 1)
        changed = True
        log("  INSTALLED_APPS += ai_company")
    else:
        log("  INSTALLED_APPS 已含 ai_company（跳过）")

    if MARK not in text:
        text = text.rstrip("\n") + "\n" + SETTINGS_BLOCK
        changed = True
        log("  追加 AI_COMPANY_* 配置块")
    else:
        log("  AI_COMPANY_* 配置块已存在（跳过）")

    if "AI_COMPANY_INTERNAL_TOKEN" not in text:
        if ANCHOR_TOKEN not in text:
            raise SystemExit(f"[x] 在 {path} 里找不到锚点 {ANCHOR_TOKEN!r}")
        block = (
            "\n# AI 取数工具层内部凭据：ai-company 回宿主调「受控取数工具」时携带。\n"
            "# 与容器 env 的 AI_COMPANY_HOST_INTERNAL_TOKEN 必须一致。\n"
            f'AI_COMPANY_INTERNAL_TOKEN = "{token}"'
        )
        text = text.replace(ANCHOR_TOKEN, ANCHOR_TOKEN + block, 1)
        changed = True
        log("  AI_COMPANY_INTERNAL_TOKEN 已写入 settings.py")
    else:
        log("  AI_COMPANY_INTERNAL_TOKEN 已存在（跳过）")

    if changed:
        note_rollback(path)
        path.write_text(text, encoding="utf-8")
    return changed


def sync_service_env(env_file: Path, token: str, host_base_url: str) -> bool:
    """把宿主地址 + 内部 token 同步进 ai-company 容器 env（600，幂等）。

    容器里的 ai-company 靠这两个值回宿主取数；缺任一个就自动降级为普通对话
    （不会报错，只是 AI 不再声称查过数据）。
    """
    if not env_file.is_file():
        log(f"  [!] 未找到容器 env {env_file}，跳过同步（容器起来后需手动补这两个变量）")
        return False

    text = env_file.read_text(encoding="utf-8")
    wanted = {
        "AI_COMPANY_HOST_BASE_URL": host_base_url,
        "AI_COMPANY_HOST_INTERNAL_TOKEN": token,
    }
    changed = False
    for key, value in wanted.items():
        line = f"{key}={value}"
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
        found = pattern.search(text)
        if found:
            if found.group(0) == line:
                continue
            text = pattern.sub(line, text)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
        changed = True
        log(f"  容器 env: {key} 已更新")

    if changed:
        env_file.write_text(text, encoding="utf-8")
        try:
            env_file.chmod(0o600)
        except OSError:
            pass
    else:
        log("  容器 env 已一致（跳过）")
    return changed


def patch_urls(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if ANCHOR_URL not in text:
        raise SystemExit(f"[x] 在 {path} 里找不到锚点 {ANCHOR_URL!r}")
    changed = False

    if "urls_host" not in text:
        # 必须挂在 api/ 之前：否则 /api/ai/... 会先被 api.urls 抢走
        text = text.replace(ANCHOR_URL, URL_LINE + "\n" + ANCHOR_URL, 1)
        changed = True
        log("  urls.py 挂载 /api/ai/ -> ai_company.urls_host")
    else:
        log("  urls.py 已挂载 /api/ai/（跳过）")

    if "urls_internal" not in text:
        # 挂在 /api/ 之外，中间件不参与；由共享 token 鉴权
        text = text.replace(ANCHOR_URL, INTERNAL_URL_LINE + "\n" + ANCHOR_URL, 1)
        changed = True
        log("  urls.py 挂载 /ai-internal/ -> ai_company.urls_internal（取数工具层）")
    else:
        log("  urls.py 已挂载 /ai-internal/（跳过）")

    if changed:
        note_rollback(path)
        path.write_text(text, encoding="utf-8")
    return changed


def patch_index_html(path: Path, version: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if "ai_company/widget.js" in text:
        # 已引入过：把 ?v= 换成新版本（升级前端时很关键，否则浏览器吃旧 JS）
        new = stamp_version(text, version)
        if new != text:
            note_rollback(path)
            path.write_text(new, encoding="utf-8")
            log(f"  index.html 版本号已标注 -> ?v={version}")
            return True
        log(f"  index.html 已引入悬浮窗且版本一致 ?v={version}（跳过）")
        return False
    if "</body>" not in text:
        raise SystemExit(f"[x] 在 {path} 里找不到 </body>")
    text = text.replace("</body>", WIDGET_HTML.format(v=version) + "  </body>", 1)
    note_rollback(path)
    path.write_text(text, encoding="utf-8")
    log(f"  index.html 引入悬浮窗 js/css（?v={version}）")
    return True


def copy_package(stage: Path, bd: Path) -> None:
    src = stage / "ai_company"
    if not src.is_dir():
        raise SystemExit(f"[x] 找不到 {src}")
    dst = bd / "ai_company"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.bak*"),
    )
    n = sum(1 for _ in dst.rglob("*.py"))
    log(f"  包 -> {dst}（{n} 个 .py）")


def copy_static(stage: Path, bd: Path) -> None:
    dst = bd / "static" / "src" / "ai_company"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("widget.js", "widget.css"):
        src = stage / name
        if not src.is_file():
            raise SystemExit(f"[x] 找不到 {src}")
        shutil.copy2(src, dst / name)
        log(f"  {name} -> {dst / name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, help="暂存目录（含 ai_company/ + widget.js/css）")
    ap.add_argument("--dev-admin", default=str(Path.home() / "dev_admin"))
    ap.add_argument("--service-env", default=str(Path.home() / "ai-company" / "ai-company.env"),
                    help="ai-company 容器 env（同步 AI_COMPANY_HOST_* 用）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stage = Path(args.stage).resolve()
    dev_admin = Path(args.dev_admin).resolve()
    env_file = Path(args.service_env).expanduser().resolve()
    bd = dev_admin / "backend_django"

    for p in (stage, dev_admin, bd):
        if not p.exists():
            raise SystemExit(f"[x] 路径不存在: {p}")

    log(f"== ai-company 嵌入件安装 ==")
    log(f"  stage      : {stage}")
    log(f"  dev_admin  : {dev_admin}")

    settings_py = bd / "backend_django" / "settings.py"
    urls_py = bd / "backend_django" / "urls.py"
    index_html = bd / "templates" / "views" / "WorkplaceLayout" / "index.html"
    for p in (settings_py, urls_py, index_html):
        if not p.is_file():
            raise SystemExit(f"[x] 找不到 {p}")

    if args.dry_run:
        log("(--dry-run：只列出将要做的改动)")
        log(f"  将复制 ai_company 包      -> {bd / 'ai_company'}")
        log(f"  将复制 widget.js/css      -> {bd / 'static/src/ai_company'}")
        log(f"  将补丁 {settings_py.name} / {urls_py.name} / index.html")
        return 0

    log("[1/3] 复制接入件")
    copy_package(stage, bd)
    copy_static(stage, bd)

    # 用暂存区里 widget.js 的短哈希当版本号（防浏览器缓存）
    stage_widget = stage / "widget.js"
    version = hashlib.sha256(stage_widget.read_bytes()).hexdigest()[:8] if stage_widget.is_file() else "1"

    log("[2/3] 打补丁")
    # 容器里的 ai-company 用这个地址回宿主调「受控取数工具」。容器里的 127.0.0.1
    # 是容器自己，所以必须走 docker 网关：compose 里已加
    # extra_hosts: host.docker.internal:host-gateway，dev_admin 的 uvicorn 绑
    # 0.0.0.0:15173，命中宿主的 loopback 监听。
    host_base_url = os.environ.get(
        "AI_COMPANY_HOST_BASE_URL",
        f"http://host.docker.internal:{os.environ.get('AI_COMPANY_HOST_PORT', '15173')}",
    )
    token = resolve_internal_token(settings_py, env_file)
    patch_settings(settings_py, token)
    patch_urls(urls_py)
    patch_index_html(index_html, version)
    env_changed = sync_service_env(env_file, token, host_base_url)

    log("[3/3] 完成")
    log(f"  时间戳: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if env_changed:
        log("[!] 容器 env 有改动 -> 必须**重建**容器才生效：")
        log(f"    cd {env_file.parent} && docker compose up -d")
        log("    注意 docker compose restart 不会重读 env_file，只有 up -d 会重建。")
        log("    不重建则 AI 取数通道静默降级为普通对话（不报错，但取不到数）。")
    repo = git_root(dev_admin) or dev_admin
    rels = " ".join(
        str(p.relative_to(repo)) for p in (settings_py, urls_py, index_html)
    )
    log("")
    log("回滚（dev_admin 是 git 仓库，无需 .bak）：")
    log(f"  cd {dev_admin} && git checkout -- {rels}")
    log(f"  rm -rf {bd / 'ai_company'} {bd / 'static' / 'src' / 'ai_company'}")
    log("  然后重启： bash start.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
