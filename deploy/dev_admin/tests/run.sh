#!/usr/bin/env bash
# 嵌入件回归（canonical 入口）：悬浮窗行为 + 安装器幂等/不写 .bak/git 回滚
#
#   bash deploy/dev_admin/tests/run.sh
#
# 只用 node + python3，无第三方依赖。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v node >/dev/null || { echo "[x] 需要 node（悬浮窗回归用）"; exit 1; }

echo "== 1/2 悬浮窗回归（真导入 widget.js）=="
node "$HERE/widget_regression.mjs"

echo
echo "== 2/2 安装器回归（版本号 / 幂等 / 不写 .bak）=="
python3 "$HERE/installer_regression.py"

echo
echo "== 全过 =="
