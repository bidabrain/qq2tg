"""NTQQ 消息解析：c2c_msg_table / group_msg_table 行 -> 可读文本。

- 列名是数字字符串（protobuf 字段号）。正文在列 "40800"（protobuf blob）。
- 顶层行字段：40001 msg_id / 40050 时间 / 40013 方向 / 40011 主类型 / 40012 子类型 /
  40020 发送者 uid / 40033 发送者 QQ / 40021 会话 id(uid 或群号) / 40030 会话 QQ /
  40090 发送者名片名 / 40093 群昵称 / 40800 正文 blob。
- 不依赖 protobuf 运行库，内置极简 wire 解码器。

改编自 github.com/ZEntrosein/qq-nt-export（MIT）的 ntqq_msg_parser.py。
"""
from __future__ import annotations

import json


# ─────────────── 极简 protobuf wire 解码 ───────────────
def _read_varint(buf, i):
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def decode_fields(buf):
    out = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
            if v >= 1 << 63:
                v -= 1 << 64
        elif wt == 1:
            v = buf[i:i + 8]; i += 8
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            v = buf[i:i + ln]; i += ln
        elif wt == 5:
            v = buf[i:i + 4]; i += 4
        else:
            raise ValueError("unsupported wire type %d" % wt)
        out.setdefault(fn, []).append((wt, v))
    return out


def _i(fields, no, default=None):
    for wt, v in fields.get(no, ()):
        if wt == 0:
            return v
    return default


def _s(fields, no, default=None):
    for wt, v in fields.get(no, ()):
        if wt == 2:
            return v.decode("utf-8", errors="replace")
    return default


def _b(fields, no, default=None):
    for wt, v in fields.get(no, ()):
        if wt == 2:
            return v
    return default


def _bs(fields, no):
    return [v for wt, v in fields.get(no, ()) if wt == 2]


# ─────────────── 40800 = MsgBody{repeated MsgContent} ───────────────
def parse_content(raw):
    f = decode_fields(raw)
    c = {
        "content_type": _i(f, 45002, 0),
        "media_sub": _i(f, 45003),
        "text": _s(f, 45101),
        "f45105": _s(f, 45105),
        "filename": _s(f, 45402),
        "filesize": _i(f, 45405),
        "md5_hex": (_b(f, 45406) or b"").hex() or None,
        "img_width": _i(f, 45411),
        "img_height": _i(f, 45412),
        "f45415": _i(f, 45415),
        "file_ext": _s(f, 45419),
        "file_uuid": _s(f, 45503),
        "sticker_hex": (_b(f, 45600) or b"").hex() or None,
        "cdn_url": _s(f, 45802) or _s(f, 45803) or _s(f, 45804),
        "local_path": _s(f, 45812),
        "text_fallback": [v.decode("utf-8", errors="replace") for v in _bs(f, 45815)],
        "reply_msg_id": _i(f, 47401),
        "reply_msg_seq": _i(f, 47402),
        "reply_summary": _s(f, 47413),
        "nc_uid_1": _s(f, 47703),
        "nc_nickname_1": _s(f, 47705),
        "nc_remark_1": _s(f, 47706),
        "nc_nickname_2": _s(f, 47707),
        "nc_extra_text": _s(f, 47713),
        "video_flag": _i(f, 47601),
        "video_text": _s(f, 47602),
        "fwd_meta": _s(f, 47901),
        "call_type": _i(f, 48151, 0),
        "call_duration": _i(f, 48152, 0),
        "call_desc": _s(f, 48153),
        "sys_content": _s(f, 80900),
        "rich_text": _s(f, 48705),
    }
    ref = _b(f, 47710)
    c["ref_msg"] = parse_content(ref) if ref else None
    return c


def parse_40800(blob):
    """解析 40800 blob，返回 content 段列表。"""
    try:
        fields = decode_fields(blob)
    except Exception:  # noqa: BLE001
        return []
    return [parse_content(v) for wt, v in fields.get(40800, ()) if wt == 2]


# ─────────────── 段 -> 单行预览 ───────────────
def _seg_preview(c):
    ct = c["content_type"]
    if ct == 1 and (c["text"] or ""):
        return c["text"]
    if ct == 2:
        if (c["img_width"] or 0) > 0 or (c["img_height"] or 0) > 0:
            fb = (c["text_fallback"] or [None])[0]
            return "[图片]" + (f" {fb}" if fb else "")
        if (c["f45415"] or 0) > 0 or (c["video_flag"] or 0) > 0:
            return "[视频]"
        if c["sticker_hex"]:
            return "[表情]"
        return c["text"] or "[媒体]"
    if ct == 3:
        return f"[文件] {c['filename'] or ''}".rstrip()
    if ct == 5 or c["sticker_hex"]:
        return "[表情]"
    if c["reply_msg_id"] or c["reply_msg_seq"]:
        return f"[引用] {c['text'] or c['reply_summary'] or ''}".rstrip()
    if c["call_desc"]:
        return f"[通话] {c['call_desc']}"
    if c["nc_uid_1"]:
        name = c["nc_nickname_1"] or c["nc_remark_1"] or c["nc_uid_1"]
        return f"[名片] {name}"
    if c["fwd_meta"]:
        return "[合并转发]"
    if c["sys_content"]:
        return f"[系统] {c['sys_content']}"
    if c["rich_text"]:
        return f"[富媒体] {c['rich_text']}"
    if c["text"]:
        return c["text"]
    return ""


def render_body(blob) -> tuple[str, int]:
    """把 40800 blob 渲染成一行文本，返回 (文本, 首段 content_type)。"""
    segs = parse_40800(blob)
    if not segs:
        return "", 0
    parts = [p for p in (_seg_preview(c) for c in segs) if p]
    text = " ".join(parts).strip()
    ct0 = segs[0]["content_type"]
    # 粗分类：是否含图片/视频媒体（供桥接决定抓取媒体）
    return text or "[空消息]", ct0


def media_kind(blob) -> str | None:
    """判断消息主媒体类型：'image' / 'video' / None。供桥接抓取用。"""
    for c in parse_40800(blob):
        ct = c["content_type"]
        if ct == 2:
            if (c["img_width"] or 0) > 0 or (c["img_height"] or 0) > 0:
                return "image"
            if (c["f45415"] or 0) > 0 or (c["video_flag"] or 0) > 0:
                return "video"
        if ct == 9 or (ct == 2 and (c["video_flag"] or 0) > 0):
            return "video"
    return None
