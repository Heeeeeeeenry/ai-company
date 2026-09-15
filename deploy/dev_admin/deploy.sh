#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# 把 ai-company 嵌入件部署到民意智感中心管理端 dev_admin
#
# 目标机：my@222.223.144.110:60022（公钥免密）
# 用法：  bash deploy/dev_admin/deploy.sh
#
# 做的事：
#   1. 把接入件包 + 悬浮窗前端 + 安装器 rsync 到目标机 /tmp 暂存目录
#   2. 目标机上跑 patch_dev_admin.py（幂等打补丁）
#   3. 重启管理端后端（只重启 backend，file_parser/poller 已在跑不会被动）
#   4. 冒烟：/api/ai/health/ 未登录应 401（说明中间件在管），页面能拿到
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH_PORT=60022
SSH_HOST=my@222.223.144.110
STAGE=/tmp/ai-company-stage
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -p "$SSH_PORT")

say() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }

run() { ssh "${SSH_OPTS[@]}" "$SSH_HOST" "$@"; }

say "1/4 上传暂存目录 $STAGE"
run "rm -rf $STAGE && mkdir -p $STAGE"

rsync -az --delete \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=15 -p $SSH_PORT" \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='*.bak*' \
  "$REPO_ROOT/integration/ai_company/" "$SSH_HOST:$STAGE/ai_company/"

rsync -az \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=15 -p $SSH_PORT" \
  "$REPO_ROOT/deploy/dev_admin/static-src/ai_company/widget.js" \
  "$REPO_ROOT/deploy/dev_admin/static-src/ai_company/widget.css" \
  "$REPO_ROOT/deploy/dev_admin/patch_dev_admin.py" \
  "$SSH_HOST:$STAGE/"

say "2/4 打补丁"
run "cd $STAGE && ~/admin_runtime/python/bin/python3.12 patch_dev_admin.py --stage $STAGE"

say "3/4 重启管理端后端"
run 'set -e
PID_FILE=$HOME/dev_admin/.run/backend.pid
if [ -f "$PID_FILE" ]; then
  OLD=$(cat "$PID_FILE")
  kill "$OLD" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$OLD" 2>/dev/null || break
    sleep 0.5
  done
  echo "旧后端 PID=$OLD 已停止"
fi
$HOME/dev_admin/start.sh >/dev/null 2>&1 || true
sleep 3
echo "新后端 PID=$(cat "$PID_FILE" 2>/dev/null || echo 未知)"'

say "4/4 冒烟校验"
run 'set -e
WEB_PORT=$(grep -E "^WEB_PORT=" $HOME/dev_admin/.env | cut -d= -f2 | tr -d "[:space:]")
echo "管理端端口: $WEB_PORT"
echo -n "首页 /            -> HTTP "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:${WEB_PORT}/"
echo -n "未登录 /api/ai/health/ -> HTTP "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:${WEB_PORT}/api/ai/health/"
echo -n "悬浮窗 JS         -> HTTP "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:${WEB_PORT}/src/ai_company/widget.js"
echo -n "悬浮窗 CSS        -> HTTP "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:${WEB_PORT}/src/ai_company/widget.css"
echo "---- backend.log 末尾 ----"
tail -15 $HOME/dev_admin/.logs/backend.log'

say "完成"
echo "暂存目录 $STAGE 可留作复跑；不需要时：ssh -p $SSH_PORT $SSH_HOST \"rm -rf $STAGE\""
