"""TG → QQ 回发：用 xdotool 驱动 Linux QQ 界面，按 QQ 号/群号搜索并发送。

为什么按号码搜而不是按名字：QQ 号/群号唯一，不像昵称会重名；搜索下拉的结果行里
会直接带上该号码（联系人如「昵称(10001)」、群如「群名(5人) 包含:10001」），用 OCR
核对结果里确实含我们要发的号码，再点开——搜不到就中止，避免误发到别的会话。
（tencent:// 深链在 NTQQ 上实测不可用，故仍走 UI 自动化。）

坐标按 1000x720 窗口(规整到 0,0)校准（NTQQ 3.2.35 Linux）。换版本/分辨率可能需微调，
常量集中在下方 _COORDS 区，便于重新校准。QQ 是 CEF 应用，合成点击不被识别，必须用
绝对坐标 mousemove + 普通 click。中文用剪贴板粘贴，xdotool type 不能可靠输入 CJK。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time

DISPLAY = os.environ.get("DISPLAY", ":1")

# ----------------------------------------------------- 坐标（1000x720 窗口，原点 0,0）
WIN_W, WIN_H = 1000, 720
SEARCH_XY = (163, 66)         # 顶部搜索框
RESULT_XY = (200, 157)        # 搜索下拉第一个结果行
RESULT_REGION = (74, 128, 300, 64)   # 第一个结果行区域（OCR 核对号码用）：x,y,w,h
INPUT_XY = (650, 600)         # 聊天输入框空白区
TITLE_REGION = (322, 54, 240, 30)    # 会话标题栏（确认已打开会话）
_LOCK = threading.Lock()


def _env() -> dict:
    return {**os.environ, "DISPLAY": DISPLAY}


def _x(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["xdotool", *args], capture_output=True, text=True, env=_env())


def _qq_win() -> str | None:
    """主窗口（标题含 QQ，几何面积最大的那个）。"""
    r = _x("search", "--name", "QQ")
    best, best_area = None, 0
    for w in r.stdout.split():
        g = _x("getwindowgeometry", w).stdout
        for line in g.splitlines():
            if "Geometry" in line:
                m = re.search(r"(\d+)x(\d+)", line)
                if m:
                    a = int(m.group(1)) * int(m.group(2))
                    if a > best_area:
                        best, best_area = w, a
    return best


def _normalize(win: str):
    _x("windowactivate", "--sync", win)
    _x("windowmove", win, "0", "0")
    _x("windowsize", win, str(WIN_W), str(WIN_H))
    time.sleep(0.25)


def _click(x: int, y: int):
    """绝对坐标：物理移动光标再点（CEF 认这种，不认 click --window）。"""
    _x("mousemove", str(x), str(y))
    time.sleep(0.12)
    _x("click", "1")
    time.sleep(0.15)


def _clip(text: str):
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), env=_env())


def _ocr(region) -> str:
    """OCR 指定区域（数字+中文），返回全文（去空白）。"""
    fd, png = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        if subprocess.run(["scrot", "-a", ",".join(map(str, region)), "-o", png],
                          capture_output=True, env=_env()).returncode != 0:
            return ""
        out = subprocess.run(["tesseract", png, "-", "-l", "chi_sim+eng"],
                             capture_output=True, text=True, env=_env())
        return re.sub(r"\s+", "", out.stdout)
    finally:
        try:
            os.remove(png)
        except OSError:
            pass


def _search(number: str):
    _click(*SEARCH_XY)
    _x("key", "ctrl+a")
    _x("key", "Delete")
    _clip(number)
    time.sleep(0.1)
    _x("key", "ctrl+v")
    time.sleep(1.6)                 # 等下拉结果稳定


def _open_by_number(number: str) -> bool:
    """搜号码 → OCR 核对第一个结果含该号码 → 点开。核对不过返回 False，绝不乱点。"""
    _search(number)
    txt = _ocr(RESULT_REGION)
    digits = re.sub(r"\D", "", number)
    if not digits or digits not in re.sub(r"\D", "", txt):
        return False                # 结果里没这个号码（搜不到/已退群）→ 不点，避免误发
    _click(*RESULT_XY)
    time.sleep(0.8)
    return bool(_ocr(TITLE_REGION))  # 标题栏有内容 = 会话已打开


def send_to_open(text: str) -> dict:
    """向当前已打开的会话发送 text。"""
    _click(*INPUT_XY)
    _clip(text)
    time.sleep(0.1)
    _x("key", "ctrl+v")
    time.sleep(0.3)
    _x("key", "Return")
    return {"ok": True, "msg": "已发送到当前会话"}


def send_to_chat(chat_name: str, chat_id: str, text: str) -> dict:
    """chat_name 为 QQ 号/群号（采集层已把会话显示名设为号码）。
    搜号码并核对后发送；核对不过绝不发送，避免误发。"""
    number = re.sub(r"\D", "", str(chat_name)) or str(chat_id)
    with _LOCK:
        win = _qq_win()
        if not win:
            return {"ok": False, "step": "find_window", "msg": "找不到 QQ 窗口（是否已登录）"}
        _normalize(win)
        if not _open_by_number(number):
            return {"ok": False, "step": "verify",
                    "msg": f"未能按号码「{number}」定位会话（搜索无匹配，已避免误发）"}
        return send_to_open(text)


if __name__ == "__main__":  # 自测：python send.py <QQ号/群号> "消息"
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else ""
    msg = sys.argv[2] if len(sys.argv) > 2 else f"qq2tg send 测试 {time.strftime('%H:%M:%S')}"
    print(send_to_chat(tgt, tgt, msg))
