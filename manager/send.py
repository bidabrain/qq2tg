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
FILE_BTN_XY = (409, 535)      # 底部工具栏「发送文件」(文件夹图标)
IMAGE_BTN_XY = (441, 535)     # 底部工具栏「发送图片」(图片图标)
CONFIRM_SEND_XY = (500, 509)  # QQ「发送给 X」确认框里的「发送(1)」按钮(居中)
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
    """把窗口摆到 (0,0) 1000x720。幂等：已就位则不再 resize/activate——
    Xvfb 下反复 resize/activate CEF 窗口会间歇性黑屏不重绘，故尽量少动。"""
    g = _x("getwindowgeometry", win).stdout
    if "Position: 0,0" in g and f"{WIN_W}x{WIN_H}" in g:
        _x("windowraise", win)     # 已就位，仅抬到最前（不 activate --sync，避免黑屏）
        time.sleep(0.15)
        return
    _x("windowmove", win, "0", "0")
    _x("windowsize", win, str(WIN_W), str(WIN_H))
    time.sleep(0.5)


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


def _txtnorm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


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


DROPDOWN_REGION = (56, 95, 310, 300)   # 搜索下拉结果区（x,y,w,h @1000x720），用于确认下拉是否关闭


def _open_by_name(name: str) -> bool:
    """搜群名/昵称 → 点下拉第一条结果 → 确认会话已打开。
    用于号码搜不到的会话（如设了"禁止群号搜索"的群/好友）。
    「联系人」「群聊」分区永远排在「文件/聊天记录/搜索综合结果」之前，所以**下拉第一条
    结果必定是联系人或群聊**（不会是文件/聊天记录）——直接点第一条即满足"只点联系人/群聊"。"""
    g = _txtnorm(name)
    if len(g) < 2:
        return False
    _search(name)
    _click(*RESULT_XY)                       # 第一条结果 = 联系人/群聊（排在文件/记录之前）
    time.sleep(1.2)                          # 等下拉关闭 + 会话渲染
    drop = _ocr(DROPDOWN_REGION)
    if "综合" in drop or "联系人" in drop or "群聊" in drop:
        return False                         # 下拉还在 = 没点中结果（名字无匹配会话）
    return bool(_txtnorm(_ocr(TITLE_REGION)))  # 标题栏非空 = 会话已打开


def send_to_open(text: str) -> dict:
    """向当前已打开的会话发送 text。"""
    _click(*INPUT_XY)
    _clip(text)
    time.sleep(0.1)
    _x("key", "ctrl+v")
    time.sleep(0.3)
    _x("key", "Return")
    return {"ok": True, "msg": "已发送到当前会话"}


def _open_target(number: str, name: str = "") -> dict | None:
    """规整窗口 + 定位会话：先按号码搜，搜不到再按群名/昵称兜底（如禁群号搜索的群）。
    成功返回 None，失败返回错误 dict。核对不过绝不发送，避免误发。"""
    win = _qq_win()
    if not win:
        return {"ok": False, "step": "find_window", "msg": "找不到 QQ 窗口（是否已登录）"}
    _normalize(win)
    if _open_by_number(number):
        return None
    if name and _open_by_name(name):        # 号码搜不到 → 按名字兜底
        return None
    hint = f"号码「{number}」" + (f"/名字「{name}」" if name else "")
    return {"ok": False, "step": "verify", "msg": f"未能定位会话（{hint} 均搜索无匹配，已避免误发）"}


def send_to_chat(number: str, chat_id: str, text: str, name: str = "") -> dict:
    """number=QQ号/群号；name=群名/昵称（号码搜不到时兜底）。核对不过绝不发送。"""
    num = re.sub(r"\D", "", str(number)) or str(chat_id)
    with _LOCK:
        err = _open_target(num, name)
        if err:
            return err
        return send_to_open(text)


def _file_dialog() -> bool:
    """等「选择文件/打开」对话框出现，最多 ~3s。"""
    for _ in range(10):
        if _x("search", "--name", "选择文件").stdout.strip() or \
           _x("search", "--name", "打开").stdout.strip():
            return True
        time.sleep(0.3)
    return False


def _pick_and_send(btn_xy, path: str, finalize: str) -> dict:
    """点工具栏按钮(图片/文件)→ 文件选择器用 Ctrl+L 输绝对路径 → 发送。
    不走剪贴板(剪贴板粘贴图片会让 QQ 在 Xvfb 下黑屏/不稳)。
    finalize: 'dialog'=文件会弹「发送给X」确认框，点它的发送；'enter'=图片进输入框，回车发。"""
    _click(*btn_xy)
    if not _file_dialog():
        return {"ok": False, "step": "dialog", "msg": "未弹出文件选择对话框"}
    _x("key", "ctrl+l")              # GTK 文件选择器：打开路径输入栏
    time.sleep(0.2)
    _clip(path)                      # 仅复制文本路径（非图片），安全
    _x("key", "ctrl+v")
    time.sleep(0.2)
    _x("key", "Return")              # 选中文件
    time.sleep(1.2)
    if finalize == "dialog":
        _click(*CONFIRM_SEND_XY)     # 文件：点「发送给 X」确认框里的「发送(1)」
    else:
        _x("key", "Return")          # 图片：已进输入框，回车发送
    time.sleep(0.6)
    return {"ok": True, "msg": "已发送"}


def send_image_to_chat(number: str, chat_id: str, image_path: str, name: str = "") -> dict:
    """定位会话 → 「发送图片」按钮选图发送（不走剪贴板）。核对不过不发。"""
    num = re.sub(r"\D", "", str(number)) or str(chat_id)
    with _LOCK:
        err = _open_target(num, name)
        if err:
            return err
        r = _pick_and_send(IMAGE_BTN_XY, image_path, finalize="enter")
        return {**r, "msg": "已发送图片"} if r.get("ok") else r


def send_file_to_chat(number: str, chat_id: str, file_path: str, name: str = "") -> dict:
    """定位会话 → 「发送文件」选文件发送（.mp4 以视频形式）。核对不过不发。"""
    num = re.sub(r"\D", "", str(number)) or str(chat_id)
    with _LOCK:
        err = _open_target(num, name)
        if err:
            return err
        r = _pick_and_send(FILE_BTN_XY, file_path, finalize="dialog")
        return {**r, "msg": "已发送文件"} if r.get("ok") else r


if __name__ == "__main__":  # 自测：python send.py <QQ号/群号> "消息"
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else ""
    msg = sys.argv[2] if len(sys.argv) > 2 else f"qq2tg send 测试 {time.strftime('%H:%M:%S')}"
    print(send_to_chat(tgt, tgt, msg))
