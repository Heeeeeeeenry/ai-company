#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# 在目标机（222.223.144.110）上准备 ai-company 服务目录并起容器
#
# 用法：bash deploy/deploy-service.sh
#
# 只创建/使用 ~/ai-company/，不触碰目标机其它目录。
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_PORT=60022
SSH_HOST=my@222.223.144.110
REMOTE=/home/my/ai-company
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -p "$SSH_PORT")

say() { printf '\n\033[1;34m== %s ==\033[0m\n' "$*"; }
run() { ssh "${SSH_OPTS[@]}" "$SSH_HOST" "$@"; }
RSYNC_E="ssh -o BatchMode=yes -o ConnectTimeout=15 -p $SSH_PORT"

say "1/4 上传 compose / env 模板 / 初始化脚本 -> $REMOTE"
run "mkdir -p $REMOTE/data"
rsync -az -e "$RSYNC_E" \
  "$REPO_ROOT/deploy/docker-compose.yml" \
  "$REPO_ROOT/deploy/ai-company.env.example" \
  "$REPO_ROOT/deploy/init_service_env.py" \
  "$SSH_HOST:$REMOTE/"

say "2/4 生成 ai-company.env（密钥复用 dev_admin 的，绝不回显）"
run "cd $REMOTE && { command -v python3 >/dev/null && python3 init_service_env.py --dir $REMOTE \
        || ~/admin_runtime/python/bin/python3.12 init_service_env.py --dir $REMOTE; }"

say "3/4 确认镜像已在本地（免 registry 直传，见下方说明）"
run 'set -e
IMG=crpi-bqbwg1s59o0fx9ln.cn-beijing.personal.cr.aliyuncs.com/letter/ai-company:latest
if docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "  镜像已存在：$(docker images --format "{{.Repository}}:{{.Tag}} {{.Size}}" | grep ai-company)"
else
  echo "  [x] 目标机还没有该镜像。请先在开发机执行镜像流式传输："
  echo "      docker save $IMG | gzip -1 | ssh -p 60022 my@222.223.144.110 \"gunzip | docker load\""
  exit 1
fi
cd /home/my/ai-company && docker compose up -d'

say "4/4 自检"
run 'set -e
for i in 1 2 3 4 5 6 7 8 9 10; do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8020/ai/health || echo 000)
  echo "  尝试 $i: http=$code"
  [ "$code" = "200" ] && break
  sleep 2
done
echo "--- /ai/health ---"
curl -s http://127.0.0.1:8020/ai/health; echo
echo "--- 容器 ---"
docker ps --filter name=ai-company --format "{{.Names}}\t{{.Status}}\t{{.Ports}}"
echo "--- 日志末尾 ---"
docker logs --tail 15 ai-company 2>&1'

say "完成"
