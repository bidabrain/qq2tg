"""在 QQ 本地 nt_data 目录里按 md5 定位媒体文件（明文，无需解密）。

QQ 媒体明文存在 nt_qq_*/nt_data/ 下，按 md5 命名：
  图片  Pic/<月>/Ori/<md5>.jpg        原图（点开后才下载）
        Pic/<月>/Thumb/<md5>_720.jpg  中等分辨率缩略图（展示即有，质量够用）
        Pic/<md>/Thumb/<md5>_0.jpg    小缩略图
  视频  Video/<月>/Ori/<md5>.mp4      原视频（下载后才有）
        Video/<月>/Thumb/<md5>_0.jpg  视频封面
  表情  Emoji/.../Ori/<md5>.*

原图/原视频可能尚未下载（QQ 点开才下）；此时图片回退到 720/缩略图，视频回退到封面。
"""
from __future__ import annotations

import glob
import os

try:
    from . import qqkey
except ImportError:
    import qqkey


def nt_data_dir(home: str) -> str | None:
    db = qqkey.qq_db_path(home)
    if not db:
        return None
    # db 在 nt_qq_*/nt_db/nt_msg.db，媒体在 nt_qq_*/nt_data
    root = os.path.dirname(os.path.dirname(db))
    d = os.path.join(root, "nt_data")
    return d if os.path.isdir(d) else None


def _first(patterns: list[str]) -> str | None:
    for p in patterns:
        hits = glob.glob(p)
        if hits:
            # 多个时取最大的（原图/高清优先）
            return max(hits, key=lambda f: os.path.getsize(f) if os.path.exists(f) else 0)
    return None


def find_image(home: str, md5: str) -> str | None:
    d = nt_data_dir(home)
    if not d or not md5:
        return None
    m = md5.lower()
    return _first([
        f"{d}/Pic/*/Ori/{m}.*",
        f"{d}/Emoji/*/*/Ori/{m}.*",
        f"{d}/Pic/*/Thumb/{m}_720.*",
        f"{d}/Pic/*/Thumb/{m}_0.*",
        f"{d}/Pic/*/Thumb/{m}_*.*",   # 其它尺寸缩略图兜底（_198/_198_x 等）
        f"{d}/Pic/*/*/{m}.*",         # 少见目录结构兜底
    ])


def find_video(home: str, md5: str) -> tuple[str | None, bool]:
    """返回 (路径, 是否为视频本体)；取不到视频本体时回退封面图，第二个值 False。"""
    d = nt_data_dir(home)
    if not d or not md5:
        return None, False
    m = md5.lower()
    ori = _first([f"{d}/Video/*/Ori/{m}.*"])
    if ori:
        return ori, True
    thumb = _first([f"{d}/Video/*/Thumb/{m}_0.*", f"{d}/Video/*/Thumb/{m}_*.*"])
    return thumb, False


def find_media(home: str, kind: str, md5: str):
    """统一入口。返回 (路径, 实际类型'image'/'video'/'photo')；取不到返回 (None,None)。
    视频本体取不到时回退封面，类型标为 'photo'。"""
    if kind == "image":
        p = find_image(home, md5)
        return (p, "image") if p else (None, None)
    if kind == "video":
        p, real = find_video(home, md5)
        if p:
            return (p, "video" if real else "photo")
    return None, None
