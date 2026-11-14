#!/usr/bin/env bash
# 启动 Linux 桌面版 QQ(NTQQ)。HOME 指向持久卷，保存登录态和聊天数据库(~/.config/QQ)。
set -euo pipefail
export HOME="${QQ_HOME:-/data/home}"
mkdir -p "$HOME"
# 等 X 就绪
for _ in $(seq 1 30); do xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1 && break; sleep 1; done
# QQ 需要自己的 dbus 会话
eval "$(dbus-launch --sh-syntax)"
# QQ 可执行文件（deb 安装后的常见路径）
QQ_BIN="$(command -v qq || true)"
[[ -z "$QQ_BIN" && -x /opt/QQ/qq ]] && QQ_BIN=/opt/QQ/qq
[[ -z "$QQ_BIN" ]] && { echo "[start-qq] 找不到 qq 可执行文件"; exit 1; }
exec "$QQ_BIN" --no-sandbox
