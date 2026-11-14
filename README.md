# qq2tg

把 **QQ** 的消息转发到 **Telegram** 的一体化 Docker 方案。思路与
[wechat2tg](https://github.com/bidabrain/wechat2tg) 一致：在容器里跑官方 **Linux 桌面版 QQ(NTQQ)**，
**只读解密本地聊天数据库** `nt_msg.db` 读取新消息，再通过 Telegram Bot 转发；每个 QQ 会话对应
Telegram 群里的一个 **话题(Topic)**。无需安卓模拟器 / KVM，纯容器运行。

> ⚠️ 仅供学习研究，操作你**自己账号**的本地数据。有封号风险，建议用小号。

## 工作原理

```
Docker 容器 (cap_add: SYS_PTRACE, seccomp:unconfined)
 ├─ Xvfb + openbox + x11vnc + noVNC   扫码登录 / 查看画面
 ├─ 官方 Linux QQ(NTQQ, --disable-gpu) 常驻，手机扫码登录为「电脑在线」
 ├─ qqkey   取密钥 + 解密              读 /proc/<pid>/mem 找 SQLCipher 凭据，
 │                                     剥 1024 字节头后按 NTQQ 参数解密（每库独立密钥）
 ├─ names   显示名解析                 解 group_info/profile_info 得群名、好友昵称/备注
 ├─ collector  采集                   解析 c2c_msg_table / group_msg_table，
 │                                     轮询 WAL 产出新消息；媒体按 md5 定位本地文件
 ├─ bridge     QQ ⇄ Telegram          每会话一个 Topic；双向转发文字 + 图片 + 视频
 └─ manager    FastAPI 面板 :8080      noVNC 扫码、配置、日志、最近消息
```

> QQ 是 Electron/CEF 应用，Xvfb 无头环境下必须 `--disable-gpu`（启动脚本已带），否则
> 界面自动化操作后 CEF 窗口会黑屏/崩溃。

### 解密要点（NTQQ）

- 库路径：`~/.config/QQ/**/nt_db/nt_msg.db`
- 文件头：前 **1024 字节**为自定义头，需剥掉才是标准 SQLCipher 数据区
- SQLCipher 参数（非默认）：`page_size=4096`、`kdf_iter=4000`、
  `cipher_kdf_algorithm=PBKDF2_HMAC_SHA512`、`cipher_hmac_algorithm=HMAC_SHA1`、`aes-256-cbc`
- 取密钥：登录后 SQLCipher 凭据（`x'<64hex key><32hex salt>'` 或 16 字节 ASCII 口令）
  驻留 QQ 进程内存，本项目**只读扫描** `/proc/<pid>/mem`，用库文件头的 salt 交叉校验，
  再逐候选试开，能通过 `integrity_check` 的即为本库密钥。不注入、不改 QQ 文件。

参考实现：
[QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key)（Linux 取密钥 / gdb 断点 `nt_sqlite3_key_v2`）、
[ZEntrosein/qq-nt-export](https://github.com/ZEntrosein/qq-nt-export)（内存扫描 + 多候选解密 + 消息解析）。

## 部署

```bash
docker compose up -d --build
```

- 打开 `http://<host>:8080`，用 `WEB_PASSWORD`（在 `docker-compose.yml` 里改）登录
- 「QQ 画面」标签里用手机 QQ 扫码登录
- 「Telegram」标签填 Bot Token 和 Chat ID，保存

`docker-compose.yml` 关键项：`cap_add: SYS_PTRACE`、`security_opt: seccomp:unconfined`
（取密钥 / gdb 需要）、`shm_size: 1g`、`./data:/data`（持久化登录态、数据库、配置、日志）。

> QQ 的 deb 直链在 `qqdl.gtimg.cn`，路径含版本号与哈希段，会随版本更新（官网下载配置里
> 的 `dldir1v6` 链接对公网并不可下）。最新可用链接可参考 AUR `linuxqq` 包的 PKGBUILD。
> 若构建时下载报 404，用 `--build-arg QQ_DEB=<最新 amd64 deb 直链>` 覆盖。

## Telegram 配置

1. `@BotFather` 建 bot 拿 **Bot Token**
2. `@userinfobot` 拿你的 **user id**；或建一个群、开启 **Topics（论坛模式）**、把 bot 拉进群设为
   管理员并给「管理话题」权限，用群 id（`-100` 开头）
3. 面板「Telegram」里填入并保存

## 使用

1. **扫码登录**：面板「QQ 画面」标签里会显示 QQ 登录二维码，用手机 QQ 扫码（建议用小号）
2. **配 Telegram**：「Telegram」标签填 Bot Token、Chat ID，勾选「QQ→TG 转发」「TG→QQ 回发」后保存
3. **收消息**：QQ 来新消息 → 自动转发到目标群里对应的话题（没有就自动建，标题用群名/昵称）
4. **回消息**：直接在某个话题里发**文字 / 图片 / 视频** → 自动发回该话题对应的 QQ 会话
5. **bot 命令**：
   - `/contacts [关键词]` — 列出最近会话
   - `/synctopic` — 群名/昵称变了时，重新同步并给已建话题改名

## 功能

- ✅ **QQ→TG**：文本、图片、视频转发；话题标题显示**群名 / 好友昵称**（非号码）
- ✅ **TG→QQ 回发**：文字（输入发送）、图片（选择器）、视频/文件（发送文件对话框）；按号码定位会话，核对不过不发，避免误发
- ✅ **去回声**：桥接自己发回 QQ 的消息（含媒体）不会再被转回 TG
- ✅ **话题映射**持久化到 `/data/topics.json`；`/synctopic` 可随时重同步名称

## 已知限制

- **QQ→TG 媒体**：只有 QQ **已下载到本地**的图片/视频能发真实文件。新来的消息 QQ 会自动下载缩略图，故实时转发正常；**历史上没点开过的旧图/旧视频**本地无文件，回退占位（`[图片]`/`[视频]`）。要取全部历史媒体需走 QQ 的 CDN 下载（需鉴权），为后续项。
- **TG→QQ 界面坐标**：按 `1000x720` 窗口校准（NTQQ 3.2.35）。换 QQ 版本/分辨率可能需微调 `manager/send.py` 顶部的坐标常量。
- 不同 QQ 版本的字段 / 密钥形态可能变化；取密钥需要容器有 `SYS_PTRACE`。
- 若界面自动化中途 QQ 黑屏（理论上 `--disable-gpu` 后极少），`docker exec qq2tg supervisorctl -c /etc/supervisor/qq.conf restart qq` 重启即可（登录态在数据卷不丢）。

## 自测（容器内）

```bash
# 取密钥 + 解密是否打通
docker exec -it qq2tg /opt/manager/venv/bin/python -m qqkey /data/home
# 解析最近消息
docker exec -it qq2tg /opt/manager/venv/bin/python -m collector /data/home
```
