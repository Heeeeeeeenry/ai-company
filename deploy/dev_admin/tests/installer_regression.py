#!/usr/bin/env python3
"""安装器回归：版本号标注 + main() 端到端装机（假 dev_admin 上跑真 main()）。

要点：
  - 不生成 .bak（git 仓库自己兜回滚）；未跟踪文件要显式告警
  - 幂等：二次安装全项跳过、不改盘

    python3 installer_regression.py [patch_dev_admin.py 路径]
"""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT = HERE.parent / "patch_dev_admin.py"

p = f = 0


def ck(cond: bool, label: str, extra: str = "") -> None:
    global p, f
    if cond:
        p += 1
        print(f"  [PASS] {label}")
    else:
        f += 1
        print(f"  [FAIL] {label}  {extra}")


def load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("patch_dev_admin", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"[x] 无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stamp(m) -> None:
    print("A 版本号标注")
    bare = '<script src="/src/ai_company/widget.js"></script>\n<link href="/src/ai_company/widget.css" rel="stylesheet">'
    ck(m.stamp_version(bare, "abc123").count("?v=abc123") == 2, "裸 URL 补上版本号")
    # 版本串当不透明字符串：写死 [0-9a-f]+ 会让 v1/V1/rc 号静默跳过
    for v in ("deadbeef", "V1", "v1", "0.9.1-rc2"):
        one = f'<script src="/src/ai_company/widget.js?v={v}"></script>'
        out = m.stamp_version(one, "NEWV")
        ck("?v=NEWV" in out and v not in out, f"旧版本号 {v!r} -> 改写", out)
    same = '<script src="/src/ai_company/widget.js?v=abc123"></script>'
    ck(m.stamp_version(same, "abc123") == same, "同版本号 -> 幂等")


def test_main(m) -> None:
    print("B main() 端到端装机")
    root = pathlib.Path(tempfile.mkdtemp(prefix="hermes-verify-devadmin-"))
    da = root / "dev_admin"
    bd = da / "backend_django"
    (bd / "backend_django").mkdir(parents=True)
    (bd / "templates" / "views" / "WorkplaceLayout").mkdir(parents=True)
    stage = root / "stage"
    (stage / "ai_company").mkdir(parents=True)
    (stage / "ai_company" / "views.py").write_text("x = 1\n", encoding="utf-8")
    (stage / "widget.js").write_text("console.log('w')\n", encoding="utf-8")
    (stage / "widget.css").write_text(".a{}\n", encoding="utf-8")
    st = bd / "backend_django" / "settings.py"
    ur = bd / "backend_django" / "urls.py"
    st.write_text(f"import os\nINSTALLED_APPS = [\n    {m.ANCHOR_APP}\n]\n", encoding="utf-8")
    ur.write_text(f"urlpatterns = [\n    {m.ANCHOR_URL}\n]\n", encoding="utf-8")
    idx = bd / "templates" / "views" / "WorkplaceLayout" / "index.html"
    idx.write_text("<html>\n<body>\n<p>hi</p>\n</body>\n</html>\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(da)], check=True)
    subprocess.run(["git", "-C", str(da), "add", "-A"], check=True)

    argv = sys.argv
    # --service-env 必须显式指到临时文件：默认值是 ~/ai-company/ai-company.env，
    # 跑测试时绝不能碰开发机上的真实容器 env。
    svc = root / "ai-company.env"
    svc.write_text("AI_COMPANY_SERVICE_MODE=1\nAI_COMPANY_HOST_BASE_URL=http://127.0.0.1:9\n",
                   encoding="utf-8")
    sys.argv = ["patch_dev_admin.py", "--stage", str(stage), "--dev-admin", str(da),
                "--service-env", str(svc)]
    try:
        m.report.clear()
        rc = m.main()
        out = "\n".join(m.report)
        ck(rc == 0, "main() 返回 0", str(rc))
        ck('"ai_company"' in st.read_text(encoding="utf-8"), "settings.py 写入 INSTALLED_APPS")
        ck(m.MARK in st.read_text(encoding="utf-8"), "settings.py 写入 AI_COMPANY_* 配置块")
        ck("api/ai/" in ur.read_text(encoding="utf-8"), "urls.py 挂载 /api/ai/")
        html = idx.read_text(encoding="utf-8")
        ck("widget.js?v=" in html and "widget.css?v=" in html, "index.html 引入悬浮窗 + 版本号")
        ck((bd / "ai_company" / "views.py").is_file(), "接入件包已复制")
        ck((bd / "static" / "src" / "ai_company" / "widget.js").is_file(), "widget.js 已复制")
        leftover = [x.name for x in da.rglob("*.bak*")]
        ck(not leftover, "全程不生成任何 .bak", str(leftover))
        ck("git checkout --" in out and "无需 .bak" in out, "打印 git 回滚命令")
        ck("无法回滚" not in out, "文件被 git 跟踪 -> 不误报告警", out)

        # ── 取数工具层：内网挂载 + 共享 token 双写 ──────────────────
        urls_now = ur.read_text(encoding="utf-8")
        ck("ai-internal/" in urls_now, "urls.py 挂载 /ai-internal/（取数工具层）")
        ck(
            urls_now.index(m.INTERNAL_URL_LINE) < urls_now.index(m.ANCHOR_URL),
            "/ai-internal/ 挂在 api/ 之前（否则被 api.urls 抢走）",
        )
        st_now = st.read_text(encoding="utf-8")
        mt = m.TOKEN_RE.search(st_now)
        ck(mt is not None, "settings.py 写入 AI_COMPANY_INTERNAL_TOKEN", st_now[-200:])
        token = mt.group(1) if mt else ""
        ck(len(token) == 48, "token 为 48 位 hex", str(len(token)))
        env_now = svc.read_text(encoding="utf-8")
        ck(f"AI_COMPANY_HOST_INTERNAL_TOKEN={token}" in env_now, "容器 env 写入同一个 token")
        ck("AI_COMPANY_HOST_BASE_URL=http://host.docker.internal:15173" in env_now,
           "容器 env 写入宿主地址（走 docker 网关）")
        ck("AI_COMPANY_HOST_BASE_URL=http://127.0.0.1:9" not in env_now, "旧值被覆盖（不追加重复行）")
        ck((svc.stat().st_mode & 0o777) == 0o600, "容器 env 权限 600",
           oct(svc.stat().st_mode & 0o777))

        m.report.clear()
        rc2 = m.main()
        ck(rc2 == 0 and "已含 ai_company（跳过）" in "\n".join(m.report), "二次安装幂等（跳过）")
        ck(idx.read_text(encoding="utf-8") == html, "二次安装不改 index.html")
        ck(not list(da.rglob("*.bak*")), "二次安装仍无 .bak")
        # 这条最要紧：重复安装若重新生成 token，已经跑着的容器会一直 401
        mt2 = m.TOKEN_RE.search(st.read_text(encoding="utf-8"))
        ck(mt2 is not None and mt2.group(1) == token, "二次安装不重新生成 token")
        ck(svc.read_text(encoding="utf-8") == env_now, "二次安装不改容器 env")
        ck("已挂载 /ai-internal/（跳过）" in "\n".join(m.report), "内网挂载幂等")
    finally:
        sys.argv = argv
        shutil.rmtree(root, ignore_errors=True)

    print("C 未跟踪文件 -> 显式告警")
    d = pathlib.Path(tempfile.mkdtemp(prefix="hermes-verify-loose-"))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    loose = d / "loose.py"
    loose.write_text("y = 2\n", encoding="utf-8")
    m.report.clear()
    m.note_rollback(loose)
    ck(any("未被 git 跟踪" in x for x in m.report), "未跟踪 -> 告警无法回滚", str(m.report))
    ck(any("不在 git 仓库内" in x for x in _warn_outside(m)), "非 git 目录 -> 告警无法回滚")
    shutil.rmtree(d, ignore_errors=True)


def _warn_outside(m) -> list[str]:
    d = pathlib.Path(tempfile.mkdtemp(prefix="hermes-verify-nogit-"))
    f = d / "index.html"
    f.write_text("<html></html>", encoding="utf-8")
    m.report.clear()
    m.note_rollback(f)
    shutil.rmtree(d, ignore_errors=True)
    return m.report


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT
    if not target.is_file():
        raise SystemExit(f"[x] 找不到 {target}")
    mod = load(target)
    test_stamp(mod)
    test_main(mod)
    print(f"\n[installer_regression] 通过 {p} / 失败 {f}")
    sys.exit(1 if f else 0)
