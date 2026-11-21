"""QQ NTQQ(Linux 桌面版) 数据库密钥提取与解密。

背景：
  NTQQ 的聊天库 nt_msg.db 用 SQLCipher 加密，但在标准 SQLCipher 数据区前多了一段
  固定 1024 字节自定义头，必须先剥掉。参数为非默认组合：
    page_size=4096, kdf_iter=4000, cipher_kdf_algorithm=PBKDF2_HMAC_SHA512,
    cipher_hmac_algorithm=HMAC_SHA1, aes-256-cbc。

取密钥（只读，不注入/不改 QQ 文件）：
  登录后 SQLCipher 凭据以特征串 x'<64hex key><32hex salt>' 形式驻留 QQ 进程内存；
  也可能以 16 字节 ASCII 口令形式出现（经 KDF 派生）。本模块遍历 /proc/<pid>/mem 的
  可读区做正则匹配，收集候选，再用各候选逐一尝试打开剥头后的库，能通过 integrity_check
  的即为本库密钥。需要 CAP_SYS_PTRACE。

参考：github.com/QQBackup/qq-win-db-key（Linux 取密钥）、
      github.com/ZEntrosein/qq-nt-export（内存扫描 + 多候选解密形态）。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
from pathlib import Path

HEADER_SIZE = 1024
PAGE = 4096
QQ_PROC_CANDS = ("/opt/QQ/qq", "/usr/bin/qq")

# 内存中的 SQLCipher 凭据特征
PAT_FULL = re.compile(rb"x'([0-9a-fA-F]{64})([0-9a-fA-F]{32})'")   # key(32B) + salt(16B)
PAT_KEYONLY = re.compile(rb"x'([0-9a-fA-F]{32})'")                 # 纯 key(16B hex)
PAT_ASCII16 = re.compile(rb"[\x21-\x7e]{16}")                      # 16 字节可见 ASCII 口令候选

# SQLCipher 打开形态（候选密钥 kind -> 该 kind 适用的 PRAGMA 序列）
_SHA1_PARAMS = ["PRAGMA cipher_hmac_algorithm = HMAC_SHA1",
                "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512",
                "PRAGMA kdf_iter = 4000"]


# ---------------------------------------------------------------- 进程 / 路径
def find_qq_pid() -> int | None:
    """遍历 /proc 找 QQ 主进程 pid（纯文件操作，不起子进程）。"""
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    for d in pids:
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "ignore")
        except OSError:
            continue
        if argv0 in QQ_PROC_CANDS or os.path.basename(argv0) == "qq":
            return int(d)
    return None


def qq_db_path(home: str) -> str | None:
    """定位 nt_msg.db（home/.config/QQ/**/nt_db/nt_msg.db）。"""
    for pat in (f"{home}/.config/QQ/**/nt_db/nt_msg.db",
                f"{home}/.config/QQ/**/nt_msg.db"):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return None


def qq_db_dir(home: str) -> str | None:
    db = qq_db_path(home)
    return os.path.dirname(db) if db else None


def read_salt(db_path: str) -> str:
    """剥 1024 字节头后 page1 前 16 字节即 SQLCipher salt。"""
    with open(db_path, "rb") as f:
        f.seek(HEADER_SIZE)
        return f.read(16).hex()


# ---------------------------------------------------------------- 内存扫描
def scan_memory_candidates(pid: int, salt_hex: str) -> list[dict]:
    """遍历 /proc/<pid>/mem 可读区，收集密钥候选。返回 [{kind,key,salt?,salt_match}]。"""
    cands: list[dict] = []
    seen: set[str] = set()
    try:
        mem = open(f"/proc/{pid}/mem", "rb", 0)
    except OSError:
        return cands
    with mem:
        try:
            maps = open(f"/proc/{pid}/maps")
        except OSError:
            return cands
        with maps:
            for line in maps:
                parts = line.split()
                if len(parts) < 2 or parts[1][0] != "r":
                    continue
                try:
                    a, b = (int(x, 16) for x in parts[0].split("-"))
                except ValueError:
                    continue
                if b - a >= 1 << 30:            # 跳过超大映射
                    continue
                try:
                    mem.seek(a)
                    buf = mem.read(b - a)
                except (OSError, OverflowError, ValueError):
                    continue
                for m in PAT_FULL.finditer(buf):
                    key, salt = m.group(1).decode().lower(), m.group(2).decode().lower()
                    tag = "F:" + key + salt
                    if tag not in seen:
                        seen.add(tag)
                        cands.append({"kind": "key+salt", "key": key, "salt": salt,
                                      "salt_match": salt == salt_hex})
                for m in PAT_KEYONLY.finditer(buf):
                    key = m.group(1).decode().lower()
                    tag = "K:" + key
                    if tag not in seen:
                        seen.add(tag)
                        cands.append({"kind": "key_only", "key": key, "salt": None,
                                      "salt_match": False})
    # 优先 salt 对得上的
    cands.sort(key=lambda c: 0 if c.get("salt_match") else 1)
    return cands


def scan_ascii16(pid: int, limit: int = 2000) -> list[str]:
    """收集 16 字节可见 ASCII 口令候选（KDF 形态）。较多，故限量。"""
    out: list[str] = []
    seen: set[str] = set()
    try:
        mem = open(f"/proc/{pid}/mem", "rb", 0)
        maps = open(f"/proc/{pid}/maps")
    except OSError:
        return out
    with mem, maps:
        for line in maps:
            parts = line.split()
            if len(parts) < 2 or not parts[1].startswith("rw"):   # 口令多在可写堆
                continue
            try:
                a, b = (int(x, 16) for x in parts[0].split("-"))
            except ValueError:
                continue
            if b - a >= 1 << 30:
                continue
            try:
                mem.seek(a)
                buf = mem.read(b - a)
            except (OSError, OverflowError, ValueError):
                continue
            for m in PAT_ASCII16.finditer(buf):
                s = m.group(0).decode("ascii", "ignore")
                if s not in seen:
                    seen.add(s)
                    out.append(s)
                    if len(out) >= limit:
                        return out
    return out


# ---------------------------------------------------------------- 解密
def strip_header(src: str, out: str) -> str:
    """把源库拷贝并剥掉前 1024 字节头 -> 标准 SQLCipher 数据区。"""
    with open(src, "rb") as fi, open(out, "wb") as fo:
        fi.seek(HEADER_SIZE)
        while True:
            b = fi.read(8 << 20)
            if not b:
                break
            fo.write(b)
    return out


def _variant_stmt(tag: str, key: str, salt: str | None):
    """返回某个解密形态的 (key_pragma, extras) 或 None。"""
    _HMAC_SHA1 = ["PRAGMA cipher_hmac_algorithm = HMAC_SHA1"]
    table = {
        "A_raw_key_salt": lambda: ('PRAGMA key = "x\'%s%s\'"' % (key, salt), []),
        "D_raw_key_salt_sha1": lambda: ('PRAGMA key = "x\'%s%s\'"' % (key, salt), _HMAC_SHA1),
        "B_raw_key": lambda: ('PRAGMA key = "x\'%s\'"' % key, []),
        "B_raw_key_sha1": lambda: ('PRAGMA key = "x\'%s\'"' % key, _HMAC_SHA1),
        "C_passphrase": lambda: ("PRAGMA key = '%s'" % key, _SHA1_PARAMS),
    }
    f = table.get(tag)
    return f() if f else None


def _variants_for_kind(kind: str) -> list[str]:
    if kind == "key+salt":
        return ["A_raw_key_salt", "D_raw_key_salt_sha1"]
    if kind == "key_only":
        return ["B_raw_key", "B_raw_key_sha1"]
    return ["C_passphrase"]


def _try_open(clear_db: str, cand: dict, only_variant: str | None = None):
    """尝试打开剥头库，返回 (con, variant) 或 (None, None)。
    only_variant 指定时只试该形态（已知密钥复用，避免每次重探产生失败日志）。"""
    import sqlcipher3.dbapi2 as sqlcipher
    key = cand["key"]
    salt = cand.get("salt")
    tags = [only_variant] if only_variant else _variants_for_kind(cand["kind"])
    for tag in tags:
        built = _variant_stmt(tag, key, salt)
        if not built:
            continue
        key_stmt, extras = built
        con = sqlcipher.connect(clear_db)
        try:
            cur = con.cursor()
            cur.execute("PRAGMA cipher_page_size = %d" % PAGE)
            cur.execute(key_stmt)
            for e in extras:
                cur.execute(e)
            # 密钥对则能读出表结构；密钥错会在这里抛 "file is not a database"。
            # 不用 integrity_check 校验：group_info/profile_info 用了自定义 FTS 分词器
            # (pinyin_letter)，integrity_check 会在那些 FTS 表上报错，但库本身已正常解密。
            n = cur.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            if n >= 1:
                return con, tag
            con.close()
        except Exception:  # noqa: BLE001
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
    return None, None


def _sync_wal(db_path: str, clear: str):
    """把源库 WAL 同步到剥头副本旁（SQLCipher 才能读到最新数据）。
    QQ 会频繁 checkpoint 并删除 WAL，故对 copy 失败容错（文件消失即忽略）。"""
    wal = db_path + "-wal"
    try:
        if os.path.exists(wal):
            shutil.copy2(wal, clear + "-wal")
            return
    except OSError:
        pass
    # 无 WAL 或拷贝失败：清掉副本旁可能过期的 WAL，避免读到旧数据
    try:
        if os.path.exists(clear + "-wal"):
            os.remove(clear + "-wal")
    except OSError:
        pass


def _clear_name(db_path: str) -> str:
    return os.path.basename(db_path).replace(".db", "") + "_clear.db"


def find_working_key(db_path: str, work_dir: str, pid: int | None = None,
                     try_ascii: bool = True) -> dict | None:
    """扫描内存 + 逐候选试开，返回命中的 {key, kind, variant, salt} 或 None。
    NTQQ 每个库密钥不同，故按该库文件头 salt 优先匹配候选（避免用错别的库的密钥）。"""
    pid = pid or find_qq_pid()
    if not pid:
        return None
    salt_hex = read_salt(db_path)
    os.makedirs(work_dir, exist_ok=True)
    clear = strip_header(db_path, os.path.join(work_dir, _clear_name(db_path)))
    _sync_wal(db_path, clear)

    cands = scan_memory_candidates(pid, salt_hex)
    # salt 对得上的候选最可信，优先；scan 已按 salt_match 排序，这里再强约束一次
    ordered = [c for c in cands if c.get("salt_match")] + [c for c in cands if not c.get("salt_match")]
    for c in ordered:
        con, variant = _try_open(clear, c)
        if con:
            con.close()
            return {"key": c["key"], "kind": c["kind"], "variant": variant, "salt": c.get("salt")}
    if try_ascii:
        for s in scan_ascii16(pid):
            c = {"kind": "ascii", "key": s, "salt": None}
            con, variant = _try_open(clear, c)
            if con:
                con.close()
                return {"key": s, "kind": "ascii", "variant": variant, "salt": None}
    return None


# 记录上次剥头时主库的 (mtime,size)。主库只在 checkpoint 时变，大多数新消息只进 WAL，
# 故主库没变就复用已剥头副本、只重同步 WAL，避免每次 open 都整文件重拷（原 CPU/IO 大头之一）。
_STRIP_SIG: dict[str, tuple] = {}


def open_decrypted(db_path: str, cred: dict, work_dir: str, name: str = "nt_msg"):
    """按已知密钥打开（剥头 + 同步 WAL + 试开），返回 sqlcipher 连接。失败返回 None。"""
    os.makedirs(work_dir, exist_ok=True)
    clear = os.path.join(work_dir, f"{name}_clear.db")
    try:
        st = os.stat(db_path)
        sig = (st.st_mtime, st.st_size)
    except OSError:
        sig = None
    # 主库(mtime,size)没变且副本在，就跳过整文件重拷；否则重新剥头
    if sig is None or _STRIP_SIG.get(db_path) != sig or not os.path.exists(clear):
        strip_header(db_path, clear)
        if sig is not None:
            _STRIP_SIG[db_path] = sig
    _sync_wal(db_path, clear)                 # WAL 总是重同步（文件小，便宜）
    cand = {"kind": cred["kind"], "key": cred["key"], "salt": cred.get("salt")}
    # 复用已知形态，避免每次 open 都重探失败形态（减少 sqlcipher 错误日志、提速）
    con, _ = _try_open(clear, cand, only_variant=cred.get("variant"))
    return con


def open_db(db_path: str, work_dir: str, pid: int | None = None):
    """对任意 NTQQ 库：找到它自己的密钥并打开，返回连接或 None（供名称映射库用）。"""
    cred = find_working_key(db_path, work_dir, pid, try_ascii=False)
    if not cred:
        return None
    return open_decrypted(db_path, cred, work_dir, name=os.path.basename(db_path).replace(".db", ""))


if __name__ == "__main__":  # 手动自测：python -m qqkey /data/home
    import json
    import sys

    home = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("QQ_HOME", "/data/home")
    db = qq_db_path(home)
    if not db:
        print("未找到 nt_msg.db，QQ 可能未登录")
        raise SystemExit(1)
    pid = find_qq_pid()
    print(f"nt_msg.db={db}\nQQ pid={pid}\nsalt={read_salt(db)}")
    cred = find_working_key(db, "/data/plain", pid)
    if cred:
        print("命中密钥：" + json.dumps(
            {**cred, "key": cred["key"][:8] + "…"}, ensure_ascii=False))
    else:
        print("未找到可用密钥（QQ 是否已登录？容器是否有 SYS_PTRACE？）")
