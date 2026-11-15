"""会话显示名解析：把 QQ号/群号/uid 映射成好友昵称(备注优先)/群名。

来源（各库有各自独立密钥，按文件头 salt 匹配内存候选取钥匙）：
  群名   group_info.db   → 表 group_list：列 60001=群号, 60007=群名
  昵称   profile_info.db → 表 profile_info_v6：列 1000=uid, 1002=QQ号, 20002=昵称
  备注   profile_info.db → 表 buddy_list：列 1000=uid, 25007=好友备注(优先于昵称)

带 TTL 缓存；/synctopic 可强制刷新。昵称/群名会变，故支持手动重同步。
"""
from __future__ import annotations

import os
import time

try:
    from . import qqkey
except ImportError:
    import qqkey

PLAIN_DIR = os.environ.get("PLAIN_DIR", "/data/plain")


class NameResolver:
    def __init__(self, home: str):
        self.home = home
        self._group: dict[str, str] = {}      # 群号 -> 群名
        self._user: dict[str, str] = {}        # uid 或 QQ号 -> 昵称/备注
        self._ts = 0.0

    def _db_dir(self) -> str | None:
        return qqkey.qq_db_dir(self.home)

    def refresh(self) -> bool:
        """重新解密 group_info / profile_info，重建映射。返回是否成功。"""
        ddir = self._db_dir()
        if not ddir:
            return False
        ok = False
        # 群名
        gi = os.path.join(ddir, "group_info.db")
        if os.path.exists(gi):
            con = qqkey.open_db(gi, PLAIN_DIR)
            if con:
                try:
                    for num, nm in con.execute('SELECT "60001","60007" FROM group_list'):
                        if num and nm:
                            self._group[str(num)] = str(nm)
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
                con.close()
        # 昵称 + 备注
        pi = os.path.join(ddir, "profile_info.db")
        if os.path.exists(pi):
            con = qqkey.open_db(pi, PLAIN_DIR)
            if con:
                try:
                    for uid, qq, nick in con.execute(
                            'SELECT "1000","1002","20002" FROM profile_info_v6'):
                        if not nick:
                            continue
                        if uid:
                            self._user[str(uid)] = str(nick)
                        if qq:
                            self._user[str(qq)] = str(nick)
                    ok = True
                except Exception:  # noqa: BLE001
                    pass
                # 好友备注覆盖昵称（更贴近用户习惯）
                try:
                    for uid, remark in con.execute('SELECT "1000","25007" FROM buddy_list'):
                        if uid and remark:
                            self._user[str(uid)] = str(remark)
                except Exception:  # noqa: BLE001
                    pass
                con.close()
        if ok:
            self._ts = time.time()
        return ok

    def ensure(self, ttl: float = 600):
        if time.time() - self._ts > ttl or (not self._group and not self._user):
            self.refresh()

    def group_name(self, number: str) -> str | None:
        self.ensure()
        return self._group.get(str(number))

    def all_groups(self) -> dict[str, str]:
        """所有群：群号 -> 群名。用于 /mute 菜单列出全部群。"""
        self.ensure()
        return dict(self._group)

    def user_name(self, uid: str = "", qq: str = "") -> str | None:
        self.ensure()
        return self._user.get(str(uid)) or (self._user.get(str(qq)) if qq else None)

    def display(self, chat_id: str, is_group: bool, number: str) -> str | None:
        """会话显示名：群→群名；私聊→好友备注/昵称。取不到返回 None（调用方回退号码）。"""
        if is_group:
            return self.group_name(number) or self.group_name(chat_id)
        return self.user_name(uid=chat_id, qq=number)
