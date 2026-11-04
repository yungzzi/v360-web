# 脱离实体手机：兼容运行可行性验证

> 历史范围：无手机后端形成前的可行性实验。文中的测试数量、待办和运行状态仅代表对应阶段。当前入口见 [文档导航](README.md)，当前规则见 [自动保存](automatic-archive.md) 与 [批次发布](scheduled-publication.md)。

日期：2026-10-29。开发分支：`feat/phone-free-transport-probe`。

## 两条路线

Linux SDK 路线：本次访问 CS2 官网超时，尚未取得并验证与 V360/BFEP 设备匹配的 Linux SDK。其他厂商的 PPCS/MiOT 库及面向其他设备的 Python 实现，不能凭名称判断为兼容替代品。该路线仍保留，不报告为已找到可用 SDK。

Android 兼容路线：当前 WSL 为 x86_64，用户没有 `/dev/kvm` 读写权限。使用项目局部 QEMU user-mode 执行 ARM64 ELF，并复用私有 Android Bionic 加载器及依赖库，无需启动完整 Android 系统或使用 KVM。原 PPCS 库保持不变，通过其导出的 C API 调用，不经过 JNI、ADB 或手机桥接端口。

这属于 Android 用户态库兼容运行，不是已经取得原生 glibc Linux SDK。QEMU 和交叉 binutils 来自 Ubuntu 包，下载后与本机 APT 索引 SHA-256 核对；包记录在 `.tools/phone-free/packages.json`。包括加载器在内的七个运行文件最初从已授权手机只读取得，保存在 `.private/android-runtime-arm64/`，来源及 hash 存在其 `provenance.json`。后续探针运行只使用这些本地文件，不访问手机；未变更手机设置或复制 App 私有数据。

## 已实际运行

```bash
cd ~/src/v360-web  # 替换为你的实际克隆目录
.venv/bin/python tools/probe_phone_free.py
.venv/bin/python tools/probe_phone_free.py --initialize
.venv/bin/python tools/probe_phone_free.py --connect
```

- 库加载：SDK API 版本 `84017664`，与手机端参考一致。
- 初始化/退出：`PPCS_Initialize` 和 `PPCS_DeInitialize` 均返回 0。
- 真实底层连接：`PPCS_ConnectByServer` 返回会话 1；`PPCS_ForceClose` 和退出均返回 0。
- 初始化参数、DID 和服务器参数仅通过进程 stdin 传递，原始输出私有保存；禁止 core dump。未发送设备登录、录像或设置命令。
- 加载器报告缺少 `/linkerconfig/ld.config.txt` 的警告，但未阻止上述调用；不据此认定全部 API 或长期运行兼容。

脱敏结果见 `docs/evidence/phone-free-probe.json`。这证明当前 WSL 环境中底层原库具有脱离实体手机执行和建连的可行性，尚未证明完整录像链路可用。

## 下一阶段的验收门槛

编写隔离的原生工作进程与 Python 适配器，接入初始化、连接、读写、关闭及超时回收。先通过正常设备认证，再列目录和下载一个片段，独立解码，与已有手机桥基线对照。验证期间关闭手机助手通道以排除回退依赖。

后续已完成读写适配器及真实 SD 片段闭环，正式本机配置已切换为 `native-arm64`，详见 [无手机后端说明](native-backend.md)。此前 USB 配置仅私有保留用于对照。阿里云网络、资源占用、休眠唤醒和重启恢复仍待在目标环境验收。

录像目标按用户确认收敛为设备实际产生的观看期间和动作触发片段，不要求全天连续录像。
