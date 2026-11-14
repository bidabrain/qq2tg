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
 ├─ 官方 Linux QQ(NTQQ)               常驻，手机扫码登录为「电脑在线」
 ├─ qqkey   取密钥 + 解密              读 /proc/<pid>/mem 找 SQLCipher 凭据，
 │                                     剥 1024 字节头后按 NTQQ 参数解密
 ├─ collector  采集                   解析 c2c_msg_table / group_msg_table，
 │                                     轮询 WAL 产出新消息
 ├─ bridge     QQ ⇄ Telegram          每会话一个 Topic；TG→QQ 回发为实验性
 └─ manager    FastAPI 面板 :8080      noVNC 扫码、配置、日志、最近消息
```

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

## 当前状态 / 已知限制

- ✅ QQ→TG 文本转发、Topic 映射、面板、取密钥与解密链路已实现
- 🚧 **显示名**：好友备注 / 群名在另外的库里，当前先用 QQ 号 / 群号；群内发送者用消息自带昵称
- 🚧 **媒体**：图片 / 视频 / 文件目前以占位文本转发（`[图片]` 等），文件提取为后续项
- 🚧 **TG→QQ 回发**：经 xdotool UI 自动化，**实验性**，坐标需按实际 QQ 界面校准，默认关闭
- ⚠️ `is_self`（自己发出）用消息方向启发式判断，需用真实数据校准
- ⚠️ 不同 QQ 版本的字段 / 密钥形态可能变化；取密钥需要容器有 `SYS_PTRACE`

这些待办都需要一个**已登录的真实 QQ 账号**在本机实测后再逐步校准完善。

## 自测（容器内）

```bash
# 取密钥 + 解密是否打通
docker exec -it qq2tg /opt/manager/venv/bin/python -m qqkey /data/home
# 解析最近消息
docker exec -it qq2tg /opt/manager/venv/bin/python -m collector /data/home
```
