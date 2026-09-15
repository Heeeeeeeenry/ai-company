#!/usr/bin/env python3
"""在目标机上初始化 ai-company 服务目录的运行时环境变量。

职责：把 dev_admin 已经在用的那把公网 DeepSeek key 搬进 ``~/ai-company/ai-company.env``。

设计原则：
* **密钥永不回显**：只写文件，绝不 print、绝不进日志。
* **幂等**：已填过的 key 不覆盖；文件已存在则只补缺失项。
* 不触碰 ``~/dev_admin`` 的任何文件（只读它的 .env 取 key）。

用法（在 222.223.144.110 上，用户 my）::

    python3 init_service_env.py [--dir ~/ai-company]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
from pathlib import Path

KEY_NAME = "AI_COMPANY_DEEPSEEK_API_KEY"
SRC_ENV_KEYS = ("LLM_API_KEY", "DEEPSEEK_API_KEY", "AI_COMPANY_DEEPSEEK_API_KEY")


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def find_source_key() -> tuple[str, str] | None:
    """按优先级从宿主 .env 里找一把可用的 DeepSeek key。"""
    for candidate in (
        Path.home() / "dev_admin" / ".env",
        Path.home() / "dev_admin" / "backend_django" / ".env",
    ):
        env = read_env(candidate)
        for name in SRC_ENV_KEYS:
            val = env.get(name, "")
            if val:
                return str(candidate), val
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path.home() / "ai-company"))
    args = ap.parse_args()

    base = Path(args.dir).expanduser().resolve()
    env_file = base / "ai-company.env"
    example = base / "ai-company.env.example"
    (base / "data").mkdir(parents=True, exist_ok=True)

    if not env_file.exists():
        if not example.is_file():
            raise SystemExit(f"[x] 找不到模板 {example}（先把 deploy/ai-company.env.example 传上来）")
        shutil.copy2(example, env_file)
        print(f"  由模板生成 {env_file}")
        shutil.copymode(example, env_file)

    text = env_file.read_text(encoding="utf-8")
    current = read_env(env_file).get(KEY_NAME, "")

    if current:
        print(f"  {KEY_NAME} 已存在（{len(current)} 字符），保持不动")
    else:
        found = find_source_key()
        if not found:
            print("  [警告] 没能从 ~/dev_admin/.env 里找到可用的 key；")
            print(f"         请手动编辑 {env_file} 填 {KEY_NAME}=<公网 DeepSeek key>")
        else:
            src, key = found
            text = re.sub(
                rf"(?m)^{KEY_NAME}=.*$",
                f"{KEY_NAME}={key}",
                text,
            )
            if f"{KEY_NAME}=" not in text:
                text = text.rstrip("\n") + f"\n{KEY_NAME}={key}\n"
            print(f"  已从 {src} 取 key（{len(key)} 字符）写入 {env_file}")

    env_file.write_text(text, encoding="utf-8")
    os.chmod(env_file, stat.S_IRUSR | stat.S_IWUSR)  # 600：含密钥

    # ── 自检：只看结构，不回显值 ──
    final = read_env(env_file)
    k = final.get(KEY_NAME, "")
    print("\n  == 配置自检（值不显示）==")
    print(f"   服务模式        : {final.get('AI_COMPANY_SERVICE_MODE', '(缺)')}")
    print(f"   provider        : {final.get('AI_COMPANY_MODEL_PROVIDER', '(缺)')}")
    print(f"   base_url        : {final.get('AI_COMPANY_DEEPSEEK_BASE_URL', '(缺)')}")
    print(f"   model           : {final.get('AI_COMPANY_DEEPSEEK_MODEL', '(缺)')}")
    print(f"   key             : {'已设置 ' + str(len(k)) + ' 字符' if k else '**缺失**'}")
    print(f"   env 文件权限    : {oct(env_file.stat().st_mode)[-3:]}")
    return 0 if k else 1


if __name__ == "__main__":
    sys.exit(main())
