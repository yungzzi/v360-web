# 手机助手接入：开发验证步骤

这台手机放在你身边即可，不需要家人操作摄像头。第一步只检查手机 SDK 能否加载及 USB 收发，不接触摄像头。

## 1. 连接你身边的手机

1. 手机用 USB 数据线接到当前 Windows 电脑。
2. 开启开发者选项中的 USB 调试；入口随手机品牌不同。电脑请求调试授权时，在手机上确认。
3. 在 Windows PowerShell 中执行下面的命令。WSL 的 `\\wsl.localhost\...` 路径可能被 `RemoteSigned` 视为远程来源，因此这里为每次启动的子进程指定执行策略；不会修改当前用户或整台电脑的执行策略。每一步都是显式操作；脚本不会自动选择多台设备中的某一台。

```powershell
$project = '\\wsl.localhost\Ubuntu\home\yungzzi\workspace\04_tools-and-scripts\v360-web'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$project\tools\phone.ps1" -Action devices
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$project\tools\phone.ps1" -Action install
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$project\tools\phone.ps1" -Action open
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$project\tools\phone.ps1" -Action forward
```

如有多台设备，用 `-Serial` 明确选择。安装的包名是 `org.v360web.bridge`，与 V360 Pro 不同；现有 App 数据不参与安装。

如果安装时手机出现 USB 安装确认，解锁手机并确认安装本项目的助手。只有出现 `Success` 后才继续下一步；`unauthorized` 表示还需在手机上确认 USB 调试授权。

转发目标只有手机回环端口 8766。脚本使用 `--no-rebind`，不会直接覆盖其他已占用的转发。

## 2. 配对与基础检查

1. 手机助手点 **Start bridge**。系统可能询问通知权限。
2. 在 WSL 中先准备运行 `uv run v360 pair-bridge`。
3. 手机点 **Pair this computer (30 seconds)**，将六位码输入 WSL 终端。有效期 30 秒，最多五次尝试，成功后立即失效。
4. 执行基础检查：

```bash
cd /home/yungzzi/workspace/04_tools-and-scripts/v360-web
uv run v360 pair-bridge
uv run v360 bridge-check
```

成功时应返回协议版本、SDK API 版本和当前会话数量。配对令牌自动写入本地私有配置，不会打印出来。`bridge-check` 不初始化摄像头连接，不使用设备密码。

实际结构是：WSL 程序 → Windows Python 私有标准输入/输出管道 → Windows ADB 的回环转发 → 手机助手 → PPCS 库。

这套 WSL/Windows 管道已使用本机测试对端验证，手机端 SDK 加载仍须完成本步骤后确认。

## 3. 摄像头验证是后续独立步骤

需要确定准确型号、固件、实际通道、正常设备密码及适用的用户标识。它们在本地私有配置中填写。

先做短时连接和单个片段下载；不要先切换常开模式。原 App 可以正常回放是有用对照，但还不能替代新客户端的验证。

## 4. 结束

手机点 **Stop and release connections**。电脑可以移除对应转发：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$project\tools\phone.ps1" -Action remove-forward
```

本阶段手机是开发接入工具。长期部署时是否保留 Android 桥、放在哪里，仍需根据实际连接结果和服务器运行条件决定。
