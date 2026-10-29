# 无实体手机的本地 PPCS 后端

2026-10-29 已验证并设为本机默认适配器：`native-arm64`。本阶段不依赖实体手机、ADB、Windows Python 或完整 Android 系统；使用 QEMU user-mode 加载私有的 ARM64/Bionic 库，仍属于 Android 库兼容运行。未取得原生 glibc Linux SDK。

## 结构

```text
FastAPI / Python CameraClient
  → 私有二进制 stdin/stdout 管道
  → QEMU user-mode 中的隔离 ARM64 工作进程
  → 原始 PPCS C API
  → 摄像头
```

工作进程每次只拥有一个 SDK 会话。控制参数与设备密码不进入命令参数；SDK 输出转到 stderr，工作进程输出协议走独占管道。启动器关闭 core dump。Python 对管道收发设置截止时间；正常关闭先请求 ForceClose 和 DeInitialize，异常或卡死时终止自己启动的进程组。异常强制退出与 SDK 正常退出分别记录，不能混为一谈。

工作进程没有创建 RPC 网络监听端口。普通用户即可运行，不使用 KVM，不安装全局 binfmt，也不改变系统或手机网络设置。API 的网页访问认证仍独立保留。

## 已完成的真实验证

- 停止项目手机助手，并移除它的 `tcp:8766` USB 转发；原 V360 Pro App 未修改。
- 无手机桥令牌、无 Windows Python 配置的客户端完成真实设备认证。
- 服务层实际取得 101 条有效目录记录，另有 1 条无效日期，继续报告目录不完整，并私有保留异常响应。
- 服务层实际下载另一个 SD 片段：1,452,034 字节，文件长度/包序/结束标志验证通过，FFprobe 和 FFmpeg 整段解码通过。
- 生成 H.264/AAC 兼容版，探测时长 15.060 秒。
- 真实文件经本机 FastAPI 验证：无认证 401，有认证 200，内容 SHA-256 一致；状态接口正确报告 `native-arm64`。
- 原生认证及目录检查后，ForceClose、DeInitialize 返回 0，工作进程已退出。

这些是本机 WSL 的验收。原 App 后台在线状态未隔离，因此“摄像头深睡后的完全独立首次唤醒”仍需单独实验。没有据此宣称阿里云网络、CPU/内存预算或长期无人值守运行已经验收。原厂连接服务的依赖也未消除。

## 当前使用

已有私有运行文件、SDK 和设备参数的本机可直接运行：

```bash
cd /home/yungzzi/workspace/04_tools-and-scripts/v360-web
.venv/bin/v360 bridge-check
.venv/bin/v360 connect-check
.venv/bin/v360 recordings
```

`bridge-check` 是保留的诊断命令名，当前输出 `adapter: native-arm64`；无需开手机助手或执行 `phone.ps1`。手机可以断开电脑，USB 调试可关闭。此前 USB 配置只在 `.private/device.usb-reference.json` 中保留，供受控回归时使用，不自动回退。

重新构建工作进程：

```bash
.venv/bin/python tools/probe_phone_free.py --build-worker
```

私有 SDK、运行库和设备配置仍不进入 Git。只复制 Git 源码到新机器不足以启动，需要通过受控方式迁移这些私有依赖，适配目标系统后再验收。
