#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/logs /data/scan /data/home /data/media /data/plain /run

# 读密钥需要读取 QQ 进程内存（/proc/pid/mem）或 gdb 断点。容器需以 --cap-add SYS_PTRACE 运行。
if [[ "$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 0)" != "0" ]]; then
  echo "[entrypoint] 提示：ptrace_scope 非 0，若无法读取 QQ 内存/gdb 断点失败，请以 --cap-add SYS_PTRACE（并 seccomp:unconfined）运行容器。"
fi

exec /usr/bin/supervisord -n -c /etc/supervisor/qq.conf
