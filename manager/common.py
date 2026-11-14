"""管理面板公共工具：路径、命令执行、持久状态、日志尾读。"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("manager")

DATA = Path(os.environ.get("DATA_DIR", "/data"))
LOG_DIR = DATA / "logs"
SCAN_DIR = DATA / "scan"
PLAIN_DIR = DATA / "plain"
STATE_PATH = DATA / "manager.json"

for d in (DATA, LOG_DIR, SCAN_DIR, PLAIN_DIR):
    d.mkdir(parents=True, exist_ok=True)


def run(cmd: list[str], timeout: float = 30, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


async def arun(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(run, cmd, timeout)


# ---------------------------------------------------------------- 持久状态
DEFAULT_STATE = {
    "qq_watchdog": True,       # QQ 进程退出后自动重新拉起（由 supervisor 负责，此处留标记）
    "qq": {"uin": "", "db": "", "key_variant": ""},
    "metrics": {"key_extracts": 0, "messages_forwarded": 0, "since": 0},
}


def load_state() -> dict:
    st = json.loads(json.dumps(DEFAULT_STATE))
    if STATE_PATH.exists():
        try:
            saved = json.loads(STATE_PATH.read_text("utf-8"))
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(st.get(k), dict):
                    st[k].update(v)
                else:
                    st[k] = v
        except Exception as e:  # noqa: BLE001
            log.warning("读取 manager.json 失败：%s", e)
    return st


STATE = load_state()


def save_state():
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


def tail(path: Path, lines: int = 300) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])
