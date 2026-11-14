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
    from . import config, send, mediafiles
    from .collector import Collector, Message
    from .common import DATA, STATE, save_state
except ImportError:  # 独立运行
    import config
    import send
    import mediafiles
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
        # 媒体消息的正文是占位([图片]/[视频])，无法按文本匹配；统一用 \x00MEDIA 作 key，
        # 与 on_tg_photo/on_tg_video 记录的一致，从而正确识别桥接自己发的媒体、去回声。
        key = "\x00MEDIA" if m.media in ("image", "video") else self._norm(m.text)
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
        # 去回声：桥接刚发到 QQ 的消息会被采集回来，匹配到就跳过（不依赖方向判断，更稳）。
        # 自己从手机/其它 QQ 客户端发的消息不在此列，照常转发（带「🟢 我」前缀）。
        if self._is_echo(m):
            return
        asyncio.run_coroutine_threadsafe(self._send_tg(m), self.loop)

    def _caption(self, m: Message) -> str | None:
        if m.is_group and m.sender_name:
            return f"{m.sender_name}"
        if m.is_self:
            return "🟢 我"
        return None

    async def _send_tg(self, m: Message):
        try:
            tid = await self._topic_for(m.chat_id, m.chat_name, m.chat_number)
            # 图片/视频：按 md5 在本地 nt_data 找明文文件，发真实媒体；找不到回退占位文本
            if m.media in ("image", "video") and m.media_md5 and self.cfg.get("media_enabled", True):
                path, kind = await asyncio.to_thread(
                    mediafiles.find_media, self.home, m.media, m.media_md5)
                if path:
                    cap = self._caption(m)
                    try:
                        with open(path, "rb") as f:
                            if kind == "video":
                                await self.app.bot.send_video(self.chat_id, f, message_thread_id=tid, caption=cap)
                            else:
                                await self.app.bot.send_photo(self.chat_id, f, message_thread_id=tid, caption=cap)
                        STATE["metrics"]["messages_forwarded"] = STATE["metrics"].get("messages_forwarded", 0) + 1
                        save_state()
                        return
                    except Exception as e:  # noqa: BLE001
                        print("发送媒体到 TG 失败，回退文本:", e, flush=True)
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
        number = info.get("number") or info.get("name")   # 先按号码定位
        name = info.get("name") or ""                      # 号码搜不到时按群名兜底
        r = await asyncio.to_thread(send.send_to_chat, number, chat_id, msg.text, name)
        if r.get("ok"):
            self._recent_sent.append((chat_id, self._norm(msg.text), time.time()))
        else:
            await msg.reply_text(f"❌ 发送失败：{r.get('msg')}")

    def _target(self, msg):
        """从 TG 话题消息解析出 (chat_id, 号码, 群名)；非映射话题返回 None。"""
        tid = msg.message_thread_id if msg else None
        chat_id = self.rev.get(tid) if tid else None
        if not chat_id:
            return None
        info = self.map[chat_id]
        return chat_id, (info.get("number") or info.get("name")), (info.get("name") or "")

    async def on_tg_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """TG 话题里发来的图片 → 下载 → 按号码定位会话 → 剪贴板粘贴发回 QQ。"""
        if not self.cfg.get("reply_enabled", False):
            return
        msg = update.effective_message
        tgt = self._target(msg)
        if not tgt or not msg.photo:
            return
        chat_id, number, name = tgt
        tmp = DATA / "media" / f"tg_{msg.message_id}.jpg"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tgf = await msg.photo[-1].get_file()
            await tgf.download_to_drive(str(tmp))
            r = await asyncio.to_thread(send.send_image_to_chat, number, chat_id, str(tmp), name)
            if r.get("ok"):
                self._recent_sent.append((chat_id, "\x00MEDIA", time.time()))
            else:
                await msg.reply_text(f"❌ 图片发送失败：{r.get('msg')}")
        except Exception as e:  # noqa: BLE001
            await msg.reply_text(f"❌ 图片发送异常：{e}")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    async def on_tg_video(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """TG 话题里发来的视频/文件 → 下载 → 按号码定位会话 → 用「发送文件」发回 QQ。"""
        if not self.cfg.get("reply_enabled", False):
            return
        msg = update.effective_message
        tgt = self._target(msg)
        media_obj = (msg.video or msg.animation or msg.document) if msg else None
        if not tgt or not media_obj:
            return
        chat_id, number = tgt
        ext = "mp4" if (msg.video or msg.animation) else "bin"
        tmp = DATA / "media" / f"tg_{msg.message_id}.{ext}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tgf = await media_obj.get_file()
            await tgf.download_to_drive(str(tmp))
            r = await asyncio.to_thread(send.send_file_to_chat, number, chat_id, str(tmp), name)
            if r.get("ok"):
                self._recent_sent.append((chat_id, "\x00MEDIA", time.time()))
            else:
                await msg.reply_text(f"❌ 视频/文件发送失败：{r.get('msg')}")
        except Exception as e:  # noqa: BLE001
            await msg.reply_text(f"❌ 视频/文件发送异常：{e}")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

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
        """手动重同步昵称/群名 → 刷新映射并把已建 topic 改名（昵称/群名会变）。
        逐个限速改名并处理 Telegram 限流(flood control)，避免改名失败。"""
        from telegram.error import RetryAfter
        m = update.effective_message
        await m.reply_text("🔄 正在重新同步昵称/群名（逐个限速改名以避开 Telegram 限流，稍候）…")
        ok = await asyncio.to_thread(self.col.names.refresh)
        if not ok:
            await m.reply_text("❌ 同步失败：未能解密 group_info/profile_info（QQ 是否已登录？）")
            return
        # 先算出真正需要改名的列表（名字有变化的才改）
        todo = []
        for chat_id, info in list(self.map.items()):
            is_group = not str(chat_id).startswith("u_")
            number = info.get("number") or info.get("name")
            new_name = self.col.names.display(chat_id, is_group, number) or number
            if new_name and new_name != info.get("name"):
                todo.append((info, new_name))
        renamed, failed = 0, 0
        for info, new_name in todo:
            for _ in range(4):
                try:
                    await self.app.bot.edit_forum_topic(
                        self.chat_id, info["topic_id"], name=new_name[:120])
                    info["name"] = new_name
                    renamed += 1
                    break
                except RetryAfter as e:           # 命中限流：按要求等足再重试
                    await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
                except Exception as e:  # noqa: BLE001
                    print("[bridge] 改 topic 名失败:", e, flush=True)
                    failed += 1
                    break
            await asyncio.sleep(1.5)               # 限速，避免触发 flood control
        self._save_topics()
        msg = f"✅ 同步完成，更新了 {renamed} 个话题名称"
        if failed:
            msg += f"；{failed} 个失败，可稍后再 /synctopic 重试"
        await m.reply_text(msg)

    # -------------------------------------------------- 运行
    async def run(self):
        self.app = Application.builder().token(self.cfg["tg_bot_token"]).build()
        self.app.add_handler(CommandHandler("contacts", self.cmd_contacts, filters.Chat(self.chat_id)))
        self.app.add_handler(CommandHandler("synctopic", self.cmd_synctopic, filters.Chat(self.chat_id)))
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & filters.TEXT & ~filters.COMMAND, self.on_tg_message))
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & filters.PHOTO, self.on_tg_photo))
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & (filters.VIDEO | filters.ANIMATION | filters.Document.ALL),
            self.on_tg_video))
        self.loop = asyncio.get_running_loop()
        self._clean_media(0)                 # 启动清空残留的下载临时文件
        asyncio.create_task(self._media_cleaner())
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

    def _clean_media(self, max_age: float):
        """清理 /data/media 下的下载临时文件（max_age=0 清全部；否则清超过该秒数的）。
        正常每次发完就删(finally unlink)；此处兜底清掉崩溃残留，保证目录不膨胀。"""
        d = DATA / "media"
        if not d.exists():
            return
        now = time.time()
        for f in d.iterdir():
            try:
                if f.is_file() and f.name.startswith("tg_") and now - f.stat().st_mtime >= max_age:
                    f.unlink()
            except OSError:
                pass

    async def _media_cleaner(self):
        """每 5 分钟清一次超过 5 分钟的残留下载临时文件。"""
        while True:
            await asyncio.sleep(300)
            self._clean_media(300)

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
