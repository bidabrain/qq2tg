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

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

POLL_CONFIG_SEC = 5
TOPICS_PATH = DATA / "topics.json"
MUTED_PATH = DATA / "muted.json"


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
        self.muted: set[str] = set()             # 被屏蔽（不转发）的群号 chat_id
        self._load_muted()
        self._mute_pending: dict[int, set[str]] = {}   # 菜单 message_id -> 暂存勾选
        self._recent_sent: list[tuple[str, str, float]] = []
        self._dl_opened: dict[str, float] = {}   # chat_id -> 上次为下载而打开会话的时间（限流）
        # QQ→TG 发送队列：串行消费，避免对每条消息并发 send 把 PTB 连接池占满
        # （曾因此大量 "Pool timeout" / "Timed out" / "Flood control" 失败→消息到不了 TG）。
        self._send_q: asyncio.Queue | None = None
        self._send_gap = float(cfg.get("tg_send_gap", 0.3))   # 相邻发送最小间隔，缓解限流
        self.col = Collector(self.home, on_message=self._on_qq)

    @staticmethod
    def _norm(s: str) -> str:
        return "".join((s or "").split())

    # 媒体类消息的正文是占位符，发回 QQ 后回采可能是 image/video，也可能被 QQ 存成
    # 「文件」（正文 [文件]）或表情，都要能对上发送端记录的 \x00MEDIA，否则媒体回声。
    _MEDIA_PLACEHOLDERS = ("[图片]", "[视频]", "[文件]", "[表情]", "[动画表情]", "[媒体]")

    def _echo_key(self, m: "Message") -> str:
        if m.media in ("image", "video"):
            return "\x00MEDIA"
        t = (m.text or "").lstrip()
        if any(t.startswith(p) for p in self._MEDIA_PLACEHOLDERS):
            return "\x00MEDIA"
        return self._norm(m.text)

    def _is_echo(self, m: "Message") -> bool:
        now = time.time()
        self._recent_sent = [e for e in self._recent_sent if now - e[2] < 120]
        # 统一用 \x00MEDIA 作媒体 key，与 on_tg_photo/on_tg_video 记录的一致，去回声。
        key = self._echo_key(m)
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

    # -------------------------------------------------- 屏蔽名单（/mute）持久化
    def _load_muted(self):
        if MUTED_PATH.exists():
            try:
                self.muted = set(str(x) for x in json.loads(MUTED_PATH.read_text("utf-8")))
            except Exception:  # noqa: BLE001
                self.muted = set()

    def _save_muted(self):
        try:
            tmp = MUTED_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(self.muted), ensure_ascii=False), "utf-8")
            tmp.replace(MUTED_PATH)
        except Exception as e:  # noqa: BLE001
            print("[bridge] 保存屏蔽名单失败:", e, flush=True)

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
        # 灰字提示/系统消息（戳一戳/拍一拍/撤回/入群退群/红包提示…，content_type=8，
        # 正文为 QQ 的 <gtip> 提示而非聊天内容）渲染不出正文、render_body 回「[空消息]」，
        # 默认不转发，免得「[空消息]」刷屏。媒体消息正文是「[图片]/[视频]」非空、不受影响；
        # 需要的话配置 forward_system_tips=true 打开。
        if (m.media is None and (not m.text or m.text == "[空消息]")
                and not self.cfg.get("forward_system_tips", False)):
            return
        # 去回声：桥接刚发到 QQ 的消息会被采集回来，匹配到就跳过（不依赖方向判断，更稳）。
        # 自己从手机/其它 QQ 客户端发的消息不在此列，照常转发（带「🟢 我」前缀）。
        if self._is_echo(m):
            return
        # /mute 屏蔽的群：直接忽略，不转发到 TG
        if m.is_group and m.chat_id in self.muted:
            return
        # 入队（串行消费），不再对每条消息并发 send。_on_qq 在采集线程，跨线程调度到事件循环。
        if self._send_q is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(self._enqueue, m)

    def _enqueue(self, m: "Message"):
        if self._send_q is None:
            return
        try:
            self._send_q.put_nowait(m)
        except asyncio.QueueFull:
            print("[bridge] 发送队列已满，丢弃一条（TG 侧持续发送失败？）", flush=True)

    def _caption(self, m: Message) -> str | None:
        if m.is_group and m.sender_name:
            return f"{m.sender_name}"
        if m.is_self:
            return "🟢 我"
        return None

    @staticmethod
    def _is_thread_gone(e) -> bool:
        """话题在 TG 那边被删/失效（旧 topic_id 已不可用）。"""
        s = str(e).lower()
        return ("thread not found" in s or "topic_id_invalid" in s
                or "topic_id invalid" in s or "thread_not_found" in s)

    def _drop_topic(self, chat_id: str):
        """丢弃失效的话题映射，下次会自动重建新话题。"""
        info = self.map.pop(chat_id, None)
        if info:
            self.rev.pop(info.get("topic_id"), None)
            self._save_topics()
            print(f"[bridge] 话题已失效，丢弃旧映射并将重建：{chat_id} {info.get('name')}", flush=True)

    DL_COOLDOWN = 60.0   # 同一会话为触发下载而打开的最小间隔（秒），避免 UI 抖动

    async def _trigger_qq_download(self, m: Message) -> bool:
        """本地没有媒体文件时，QQ 多半是「懒加载」还没下载该会话的媒体。
        用与回发同一套 UI 自动化打开该会话（滚到底），促使 QQ 把最新消息的媒体下到
        本地 nt_data，之后 _locate_media 再找就有了。只打开不发消息。按会话限流。"""
        if not self.cfg.get("media_download_on_open", True):
            return False
        now = time.time()
        if now - self._dl_opened.get(m.chat_id, 0) < self.DL_COOLDOWN:
            return False
        self._dl_opened[m.chat_id] = now
        number = m.chat_number or m.chat_id
        name = m.chat_name or ""

        def _open():
            # 与 send 的回发共用 _LOCK，串行驱动 QQ 界面，避免互相打架
            with send._LOCK:
                err = send._open_target(number, name)
                if not err:
                    try:
                        send._x("key", "End")   # 滚到最新，渲染触发下载
                    except Exception:  # noqa: BLE001
                        pass
                return err

        try:
            err = await asyncio.to_thread(_open)
        except Exception as e:  # noqa: BLE001
            print("[bridge] 触发下载：打开会话异常:", e, flush=True)
            return False
        if err:
            print(f"[bridge] 触发下载：打开会话失败 {m.chat_id} {name}: {err.get('msg')}", flush=True)
            return False
        print(f"[bridge] 触发下载：已打开会话「{name or m.chat_id}」，等 QQ 下载媒体", flush=True)
        return True

    async def _locate_media(self, m: Message, tries: int = 3, delay: float = 2.0):
        """按 md5 找本地媒体文件；新消息刚入库时文件可能还在落盘，短暂重试再放弃。
        仍找不到：QQ 懒加载还没下载 → 打开会话触发下载，再多找几轮。"""
        for i in range(tries):
            path, kind = await asyncio.to_thread(
                mediafiles.find_media, self.home, m.media, m.media_md5)
            if path:
                return path, kind
            if i < tries - 1:
                await asyncio.sleep(delay)
        if await self._trigger_qq_download(m):
            for _ in range(5):
                await asyncio.sleep(delay)
                path, kind = await asyncio.to_thread(
                    mediafiles.find_media, self.home, m.media, m.media_md5)
                if path:
                    return path, kind
        return None, None

    async def _deliver(self, m: Message):
        """真正发送（话题不存在则 _topic_for 自动建）。失败抛出，交由 _deliver_with_retry 处理。"""
        tid = await self._topic_for(m.chat_id, m.chat_name, m.chat_number)
        # 图片/视频：按 md5 在本地 nt_data 找明文文件，发真实媒体；找不到回退占位文本
        if m.media in ("image", "video") and m.media_md5 and self.cfg.get("media_enabled", True):
            path, kind = await self._locate_media(m)
            if path:
                cap = self._caption(m)
                with open(path, "rb") as f:
                    if kind == "video":
                        await self.app.bot.send_video(self.chat_id, f, message_thread_id=tid, caption=cap)
                    else:
                        await self.app.bot.send_photo(self.chat_id, f, message_thread_id=tid, caption=cap)
                STATE["metrics"]["messages_forwarded"] = STATE["metrics"].get("messages_forwarded", 0) + 1
                save_state()
                return
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

    async def _send_worker(self):
        """串行消费发送队列：一次只发一条，相邻间隔 _send_gap，避免并发占满连接池/触发限流。
        单条带重试：限流(RetryAfter)按服务器要求等待、超时/网络错误退避重试、话题失效则重建。"""
        assert self._send_q is not None
        while True:
            m = await self._send_q.get()
            try:
                await self._deliver_with_retry(m)
            except Exception as e:  # noqa: BLE001
                print("发送到 TG 失败(worker):", e, flush=True)
            finally:
                self._send_q.task_done()
            if self._send_gap > 0:
                await asyncio.sleep(self._send_gap)

    async def _deliver_with_retry(self, m: Message, tries: int = 5):
        last = None
        for _ in range(tries):
            try:
                await self._deliver(m)
                return
            except RetryAfter as e:                       # 触发 Telegram 限流：按要求等待后重试
                last = e
                await asyncio.sleep(getattr(e, "retry_after", 5) + 1)
            except (TimedOut, NetworkError) as e:         # 超时/网络抖动：短退避重试
                last = e
                await asyncio.sleep(1.5)
            except Exception as e:                        # noqa: BLE001
                # 话题被删/失效 → 丢弃旧映射，下一轮 _deliver 会自动重建话题
                if self._is_thread_gone(e) and self.map.get(m.chat_id):
                    self._drop_topic(m.chat_id)
                    last = e
                    continue
                print("发送到 TG 失败:", e, flush=True)
                return
        print("发送到 TG 重试多次仍失败，放弃一条:", last, flush=True)

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
            await self._notify_if_muted(msg, chat_id)
        else:
            await msg.reply_text(f"❌ 发送失败：{r.get('msg')}")

    async def _notify_if_muted(self, msg, chat_id: str):
        """被 /mute 的群：回复仍发到 QQ，但该群消息不会转发回来，回复后提醒一次。"""
        if chat_id not in self.muted:
            return
        try:
            await msg.reply_text(
                "🔇 该群已被 /mute 屏蔽：你的回复已发到 QQ，但这个群的新消息"
                "不会转发到这里。想重新接收该群消息，请 /mute 取消勾选它。")
        except Exception:  # noqa: BLE001
            pass

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
                await self._notify_if_muted(msg, chat_id)
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
        chat_id, number, name = tgt
        ext = "mp4" if (msg.video or msg.animation) else "bin"
        tmp = DATA / "media" / f"tg_{msg.message_id}.{ext}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            tgf = await media_obj.get_file()
            await tgf.download_to_drive(str(tmp))
            r = await asyncio.to_thread(send.send_file_to_chat, number, chat_id, str(tmp), name)
            if r.get("ok"):
                self._recent_sent.append((chat_id, "\x00MEDIA", time.time()))
                await self._notify_if_muted(msg, chat_id)
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

    # -------------------------------------------------- /mute 屏蔽群菜单
    async def _group_choices(self) -> list[tuple[str, str]]:
        """返回 [(群号, 群名)]，按名称排序；已屏蔽但已不在群列表里的也带上，便于取消屏蔽。"""
        groups = await asyncio.to_thread(self.col.names.all_groups)
        merged = dict(groups)
        for gid in self.muted:
            merged.setdefault(gid, groups.get(gid) or gid)
        return sorted(merged.items(), key=lambda kv: (kv[1] or kv[0]))

    def _mute_keyboard(self, choices, pend: set[str]) -> InlineKeyboardMarkup:
        rows = []
        for gid, name in choices:
            mark = "✅" if gid in pend else "⬜"
            label = f"{mark} {(name or gid)[:40]}"
            rows.append([InlineKeyboardButton(label, callback_data=f"m:t:{gid}")])
        rows.append([InlineKeyboardButton(
            f"💾 提交（屏蔽 {len(pend)} 个群）", callback_data="m:s")])
        return InlineKeyboardMarkup(rows)

    async def cmd_mute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """列出所有群，勾选要屏蔽（不转发）的群，点「提交」生效。再次 /mute 显示当前勾选状态。"""
        m = update.effective_message
        choices = await self._group_choices()
        if not choices:
            await m.reply_text("没取到群列表（QQ 是否已登录并取到密钥？可稍后再试或先 /synctopic）。")
            return
        pend = set(self.muted)                      # 初始勾选 = 当前已屏蔽
        sent = await m.reply_text(
            "🔇 勾选要屏蔽（不转发到 TG）的群，点一下切换 ✅/⬜，改好后点底部「提交」：",
            reply_markup=self._mute_keyboard(choices, pend))
        # 连同群列表一起缓存，切换时直接用，不必每次点击都去解密 DB（更快更稳）
        self._mute_pending[sent.message_id] = {"pend": pend, "choices": choices}

    async def on_mute_cb(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q:
            return
        try:
            await q.answer()                        # 先消掉转圈，再处理
        except Exception:  # noqa: BLE001
            pass
        msg = q.message
        if not msg:
            return
        mid = msg.message_id
        data = q.data or ""
        st = self._mute_pending.get(mid)
        if st is None:                              # bot 重启后菜单失效 → 以当前屏蔽名单重建
            st = {"pend": set(self.muted), "choices": await self._group_choices()}
            self._mute_pending[mid] = st
        pend, choices = st["pend"], st["choices"]
        if data == "m:s":                           # 提交
            self.muted = set(pend)
            self._save_muted()
            self._mute_pending.pop(mid, None)
            nmap = dict(choices)
            if self.muted:
                lst = "\n".join("· " + (nmap.get(g) or g) for g in sorted(self.muted))
                txt = f"✅ 已屏蔽 {len(self.muted)} 个群（这些群的消息不再转发）：\n{lst}"
            else:
                txt = "✅ 已提交：当前没有屏蔽任何群。"
            try:
                await q.edit_message_text(txt)
            except Exception as e:  # noqa: BLE001
                print("[mute] 提交改文本失败:", e, flush=True)
            return
        if data.startswith("m:t:"):                 # 切换某个群
            gid = data[4:]
            if gid in pend:
                pend.discard(gid)
            else:
                pend.add(gid)
            try:
                await q.edit_message_reply_markup(self._mute_keyboard(choices, pend))
            except Exception as e:  # noqa: BLE001  内容未变等
                print("[mute] 切换改键盘失败:", e, flush=True)

    # -------------------------------------------------- 运行
    async def run(self):
        # 加大连接池与超时：默认池很小，QQ→TG 并发/媒体上传时极易 "Pool timeout" / "Timed out"。
        self.app = (Application.builder()
                    .token(self.cfg["tg_bot_token"])
                    .connection_pool_size(16)
                    .pool_timeout(20.0)
                    .connect_timeout(20.0)
                    .read_timeout(30.0)
                    .write_timeout(60.0)
                    .get_updates_read_timeout(40.0)
                    .build())
        self.app.add_handler(CommandHandler("contacts", self.cmd_contacts, filters.Chat(self.chat_id)))
        self.app.add_handler(CommandHandler("synctopic", self.cmd_synctopic, filters.Chat(self.chat_id)))
        self.app.add_handler(CommandHandler("mute", self.cmd_mute, filters.Chat(self.chat_id)))
        self.app.add_handler(CallbackQueryHandler(self.on_mute_cb, pattern=r"^m:"))
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & filters.TEXT & ~filters.COMMAND, self.on_tg_message))
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & filters.PHOTO, self.on_tg_photo))
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & (filters.VIDEO | filters.ANIMATION | filters.Document.ALL),
            self.on_tg_video))
        self.loop = asyncio.get_running_loop()
        self._send_q = asyncio.Queue(maxsize=2000)   # QQ→TG 串行发送队列
        asyncio.create_task(self._send_worker())
        self._clean_media(0)                 # 启动清空残留的下载临时文件
        asyncio.create_task(self._media_cleaner())
        threading.Thread(target=self._collector_loop, daemon=True).start()
        print(f"[bridge] 已启动，目标群 chat_id={self.chat_id}，已载入 {len(self.map)} 个 topic 映射", flush=True)
        await self.app.initialize()
        await self.app.start()
        # 必须显式带上 callback_query：Telegram 会记住上一次 getUpdates 的 allowed_updates，
        # 旧程序可能只订阅了 message，导致内联按钮回调收不到（/mute 勾选点了没反应）。
        await self.app.updater.start_polling(
            drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
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
                BotCommand("mute", "屏蔽/取消屏蔽某些群（勾选后提交）"),
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
