"""QQ ⇄ Telegram 桥接（Topic 模式）。

每个 QQ 会话对应 Telegram 群里的一个 Topic（话题）：
  QQ → TG：新消息发到对应会话的 topic（没有就自动建）。
  TG → QQ：你直接在某个 topic 里发言，就发回该 topic 对应的 QQ 会话
           （需开启 reply_enabled；经 UI 自动化，实验性）。

topic 映射持久化到 /data/topics.json。
前提：目标群需开启 Topics（论坛模式），且 bot 为管理员并有「管理话题」权限。

当前版本 QQ→TG 转发文本与媒体占位（[图片]/[视频]/[文件]…）；媒体文件提取为后续项。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

try:
    from . import config, send
    from .collector import Collector, Message
    from .common import DATA, STATE, save_state
except ImportError:  # 独立运行
    import config
    import send
    from collector import Collector, Message
    from common import DATA, STATE, save_state

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

POLL_CONFIG_SEC = 5
TOPICS_PATH = DATA / "topics.json"


class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.chat_id = int(cfg["tg_chat_id"]) if str(cfg.get("tg_chat_id", "")).lstrip("-").isdigit() else None
        self.home = cfg["qq_home"]
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app: Application | None = None
        self._topic_lock = asyncio.Lock()
        self._forum_ok = True
        self.map: dict[str, dict] = {}           # qq_chat_id -> {topic_id, name}
        self.rev: dict[int, str] = {}
        self._load_topics()
        self._recent_sent: list[tuple[str, str, float]] = []
        self.col = Collector(self.home, on_message=self._on_qq)

    @staticmethod
    def _norm(s: str) -> str:
        return "".join((s or "").split())

    def _is_echo(self, m: "Message") -> bool:
        now = time.time()
        self._recent_sent = [e for e in self._recent_sent if now - e[2] < 120]
        key = self._norm(m.text)
        for i, (cid, txt, _) in enumerate(self._recent_sent):
            if cid == m.chat_id and txt == key:
                self._recent_sent.pop(i)
                return True
        return False

    # -------------------------------------------------- 映射持久化
    def _load_topics(self):
        if TOPICS_PATH.exists():
            try:
                self.map = json.loads(TOPICS_PATH.read_text("utf-8"))
                self.rev = {v["topic_id"]: k for k, v in self.map.items()}
            except Exception:  # noqa: BLE001
                self.map, self.rev = {}, {}

    def _save_topics(self):
        try:
            tmp = TOPICS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.map, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(TOPICS_PATH)
        except Exception as e:  # noqa: BLE001
            print("[bridge] 保存 topics 失败:", e, flush=True)

    async def _topic_for(self, chat_id: str, chat_name: str, number: str = "") -> int | None:
        hit = self.map.get(chat_id)
        if hit:
            return hit["topic_id"]
        if not self._forum_ok:
            return None
        async with self._topic_lock:
            hit = self.map.get(chat_id)
            if hit:
                return hit["topic_id"]
            try:
                t = await self.app.bot.create_forum_topic(self.chat_id, name=(chat_name or number or chat_id)[:120])
                tid = t.message_thread_id
            except Exception as e:  # noqa: BLE001
                self._forum_ok = False
                print(f"[bridge] 建 topic 失败（群是否开启 Topics？bot 是否管理员且有管理话题权限？）: {e}", flush=True)
                return None
            self.map[chat_id] = {"topic_id": tid, "name": chat_name, "number": number}
            self.rev[tid] = chat_id
            self._save_topics()
            try:
                await self.app.bot.unpin_all_forum_topic_messages(self.chat_id, tid)
            except Exception:  # noqa: BLE001
                pass
            return tid

    # -------------------------------------------------- QQ → TG
    def _on_qq(self, m: Message):
        if not self.cfg.get("forward_enabled", True) or not self.loop or not self.app:
            return
        if m.is_self and self._is_echo(m):
            return
        asyncio.run_coroutine_threadsafe(self._send_tg(m), self.loop)

    async def _send_tg(self, m: Message):
        try:
            tid = await self._topic_for(m.chat_id, m.chat_name, m.chat_number)
            text = m.text or ""
            if m.is_group and m.sender_name:
                text = f"{m.sender_name}:\n{text}"
            elif m.is_self:
                text = f"🟢 我：{text}"
            elif tid is None:
                text = f"【{m.chat_name}】{text}"
            await self.app.bot.send_message(self.chat_id, text, message_thread_id=tid)
            STATE["metrics"]["messages_forwarded"] = STATE["metrics"].get("messages_forwarded", 0) + 1
            save_state()
        except Exception as e:  # noqa: BLE001
            print("发送到 TG 失败:", e, flush=True)

    # -------------------------------------------------- TG → QQ
    async def on_tg_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.cfg.get("reply_enabled", False):
            return
        msg = update.effective_message
        if not msg or not msg.text:
            return
        tid = msg.message_thread_id
        chat_id = self.rev.get(tid) if tid else None
        if not chat_id:
            return
        info = self.map[chat_id]
        number = info.get("number") or info.get("name")   # 发送按号码定位
        r = await asyncio.to_thread(send.send_to_chat, number, chat_id, msg.text)
        if r.get("ok"):
            self._recent_sent.append((chat_id, self._norm(msg.text), time.time()))
        else:
            await msg.reply_text(f"❌ 发送失败：{r.get('msg')}")

    # -------------------------------------------------- 命令
    async def cmd_contacts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        kw = " ".join(ctx.args) if ctx.args else ""
        rows = await asyncio.to_thread(self.col.list_sessions, kw, 40)
        if not rows:
            await update.effective_message.reply_text("没找到匹配的会话（QQ 是否已登录并取到密钥？）")
            return
        lines = [("👥 " if r["is_group"] else "👤 ") + r["name"] for r in rows]
        await update.effective_message.reply_text("最近会话：\n" + "\n".join(lines[:40]))

    async def cmd_synctopic(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """手动重同步昵称/群名 → 刷新映射并把已建 topic 改名（昵称/群名会变）。"""
        m = update.effective_message
        await m.reply_text("🔄 正在重新同步昵称/群名…")
        ok = await asyncio.to_thread(self.col.names.refresh)
        if not ok:
            await m.reply_text("❌ 同步失败：未能解密 group_info/profile_info（QQ 是否已登录？）")
            return
        renamed = 0
        for chat_id, info in list(self.map.items()):
            is_group = not str(chat_id).startswith("u_")
            number = info.get("number") or info.get("name")
            new_name = self.col.names.display(chat_id, is_group, number) or number
            if new_name and new_name != info.get("name"):
                try:
                    await self.app.bot.edit_forum_topic(
                        self.chat_id, info["topic_id"], name=new_name[:120])
                    info["name"] = new_name
                    renamed += 1
                except Exception as e:  # noqa: BLE001
                    print("[bridge] 改 topic 名失败:", e, flush=True)
        self._save_topics()
        await m.reply_text(f"✅ 同步完成，更新了 {renamed} 个话题名称。")

    # -------------------------------------------------- 运行
    async def run(self):
        self.app = Application.builder().token(self.cfg["tg_bot_token"]).build()
        self.app.add_handler(CommandHandler("contacts", self.cmd_contacts, filters.Chat(self.chat_id)))
        self.app.add_handler(CommandHandler("synctopic", self.cmd_synctopic, filters.Chat(self.chat_id)))
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & filters.TEXT & ~filters.COMMAND, self.on_tg_message))
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self._collector_loop, daemon=True).start()
        print(f"[bridge] 已启动，目标群 chat_id={self.chat_id}，已载入 {len(self.map)} 个 topic 映射", flush=True)
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        await self._reset_commands()
        try:
            await self.app.bot.send_message(self.chat_id, "✅ qq2tg 桥接已连接（Topic 模式）。")
        except Exception as e:  # noqa: BLE001
            print("连接测试消息发送失败（检查 chat_id / bot 是否在群里）:", e, flush=True)
        while True:
            await asyncio.sleep(3600)

    async def _reset_commands(self):
        """清掉旧程序残留在各作用域的命令，再把本 bot 命令设到默认作用域。
        Telegram 的 bot 命令按 scope 存，只设默认作用域不会清掉旧程序设在
        私聊/群/群管理员/指定群等作用域里的命令，故逐个作用域先删。"""
        from telegram import (
            BotCommandScopeDefault, BotCommandScopeAllPrivateChats,
            BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators,
            BotCommandScopeChat, BotCommandScopeChatAdministrators,
        )
        scopes = [
            BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(), BotCommandScopeAllChatAdministrators(),
        ]
        if self.chat_id:
            scopes += [BotCommandScopeChat(chat_id=self.chat_id),
                       BotCommandScopeChatAdministrators(chat_id=self.chat_id)]
        # 各语言菜单也可能有残留：清默认语言 + 常见 language_code
        for sc in scopes:
            for lang in (None, "", "zh", "zh-hans", "en"):
                try:
                    await self.app.bot.delete_my_commands(scope=sc, language_code=lang)
                except Exception:  # noqa: BLE001
                    pass
        try:
            await self.app.bot.set_my_commands([
                BotCommand("contacts", "最近会话（可带关键词）"),
                BotCommand("synctopic", "重新同步昵称/群名到话题标题"),
            ], scope=BotCommandScopeDefault())
            print("[bridge] 已清理旧命令并设置菜单", flush=True)
        except Exception as e:  # noqa: BLE001
            print("设置命令菜单失败:", e, flush=True)

    def _collector_loop(self):
        while True:
            try:
                if self.col.ready():
                    self.col.poll_loop()
                else:
                    print("[bridge] QQ 未登录/未就绪，等待…", flush=True)
            except Exception as e:  # noqa: BLE001
                print("[bridge] 采集循环异常，5s 后重试:", e, flush=True)
            time.sleep(5)


def main():
    while True:
        cfg = config.load()
        if not cfg.get("tg_bot_token") or not cfg.get("tg_chat_id"):
            print("[bridge] 未配置 TG_BOT_TOKEN / TG_CHAT_ID，等待面板配置…", flush=True)
            time.sleep(POLL_CONFIG_SEC)
            continue
        try:
            asyncio.run(Bridge(cfg).run())
        except Exception as e:  # noqa: BLE001
            print("[bridge] 退出，10s 后重启:", e, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
