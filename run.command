#!/bin/bash
# vel（VideoEnglishLearner）macOS 启动脚本
# 用法：双击本文件，或在终端执行 ./run.command [--no-open] [--asr] [--reinstall]
# 首次运行会自动建 .venv、装依赖；之后直接启动，Ctrl+C 停止。
set -u

cd "$(dirname "$0")" || { echo "无法进入项目目录"; exit 1; }
ROOT="$(pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

OPEN_BROWSER=1
WITH_ASR=0
REINSTALL=0

for arg in "$@"; do
    case "$arg" in
        --no-open) OPEN_BROWSER=0 ;;
        --asr) WITH_ASR=1 ;;
        --reinstall) REINSTALL=1 ;;
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) echo "未知参数：$arg（可用：--no-open / --asr / --reinstall）"; exit 2 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$*"; }

pick_python() {
    local candidate
    for candidate in python3.12 python3.11 python3.13 python3; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# ---------- 1. 虚拟环境 ----------
if [ ! -x "$PY" ]; then
    bold "首次运行：创建虚拟环境 .venv"
    BASE_PY="$(pick_python)" || {
        fail "没找到 Python 3.11+。先安装：brew install python@3.12"
        [ -t 0 ] && read -r -p "按回车关闭窗口…" _
        exit 1
    }
    "$BASE_PY" -m venv "$VENV" || {
        fail "创建 .venv 失败"
        [ -t 0 ] && read -r -p "按回车关闭窗口…" _
        exit 1
    }
    ok "已创建 .venv（$("$PY" -V 2>&1)）"
    REINSTALL=1
fi

# ---------- 2. 依赖 ----------
if [ "$REINSTALL" = 1 ] || ! "$PY" -c 'import fastapi, uvicorn, yt_dlp' >/dev/null 2>&1; then
    bold "安装运行依赖（requirements.txt）…"
    "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip 升级失败，继续用现有版本"
    "$PY" -m pip install -r requirements.txt || {
        fail "依赖安装失败，检查网络后重试（或 ./run.command --reinstall）"
        [ -t 0 ] && read -r -p "按回车关闭窗口…" _
        exit 1
    }
    ok "依赖就绪"
fi

if [ "$WITH_ASR" = 1 ]; then
    bold "安装本地转写依赖（requirements-asr.txt，体积较大）…"
    "$PY" -m pip install -r requirements-asr.txt || warn "ASR 依赖安装失败；YouTube 有字幕时不影响使用"
fi

# ---------- 3. ffmpeg / 配置 ----------
if ! command -v ffmpeg >/dev/null 2>&1; then
    FFMPEG_HINT="$("$PY" -c 'from app.config import cfg; print(cfg.get("download.ffmpeg_location", "") or "")' 2>/dev/null || true)"
    if [ -z "$FFMPEG_HINT" ]; then
        warn "没找到 ffmpeg：下载合并视频、导出 Anki 音频会受限"
        warn "安装：brew install ffmpeg（装好后重新运行本脚本即可）"
    fi
fi

# 不自动复制 config.example.toml：示例里带了一个占位代理（127.0.0.1:7890），
# 没有代理的人反而会连不上 YouTube。默认配置就能跑，需要时在网页「设置」里保存即可生成 config.toml。
if [ ! -f "$ROOT/config.toml" ]; then
    warn "还没有 config.toml：先用默认配置启动（设置页保存时会自动生成）"
    warn "国内网络访问 YouTube 需要代理：设置 → 网络 → 代理地址"
fi

# ---------- 4. 端口 / 已有实例 ----------
PORT="$("$PY" -c 'from app.config import cfg; print(int(cfg.get("server.port", 8787)))' 2>/dev/null || echo 8787)"
case "$PORT" in
    ''|*[!0-9]*) PORT=8787 ;;
esac
URL="http://127.0.0.1:$PORT"
healthy() { curl -sf --max-time 2 "$URL/api/health" >/dev/null 2>&1; }

if healthy; then
    ok "vel 已经在运行：$URL"
    [ "$OPEN_BROWSER" = 1 ] && open "$URL"
    exit 0
fi

# ---------- 5. 启动 ----------
bold "启动 vel（VideoEnglishLearner）…  Ctrl+C 停止"
"$PY" -m app.main &
SERVER_PID=$!

STOPPING=0
stop() {
    [ "$STOPPING" = 1 ] && return
    STOPPING=1
    echo
    warn "正在停止服务…"
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    ok "已停止"
    exit 0
}
trap stop INT TERM

if [ "$OPEN_BROWSER" = 1 ]; then
    (
        for _ in $(seq 1 120); do
            if healthy; then
                open "$URL"
                exit 0
            fi
            sleep 0.25
        done
    ) &
fi

wait "$SERVER_PID"
STATUS=$?
trap - INT TERM

if [ "$STOPPING" = 0 ] && [ "$STATUS" -ne 0 ]; then
    fail "服务异常退出（exit ${STATUS}），请查看上面的日志"
    [ -t 0 ] && read -r -p "按回车关闭窗口…" _
fi
exit "$STATUS"
