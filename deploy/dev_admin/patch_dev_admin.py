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
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MARK = "ai-company 嵌入式 AI 助手"
URL_LINE = '    path("api/ai/", include("ai_company.urls_host")),'
ANCHOR_URL = '    path("api/", include("api.urls")),'
ANCHOR_APP = 'INSTALLED_APPS = ['

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


def patch_settings(path: Path) -> bool:
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

    if changed:
        note_rollback(path)
        path.write_text(text, encoding="utf-8")
    return changed


def patch_urls(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if "urls_host" in text:
        log("  urls.py 已挂载 /api/ai/（跳过）")
        return False
    if ANCHOR_URL not in text:
        raise SystemExit(f"[x] 在 {path} 里找不到锚点 {ANCHOR_URL!r}")
    # 必须挂在 api/ 之前：否则 /api/ai/... 会先被 api.urls 抢走
    text = text.replace(ANCHOR_URL, URL_LINE + "\n" + ANCHOR_URL, 1)
    note_rollback(path)
    path.write_text(text, encoding="utf-8")
    log("  urls.py 挂载 /api/ai/ -> ai_company.urls_host")
    return True


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
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    stage = Path(args.stage).resolve()
    dev_admin = Path(args.dev_admin).resolve()
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
    patch_settings(settings_py)
    patch_urls(urls_py)
    patch_index_html(index_html, version)

    log("[3/3] 完成")
    log(f"  时间戳: {time.strftime('%Y-%m-%d %H:%M:%S')}")
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
