# 手机助手接入：开发验证步骤

这台手机放在你身边即可，不需要家人操作摄像头。第一步只检查手机 SDK 能否加载及 USB 收发，不接触摄像头。

## USB 在本项目里的作用

原包提供的 PPCS 库用于 Android ARM 环境，当前 WSL 后端不能直接加载它。手机助手通过 JNI 调用该库，ADB 通过 USB 将电脑与手机助手的回环端口相连。现阶段摄像头命令、录像和未来视频的数据链路依赖这台手机。摄像头与手机之间仍使用互联网；手机无需位于摄像头附近。

ADB 还用于安装、启动助手以及检查原 App 的设备设置页信息。已经取得的 DID 存在本地私有配置，重新插线无需重新获取。账号登录和账号设备列表通过后端的 HTTPS 请求完成，不依赖 USB，也不是读取原 App 的私有数据库。自建网页的用户认证是另一个独立层。

手机桥目前用于开发验证。服务器独立运行的通信层及长期部署方案尚未完成，不能把当前 USB 接入等同于已经实现无人值守云端服务。

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

这套 WSL/Windows 管道已完成真实手机配对和 SDK 状态检查。2026-10-29 的设备底层连接记录见 `docs/validation.md`；重新接入时仍需执行 `bridge-check` 确认当前状态。

## 重新连接 USB 后

通常无需重新安装或配对。插线并确认调试授权，打开助手并在需要时点 **Start bridge**；在 Windows PowerShell 再执行前面的 `forward` 命令，然后在 WSL 执行 `uv run v360 bridge-check`。已有对应转发时脚本会提示已配置。

拔线或重启通常保留配对令牌。助手数据清除或令牌变化后才需重新配对。当前手机是开发桥接工具，拔线期间后端无法通过它访问摄像头。

## 3. 摄像头验证是后续独立步骤

需要确定准确型号、固件、实际通道、正常设备密码及适用的用户标识。它们在本地私有配置中填写。

先做短时连接和单个片段下载；不要先切换常开模式。原 App 可以正常回放是有用对照，但还不能替代新客户端的验证。

## 4. 结束

手机点 **Stop and release connections**。电脑可以移除对应转发：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$project\tools\phone.ps1" -Action remove-forward
```

本阶段手机是开发接入工具。长期部署时是否保留 Android 桥、放在哪里，仍需根据实际连接结果和服务器运行条件决定。
