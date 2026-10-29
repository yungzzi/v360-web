# 无手机实时取流：短时验证版

2026-10-29，本机 WSL 的 `native-arm64` 后端已从真实 4G 摄像头取得实时视频。这个功能建立实时开流、解帧和转码基础，当前输出有限时长的预览文件；持续网页直播播放器尚未实现。

## 使用

在已有私有设备配置、SDK 和运行库的本机执行：

```bash
cd /home/yungzzi/workspace/04_tools-and-scripts/v360-web
.venv/bin/v360 live-capture --seconds 8
```

默认选择原 App 的 `STREAM_MIDDLE=2`，本机返回 640×360。参数范围为 1–60 秒，分辨率选项为 0–3；其他选项的实际分辨率未验收。该命令会正常开启实时视频，消耗电量和流量，也可能触发设备原有的观看录像行为。结束或异常时请求停止视频，并由连接上下文关闭会话；未修改设备工作模式、录像设置或固件。

私有输出在 `data/live/<id>/`：原始 `video.hevc`、转换后的 `preview.mp4` 和 `capture.json`。仅完成解析、FFprobe 识别、原始整段解码、转码及预览整段解码后才生成 `state=verified` 清单。失败文件保留用于排查，API 不提供它们。当前未实现自动清理，取流也没有写入 SD 录像索引。

## API

沿用 `Authorization: Bearer <本地网页令牌>` 鉴权，以及已有的单摄像头串行任务锁：

- `POST /api/jobs/live`，JSON 为 `{"seconds":8,"resolution":2}`，返回任务 ID。
- `GET /api/jobs/<任务ID>`，完成后读取 `result.id`。
- `GET /api/live/captures/<result.id>/file`，提供 H.264 MP4，支持 Range。

接口使用已有网页令牌，不使用摄像头账号密码作为网页密码。CLI 服务当前仍只绑定本机回环地址；这里没有配置公网 HTTPS 或部署到阿里云。

## 协议依据与真实结果

REA 对同一 SHA-256 原包的静态分析确认：

- `P2pSDK.startLive` 命令 1；`IOCTRL_LIVE.createBuffer` 的五参数变体产生 20 字节请求，依次写分辨率、仅视频类型 0、镜头 0，用户标识低 16 位在偏移 18。
- `stopLive` 命令 3，载荷为四个零字节。
- `keepDeviceLive` 命令 914，载荷为小端整数 10；App 在观看期间周期调用。未把它当作独立冷唤醒机制。
- `LiveAVFrame.setHeadData` 定义 32 字节媒体头；`LiveVideoChannel.getLiveAVFrame` 分别完整读取头和载荷。
- 原包 DEX 中 `Connect.lambda$readLiveData$7$ppcs-sdk-connect-Connect` 给主直播通道传入 2；`AVFrame` 常量定义帧标志。该通道初始化与常量通过本地 DEX 读取核对，未将它标成 REA 运行时观测。

完整 REA 输出保存在 Windows 分析目录 `analysis/2026-10-29/live/results/`，不复制第三方反编译全文到源码仓库。

第一次独立探测收到 178 帧，原生 SDK ForceClose、DeInitialize 返回 0。随后通过正式 CLI 开流约 8.222 秒，收到 118 帧、185,146 字节，帧序连续；原始 HEVC 和 H.264 预览均整段解码通过，预览 7.866667 秒。真实预览经本机 FastAPI TestClient 验证：无认证 401、有认证 200 且 hash 一致、Range 206 且字节一致。这是进程内 HTTP 验收，尚未做浏览器播放器验收。

此设备的编码标识为 5（HEVC），但帧类型使用 1/2，因此不能只凭帧类型推断 H.264。当前只接受该已验证的 HEVC 标识、主子通道 0 和 Annex B 数据；遇到其他编码/子通道、丢帧、非法长度或存储上限即失败并清理连接。

帧头原始 PTS 的变化粒度在这次样本中为秒，未将其误作毫秒。预览按 FFprobe 识别的码流帧率 15 fps 重新构造播放时间轴，保留原始起止 PTS 用于后续研究；尚未实现精确帧时间戳和音画同步。此阶段仅请求视频，不开启麦克风。

脱敏实测摘要见 [取流验证记录](evidence/live-capture-validation.json)。后续仍需持续播放器、云台操作、断线恢复、深睡首次连接、保留期清理和阿里云验收。
