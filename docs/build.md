# 本地工具与 Android 助手构建

开发环境为 WSL Ubuntu x86_64。使用已经安装的 `uv`。开发脚本可将额外工具局部解包到 `.tools`，不要求修改全局工具链。日常原生后端不需要 Android 助手；本页 Android 构建仅用于可选的对照路径。

## Python 与媒体工具

```bash
uv sync --extra dev --locked
python3 tools/bootstrap_media.py
```

媒体脚本利用当前 Ubuntu 的包元数据解析依赖，`apt-get download` 下载仓库校验的包，再用 `dpkg-deb -x` 局部解包。生成的包装器只对媒体子进程设置本地库路径，具体版本及 SHA-256 保留在 `.tools/media-provenance.json`。

## 局部 JDK 与 Android 工具

初次准备 JDK：

```bash
mkdir -p .tools/downloads .tools/sysroot
(cd .tools/downloads && apt-get download openjdk-17-jdk-headless openjdk-17-jre-headless)
for package in .tools/downloads/openjdk-17-*.deb; do
    dpkg-deb -x "$package" .tools/sysroot
done
python3 tools/bootstrap_android.py
python3 tools/bootstrap_android.py --windows-adb
```

Android 下载自 Google 官方仓库，选择 API 34、Build Tools 35.0.0 和仓库元数据指明的 Platform Tools，并核对仓库摘要；下载地址和 SHA-256 在 `.tools` 中保留。脚本还修正局部 JDK 指向项目内配置的链接。

原始 SDK 不从第三方 APK 站点获取。使用用户已提供、已分析的原包：

```bash
uv run v360 init-config
uv run v360 import-apk /absolute/path/to/the/original.apk
python3 tools/build_android.py
```

私有配置已经存在时，`init-config` 不会覆盖它。只有新环境需要首次导入；已有私有配置及 SDK 时不要重复覆盖。

构建脚本执行 javac、D8、aapt2、16 KiB ZIP 对齐和 APK 签名，然后执行 `apksigner verify` 与对齐检查。产物为：

- `dist/v360-usb-bridge-0.1.0.apk`
- `dist/android-build-evidence.json`

厂商 ARM 库来自用户本地原包；APK 为本地开发验证产物，不在 Git 中分发。签名私钥也只保留在 `.private/`。

## 配对门逻辑检查

`PairingGateCheck` 可在普通 JDK 中检查关闭状态、六位码格式、一次性使用、到期、五次尝试限制和重新开启。它不加载原生 PPCS 库，不属于 Android 真机测试。

## 已知边界

- 只编译了 ARM32/ARM64 的原厂 SDK 包装器，不支持原厂没有提供的 x86 模拟器 ABI。
- APK 构建和签名验证不证明手机会成功加载 SDK，也不证明厂商服务接受新客户端。
- Android 设备的后台策略、16 KiB 原生页兼容、原厂 SDK 行为需真机确认。
- 部分 Android 界面 API 目前有弃用编译提示；仍通过 API 34 编译。
