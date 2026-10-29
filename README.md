# V360 Web：开发验证版

工程目录：`/home/yungzzi/workspace/04_tools-and-scripts/v360-web`（WSL Ubuntu）。

目标：为用户自有的太阳能 4G 摄像头提供带身份认证的网页访问，支持按需直播、云台和 SD 录像同步。已实现接入、SD 取片、本地网页直播、云台点按、电量和历史录像列表回放；已增加定时备份和滚动清理，云端部署尚未实现。

录像验收范围已按用户确认调整：备份设备实际产生的观看期间录像与动作触发录像；不要求全天连续录像，不为补齐空白时间强制常开。

共享服务器初始预算：录像 10 GiB、整个项目磁盘 12 GiB，保持系统可用空间至少 8 GiB；CPU 总额度先按 1 vCPU、内存压力阈值 512 MiB/硬上限 768 MiB 设计。当前为待实现的部署约束，见 [资源预算](docs/resource-budget.md)。

## 当前已经实现

- 按日期查询历史目录、下载进度、网页回放、下载原片和持久任务记录；容量/磁盘余量检查，以及后台自动保存和旧副本滚动清理。见 [历史录像说明](docs/recording-library.md)。
- 电池图标与百分比，观看时每 5 分钟自动更新，以及观看期间的上下左右云台点按和自动请求停止；详见 [自动电量与云台](docs/automatic-battery-ptz.md)。
- 本地网页认证、持续播放、开始/停止和共享转码，真实 Edge 双窗口验收通过；使用方式与边界见 [网页直播说明](docs/browser-live.md)。
- 无手机实时开流、主视频通道解帧、有限时长取流、停止与保活，转换 H.264 预览并经鉴权 API 提供；详见 [实时取流说明](docs/live-capture.md)。
- 从已分析的 V360 Pro 7.2.0 APK 静态导入 SDK，校验原包 SHA-256；SDK 和初始化参数只存本地私有目录。
- 命令编解码、正常设备登录、分页录像目录、按设备/通道/时间请求下载。
- 部分读取、超时、开始/结束标记、包序号和声明文件长度校验。
- SQLite 录像索引、幂等发现、临时文件和“已下载但未验证”状态。
- FFprobe 检查和 FFmpeg 解码验证；只有通过验证的录像可经 API 访问。
- Bearer 鉴权 API 和媒体专用会话，串行摄像头及回放准备任务；任务写入 SQLite；自动备份按持久策略定时运行，失败片段延迟重试，认证失败停止自动重试。
- Android USB 助手：本机回环监听、一次性六位码配对、每条 RPC 的令牌认证、连接归属与断线释放。
- WSL 通过 Windows Python 私有管道访问 Windows 的 ADB USB 转发；不依赖 WSL 直通 USB，也不配置公开桥接端口。

**初期手机桥阶段已完成配对、账号参数导入、摄像头认证、SD 目录查询和首片下载解码。该阶段取得 95 条有效记录及 1 条异常时间记录，目录完整性标为 false。** 后续无手机及网页直播验收见下文与 [开发验证记录](docs/validation.md)。

**最新默认后端已切换为 `native-arm64`，本机完成无手机的认证、101 条有效记录查询和另一个真实片段下载、解码。** 手机助手已停止，USB 转发已移除；详见 [无手机后端说明](docs/native-backend.md)。服务器迁移、独立休眠唤醒与长期稳定性仍待验收。

## Git 规则

用户要求：开发使用新功能分支，普通提交不得直接落在 `main`，功能完成后才合并，所有合并显式使用 `--no-ff`。

本地功能验收基线合并至 `main`，所有集成保留 `--no-ff` 合并记录。直播、电量、云台点按、历史录像下载回放与自动保存已验证；长期部署尚未验收，后续继续新建功能分支。

账号参数导入在 `feat/cloud-device-onboarding` 开发，已完成真实账号设备参数导入与后续摄像头认证检查。

```bash
python3 tools/setup_git.py
git switch -c feat/your-feature main
# 开发和检查，然后在功能分支提交
git switch main
git merge --no-ff feat/your-feature
```

本地配置设为 `merge.ff=false`，仓库 hook 阻止直接向 main 普通提交。初始化根提交在初始化分支创建，main 从该基线建立。没有向 main 直接提交实现代码。

## 开发检查

本机已经准备好工程私有工具及 Python 环境。

```bash
cd /home/yungzzi/workspace/04_tools-and-scripts/v360-web
uv sync --extra dev --locked
uv run v360 doctor
uv run --extra dev ruff check src tests tools
uv run --extra dev pytest -q
```

原始 APK 与 REA 证据继续留在 Windows 的 `逆向v360pro/analysis` 下。当前程序严格限定到已分析原包的 SHA-256，未支持任意版本 APK。

## 当前默认接入

本机使用 `native-arm64`，无需手机桥令牌、ADB 或 Windows Python。`uv run v360 bridge-check` 可检查当前适配器；其名称保留但已不代表必须使用手机。新机器的私有依赖准备见 [无手机后端说明](docs/native-backend.md)。

## 可选的手机对照路径

按 [手机接入说明](docs/phone-setup.md) 安装助手、通过 USB 连接你身边的 Android 设备，先执行 `pair-bridge` 与 `bridge-check`。这一阶段不需要摄像头密码，不会主动连接家中的摄像头。

用户不知道单独设备密码时，可按 [账号导入说明](docs/account-onboarding.md) 在本地运行 `uv run v360 cloud-login`，通过正常账号登录取得目标设备参数。已有有效会话时可运行 `uv run v360 cloud-import`，排查字段处理时可用 `--cached` 复用私有列表。

后续填好正常设备凭据，再验证一个 SD 片段：

```bash
# 交互式输入到本地私有配置，不在命令参数中输入密码
uv run v360 configure
uv run v360 connect-check

# 时间表示设备自己的本地时间；不要附加 Z 或 UTC 偏移
# channel 必须取实际设备通道，下面的 0 是示例
uv run v360 list --channel 0 --start 2026-10-29T00:00:00 --end 2026-10-29T23:59:59
uv run v360 recordings
uv run v360 download RECORDING_ID
```

设备密码、设备命令 user ID 和 App 网页登录身份是不同信息。不能只凭“App 已登录”就假定这些参数都已取得。连接模式保留可配置；默认 0 尚未被用户硬件验证。

此设备的实际私有配置使用 LTE 连接模式 94。其目录响应包含异常时间条目，因此在该私有配置中显式启用 `allow_invalid_record_dates: true`：只索引可验证的时间，返回异常数和 `metadata_complete: false`，并在 `data/protocol-diagnostics/` 私有保存最多四个异常响应。其他设备默认仍严格拒绝异常日期。下载字节、顺序和解码检查不受此选项影响。

## 本地 API

```bash
uv run v360 serve --port 8080
```

监听 `127.0.0.1:8080`。API 说明在 `/docs`；`/health` 为基本存活检查，其余业务接口需要 `.private/web.json` 中的令牌。手机桥的令牌与网页 API 令牌分别管理。

启动后默认启用本地轻量自动保存：约 30 秒后首次检查，此后每小时一次，每轮最多 2 段/8 MiB、每天下载额度 16 MiB，总量上限 128 MiB。关闭网页不会停止，关闭后端会停止。可在历史录像页暂停或切换 10 GiB 服务器档。不会改变摄像头模式。详见 [自动保存与滚动保留](docs/automatic-archive.md)。

## 私有资料与运行数据

| 目录 | 内容 | Git |
|---|---|---|
| `src/`、`android/`、`tools/`、`tests/` | 我们编写的代码与检查 | 跟踪 |
| `.private/` | SDK、设备凭据、桥令牌、签名密钥 | 忽略 |
| `.tools/` | 项目局部 JDK、Android、媒体工具和日志 | 忽略 |
| `data/` | SQLite、下载中间文件、录像 | 忽略 |
| `dist/` | 本地生成的助手 APK 与构建证据 | 忽略 |

原始厂商 SDK 仅从用户输入包导入并用于本地构建，不包含在 Git 源码中。Android 助手不会替换原 App，也没有修改设备工作模式的接口。

新环境的工具准备与构建过程见 [构建说明](docs/build.md)。
