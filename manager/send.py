"""TG → QQ 回发（实验性）：用 xdotool 驱动 Linux QQ 界面发送文本。

说明：QQ(NTQQ) 是 Electron/CEF 应用，合成点击常不被识别，需用绝对坐标 mousemove + 普通
click（物理移动光标再点）。下面的坐标是针对 1280x800 窗口的占位值，**需在你的环境里按
实际 QQ 界面校准**（搜索框、结果项、输入框位置）。中文用剪贴板粘贴，xdotool type 不可靠。

默认 reply_enabled=False，此模块不参与转发主链路；开启后请先校准坐标再使用。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

DISPLAY = os.environ.get("DISPLAY", ":1")
WIN_W, WIN_H = 1280, 800
# —— 占位坐标，需按实际 QQ 界面校准 ——
SEARCH_XY = (150, 55)      # 顶部搜索框
RESULT_XY = (150, 130)     # 搜索下拉第一个结果
INPUT_XY = (760, 720)      # 聊天输入框
_LOCK = threading.Lock()


def _env() -> dict:
    return {**os.environ, "DISPLAY": DISPLAY}


def _x(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["xdotool", *args], capture_output=True, text=True, env=_env())


def _qq_win() -> str | None:
    r = _x("search", "--name", "QQ")
    best, best_area = None, 0
    for w in r.stdout.split():
        g = _x("getwindowgeometry", w).stdout
        for line in g.splitlines():
            if "Geometry" in line:
                try:
                    ww, hh = (int(x) for x in line.split(":")[1].strip().split("x"))
                    if ww * hh > best_area:
                        best, best_area = w, ww * hh
                except ValueError:
                    pass
    return best


def _normalize(win: str):
    _x("windowactivate", "--sync", win)
    _x("windowmove", win, "0", "0")
    _x("windowsize", win, str(WIN_W), str(WIN_H))
    time.sleep(0.2)


def _click(x: int, y: int):
    _x("mousemove", str(x), str(y))
    time.sleep(0.12)
    _x("click", "1")
    time.sleep(0.15)


def _clip(text: str):
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), env=_env())


def send_to_chat(chat_name: str, chat_id: str, text: str) -> dict:
    """搜索并打开会话后发送文本。坐标未校准时很可能发错，请先校准。"""
    with _LOCK:
        win = _qq_win()
        if not win:
            return {"ok": False, "msg": "找不到 QQ 窗口（是否已登录）"}
        _normalize(win)
        # 搜索
        _click(*SEARCH_XY)
        _x("key", "ctrl+a"); _x("key", "Delete")
        _clip(chat_name); time.sleep(0.1); _x("key", "ctrl+v")
        time.sleep(1.5)
        _click(*RESULT_XY)        # 打开首个结果（无标题核对，实验性）
        time.sleep(0.6)
        # 发送
        _click(*INPUT_XY)
        _clip(text); time.sleep(0.1); _x("key", "ctrl+v")
        time.sleep(0.3); _x("key", "Return")
        return {"ok": True, "msg": "已尝试发送（请在 QQ 侧确认，坐标可能需校准）"}
