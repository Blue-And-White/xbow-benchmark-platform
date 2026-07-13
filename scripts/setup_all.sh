#!/usr/bin/env bash
# ============================================================
# xbow CTF 平台一键部署脚本
# 从零开始：下载仓库 → 生成 manifest → 构建104镜像 → 起平台
#
# 用法:
#   bash scripts/setup_all.sh
#
# 环境变量(可选):
#   XBOW_PORT=6888              平台端口(默认6888)
#   XBOW_ADMIN_PASSWORD=YOUR_PASSWORD 管理员密码
#   XBOW_N_BUILD=104            构建多少题(默认全部104)
# ============================================================
set -euo pipefail

PORT="${XBOW_PORT:-6888}"
ADMIN_PW="${XBOW_ADMIN_PASSWORD:-YOUR_PASSWORD}"
N_BUILD="${XBOW_N_BUILD:-104}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO_DIR="$ROOT/validation-benchmarks"
VENV_DIR="$ROOT/.venv"

echo "============================================"
echo "  xbow CTF 平台一键部署"
echo "  端口: $PORT | 管理员: admin/$ADMIN_PW"
echo "  构建题数: $N_BUILD"
echo "============================================"

# ---- 1. 检查 Docker ----
echo ""
echo "[1/6] 检查 Docker..."
if ! docker info >/dev/null 2>&1; then
  echo "  Docker 未运行，尝试启动..."
  if command -v dockerd >/dev/null; then
    nohup dockerd > /tmp/dockerd.log 2>&1 &
    sleep 5
  fi
  docker info >/dev/null 2>&1 || { echo "ERROR: Docker 不可用"; exit 1; }
fi
echo "  ✅ Docker $(docker --version | awk '{print $3}' | tr -d ',')"

# ---- 2. 下载 xbow 仓库 ----
echo ""
echo "[2/6] 下载 xbow benchmark 仓库(1.25GB)..."
if [ -d "$REPO_DIR/benchmarks" ] && [ "$(ls "$REPO_DIR/benchmarks" | wc -l)" -ge 100 ]; then
  echo "  ✅ 仓库已存在 ($(ls "$REPO_DIR/benchmarks" | wc -l) 题)，跳过"
else
  cd "$ROOT"
  rm -rf validation-benchmarks validation-benchmarks-main vb.tar.gz
  # 优先 GitHub 直连，失败走 gh-proxy
  if curl -fsSL --max-time 30 -o vb.tar.gz \
    "https://github.com/xbow-engineering/validation-benchmarks/archive/refs/heads/main.tar.gz" 2>/dev/null; then
    echo "  GitHub 直连成功"
  else
    echo "  GitHub 直连失败，使用 gh-proxy..."
    curl -fL --retry 5 -o vb.tar.gz \
      "https://gh-proxy.com/https://github.com/xbow-engineering/validation-benchmarks/archive/refs/heads/main.tar.gz"
  fi
  tar xzf vb.tar.gz && mv validation-benchmarks-main validation-benchmarks && rm vb.tar.gz
  echo "  ✅ 下载完成 ($(ls "$REPO_DIR/benchmarks" | wc -l) 题)"
fi
cd "$ROOT"

# ---- 3. Python 环境 + 依赖 ----
echo ""
echo "[3/6] 准备 Python 环境..."
if [ ! -f "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR" 2>/dev/null || {
    # fallback: 系统 python
    VENV_DIR=""
  }
fi
PIP="${VENV_DIR:-/usr/local}/bin/pip"
PY="${VENV_DIR:-/usr/local}/bin/python"
if [ -f "$PIP" ]; then
  "$PIP" install -q -r requirements.txt 2>&1 | tail -2
else
  pip3 install -q -r requirements.txt
fi
echo "  ✅ Python 依赖就绪"

# ---- 4. 生成 flag manifest ----
echo ""
echo "[4/6] 生成 flag manifest..."
"$PY" scripts/gen_manifest.py 2>&1 | tail -3
echo "  ✅ flag_manifest.json 就绪"

# ---- 5. 构建 104 个靶机镜像 ----
echo ""
echo "[5/6] 构建 ${N_BUILD} 个靶机镜像(后台, 可能需要数小时)..."
BUILT=$(docker images --format '{{.Repository}}' 2>/dev/null | grep -oE '^xben-[0-9]+-[0-9]+' | sort -u | wc -l)
echo "  当前已构建: $BUILT / 104"
if [ "$BUILT" -lt "$N_BUILD" ]; then
  echo "  开始构建(后台)... 日志: /root/prebuild.log"
  # 启动 apt-cacher-ng(加速国内构建)
  if ! pgrep apt-cacher-ng >/dev/null 2>&1 && command -v apt-cacher-ng >/dev/null 2>&1; then
    nohup apt-cacher-ng -c /etc/apt-cacher-ng > /tmp/acng.log 2>&1 &
    sleep 2
  fi
  "$PY" scripts/prebuild.py --repo "$REPO_DIR" --n "$N_BUILD" --log /root/prebuild.log &
  BUILD_PID=$!
  echo "  构建PID: $BUILD_PID (后台运行, 不阻塞平台启动)"
  echo "  查看进度: tail -f /root/prebuild.log"
else
  echo "  ✅ 全部镜像已构建"
fi

# ---- 6. 启动平台 ----
echo ""
echo "[6/6] 启动平台(端口 $PORT)..."
pkill -f "uvicorn.*app.main" 2>/dev/null; sleep 1
cd "$ROOT"
export XBEN_REPO_DIR="$REPO_DIR"
export XBEN_RUNS_DIR="$ROOT/runs"
export XBEN_DATA_DIR="$ROOT/data"
export XBEN_FLAG_MANIFEST="$ROOT/flag_manifest.json"
export XBEN_ADMIN_USER=admin
export XBEN_ADMIN_PASSWORD="$ADMIN_PW"
export XBEN_PUBLIC_BASE_URL="http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):$PORT"
export XBEN_CHALLENGE_HOST=127.0.0.1
export XBEN_REGISTRATION_CODE=ROOTLAB@123
export XBEN_SECRET_KEY="xbow-$(openssl rand -hex 16 2>/dev/null || echo dev-secret)"
mkdir -p "$ROOT/runs" "$ROOT/data"

nohup "${VENV_DIR:-/usr/local}/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORT" \
  > /root/platform.log 2>&1 &
sleep 5

# ---- 验证 ----
echo ""
echo "============================================"
if curl -sS --max-time 5 "http://127.0.0.1:$PORT/health" | grep -q ok; then
  echo "  ✅ 平台启动成功!"
  echo ""
  echo "  地址: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):$PORT"
  echo "  管理员: admin / $ADMIN_PW"
  echo "  注册码: ROOTLAB@123"
  echo ""
  echo "  靶机构建进度: tail -f /root/prebuild.log"
  echo "  平台日志: tail -f /root/platform.log"
else
  echo "  ❌ 平台启动失败，查看日志: /root/platform.log"
  exit 1
fi
echo "============================================"
