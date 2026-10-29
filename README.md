# V360 Web：开发验证版

工程目录：`/home/yungzzi/workspace/04_tools-and-scripts/v360-web`（WSL Ubuntu）。

目标：为用户自有的太阳能 4G 摄像头提供带身份认证的网页访问，支持按需直播、云台和 SD 录像同步。本阶段实现接入和录像同步的基础程序；完整网页、直播、云台、无人值守部署尚未实现。

## 当前已经实现

- 从已分析的 V360 Pro 7.2.0 APK 静态导入 SDK，校验原包 SHA-256；SDK 和初始化参数只存本地私有目录。
- 命令编解码、正常设备登录、分页录像目录、按设备/通道/时间请求下载。
- 部分读取、超时、开始/结束标记、包序号和声明文件长度校验。
- SQLite 录像索引、幂等发现、临时文件和“已下载但未验证”状态。
- FFprobe 检查和 FFmpeg 解码验证；只有通过验证的录像可经 API 访问。
- Bearer 鉴权 API：录像目录、文件访问、串行列目录/下载任务。任务暂存在内存，不会在进程重启后自动续跑。
- Android USB 助手：本机回环监听、一次性六位码配对、每条 RPC 的令牌认证、连接归属与断线释放。
- WSL 通过 Windows Python 私有管道访问 Windows 的 ADB USB 转发；不依赖 WSL 直通 USB，也不配置公开桥接端口。

**已完成真实手机配对、正常账号参数导入、摄像头认证、SD 目录查询及首个真实片段下载和完整解码。最近查询取得 95 条有效记录，另有 1 条异常时间记录，目录完整性仍标为 false。直播、云台、深睡唤醒和全天运行尚未验收。** 验证依据见 [开发验证记录](docs/validation.md)。

## Git 规则

用户要求：开发使用新功能分支，普通提交不得直接落在 `main`，功能完成后才合并，所有合并显式使用 `--no-ff`。

开发汇总分支：`feat/ppcs-recording-bootstrap`。最小 SD 片段闭环已验证；完整网页、直播、云台与长期部署尚未完成，继续保留开发分支。

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

## 下一步：只验证手机助手

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

此时不会自动开始同步或改变摄像头模式。启动同步任务需要显式调用对应 API。

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
