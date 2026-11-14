"""QQ 消息采集：解密 nt_msg.db，解析 c2c/group 消息，监听新消息。

链路：
  私聊表 c2c_msg_table / 群聊表 group_msg_table，列名是 protobuf 字段号字符串。
  会话标识 chat_id = 列 "40021"（私聊=对端 uid，群聊=群号）。
  正文 列 "40800"（protobuf blob）经 msgparser 渲染为一行文本。

监听：轮询 nt_msg.db-wal 的 mtime/size，一有变化就重新解密并按 (表, msg_id) 去重，
产出新消息回调。时延下限取决于 QQ 自身落库节奏，而非轮询频率。

名称映射：好友备注/群名存在另外的库（profile/group_info），当前版本先用 QQ 号/群号
作为显示名，群内发送者用消息自带的群昵称(40093)/名片(40090)。可后续增量完善。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

try:
    from . import qqkey, msgparser
    from .names import NameResolver
except ImportError:  # 独立运行
    import qqkey
    import msgparser
    from names import NameResolver

PLAIN_DIR = os.environ.get("PLAIN_DIR", "/data/plain")

# 顶层行列（按此顺序 SELECT，用下标取）
COLS = ["40001", "40050", "40013", "40011", "40012",
        "40020", "40033", "40021", "40030", "40090", "40093", "40800"]
C_MSGID, C_TIME, C_DIR, C_TYPE, C_SUBTYPE, \
    C_SUID, C_SQQ, C_PEERUID, C_PEERQQ, C_CARD, C_GNICK, C_BODY = range(len(COLS))

TABLES = {"c2c_msg_table": False, "group_msg_table": True}


@dataclass
class Message:
    chat_id: str                 # 会话标识（对端 uid 或群号）——稳定路由键
    chat_name: str               # 会话显示名（好友昵称/群名，取不到则号码）
    chat_number: str             # 对端 QQ号 / 群号——发送时按它搜索定位
    is_group: bool
    sender_id: str               # 发送者 uid
    sender_name: str             # 发送者显示名
    type: int                    # 首段 content_type
    text: str                    # 渲染后的一行文本
    time: int                    # 秒级时间戳
    msg_id: int
    table: str
    is_self: bool = False        # 是否自己发出（用于避免回声）
    media: str | None = None     # 'image' / 'video' / None
    media_md5: str = ""          # 媒体 md5（在 nt_data 本地文件按它定位）


class Collector:
    def __init__(self, home: str, on_message=None):
        self.home = home
        self.db_path = qqkey.qq_db_path(home)
        self.on_message = on_message
        self.cred: dict | None = None
        self.names = NameResolver(home)
        os.makedirs(PLAIN_DIR, exist_ok=True)

    # -------------------------------------------------------------- 密钥
    def ready(self) -> bool:
        if not self.db_path:
            self.db_path = qqkey.qq_db_path(self.home)
        return bool(self.db_path and qqkey.find_qq_pid())

    def refresh_key(self) -> bool:
        if not self.db_path:
            self.db_path = qqkey.qq_db_path(self.home)
        if not self.db_path:
            return False
        if self.cred:
            # 快速校验现有密钥还能打开
            con = qqkey.open_decrypted(self.db_path, self.cred, PLAIN_DIR)
            if con:
                con.close()
                return True
        cred = qqkey.find_working_key(self.db_path, PLAIN_DIR)
        if cred:
            self.cred = cred
        return bool(self.cred)

    # -------------------------------------------------------------- 快照
    def _open(self):
        if not self.cred and not self.refresh_key():
            return None
        return qqkey.open_decrypted(self.db_path, self.cred, PLAIN_DIR)

    def snapshot(self) -> dict[tuple[str, int], Message]:
        """返回 {(表, msg_id): Message}，覆盖私聊 + 群聊。"""
        con = self._open()
        if not con:
            return {}
        msgs: dict[tuple[str, int], Message] = {}
        try:
            existing = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table, is_group in TABLES.items():
                if table not in existing:
                    continue
                sel = ",".join(f'"{c}"' for c in COLS)
                try:
                    cur = con.execute(f"SELECT {sel} FROM {table}")
                except Exception:  # noqa: BLE001  列缺失等
                    continue
                for r in cur:
                    m = self._row_to_msg(table, is_group, r)
                    if m:
                        msgs[(table, m.msg_id)] = m
        finally:
            con.close()
        return msgs

    def _row_to_msg(self, table, is_group, r) -> Message | None:
        msg_id = r[C_MSGID] or 0
        blob = r[C_BODY]
        text, ct0 = ("", 0)
        media = None
        media_md5 = ""
        if isinstance(blob, (bytes, bytearray)) and blob:
            text, ct0 = msgparser.render_body(bytes(blob))
            mi = msgparser.first_media(bytes(blob))
            if mi:
                media = mi["kind"]
                media_md5 = mi.get("md5") or ""
        chat_id = str(r[C_PEERUID] or r[C_PEERQQ] or "")
        if not chat_id:
            return None
        peer_qq = r[C_PEERQQ]
        # 发送定位用的号码：群=群号(chat_id)，私聊=对端 QQ 号
        number = str(chat_id if is_group else (peer_qq or chat_id))
        # 显示名：群名 / 好友昵称；取不到回退号码
        chat_name = self.names.display(chat_id, is_group, number) or number
        sender_id = str(r[C_SUID] or "")
        if is_group:
            sender_name = (r[C_GNICK] or r[C_CARD] or str(r[C_SQQ] or "") or sender_id)
        else:
            sender_name = chat_name
        is_self = (r[C_DIR] == 1)      # 方向：1=自己发出，0=收到（实测校准）
        return Message(
            chat_id=chat_id, chat_name=chat_name, chat_number=number, is_group=is_group,
            sender_id=sender_id, sender_name=str(sender_name),
            type=ct0, text=text, time=r[C_TIME] or 0, msg_id=msg_id,
            table=table, is_self=is_self, media=media, media_md5=media_md5,
        )

    # -------------------------------------------------------------- 会话列表
    def list_sessions(self, keyword: str = "", limit: int = 40) -> list[dict]:
        """按最近消息列出会话 [{chat_id, name, is_group}]。"""
        snap = self.snapshot()
        latest: dict[str, Message] = {}
        for m in snap.values():
            cur = latest.get(m.chat_id)
            if not cur or m.time > cur.time:
                latest[m.chat_id] = m
        rows = sorted(latest.values(), key=lambda m: m.time, reverse=True)
        kw = keyword.strip().lower()
        out = []
        for m in rows:
            if kw and kw not in m.chat_name.lower() and kw not in m.chat_id.lower():
                continue
            out.append({"chat_id": m.chat_id, "name": m.chat_name,
                        "number": m.chat_number, "is_group": m.is_group})
            if len(out) >= limit:
                break
        return out

    # -------------------------------------------------------------- 监听
    def _wal_sig(self):
        for suf in ("-wal", ""):
            try:
                st = os.stat(self.db_path + suf)
                return (st.st_mtime, st.st_size)
            except OSError:
                continue
        return (0, 0)

    def poll_loop(self, interval: float = 1.0):
        """建立基线后持续监听，新消息经 on_message 回调。阻塞运行。"""
        if not self.refresh_key():
            raise RuntimeError("未取到密钥，QQ 可能未登录")
        prev = set(self.snapshot().keys())
        sig = self._wal_sig()
        while True:
            cur_sig = self._wal_sig()
            if cur_sig != sig:
                sig = cur_sig
                cur = self.snapshot()
                new = [k for k in cur if k not in prev]
                for k in sorted(new, key=lambda k: cur[k].time):
                    if self.on_message:
                        try:
                            self.on_message(cur[k])
                        except Exception as e:  # noqa: BLE001
                            print("on_message 异常:", e, flush=True)
                prev = set(cur.keys())
            time.sleep(interval)


if __name__ == "__main__":  # 自测：python -m collector /data/home
    import datetime
    import sys

    home = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("QQ_HOME", "/data/home")
    col = Collector(home)
    if not col.refresh_key():
        print("未取到密钥（QQ 是否已登录？SYS_PTRACE？）")
        raise SystemExit(1)
    snap = col.snapshot()
    rows = sorted(snap.values(), key=lambda m: m.time, reverse=True)[:15]
    print(f"消息库共 {len(snap)} 条，最近 15 条：")
    for m in rows:
        t = datetime.datetime.fromtimestamp(m.time).strftime("%m-%d %H:%M") if m.time else "?"
        who = m.chat_name + (f"/{m.sender_name}" if m.is_group else "")
        print(f"{t} [{who}] {m.text[:50]}")
