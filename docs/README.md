# 文档导航

当前使用方式以根目录 [README](../README.md) 和下面的说明为准。软件检查、单次实机试验与长期验收分开看；不能把某次测试数量或性能样本当成当前运行状态。

## 当前说明

| 场景 | 文档 |
| --- | --- |
| 浏览历史、下载和失败补取 | [历史录像](recording-library.md) |
| 自动下载、处理、重试及清理 | [自动保存](automatic-archive.md) |
| 批次何时可看 | [半小时批次发布](scheduled-publication.md) |
| 时间轴和快照 | [时间轴交互](history-timeline.md) |
| 登录、中文密码和恢复 | [访问密码](browser-password.md)、[登录保持](browser-sessions-responsive.md) |
| 声音、电量、云台、镜头 | [声音](live-audio.md)、[电量与云台](automatic-battery-ptz.md)、[三画面](multi-lens.md) |
| 构建与设备接入 | [工具准备](build.md)、[无手机后端](native-backend.md)、[账号导入](account-onboarding.md) |
| 部署、更新和备份 | [Docker 运维](docker-deployment.md)、[资源预算](resource-budget.md) |
| 性能与界面设计 | [回放性能](history-performance.md)、[家庭界面](family-interface.md) |
| 尚未解决的事项 | [已知限制与后续方向](next-features.md) |

## 历史实验与开发记录

以下文档保留用于追溯，不是新的部署步骤或当前产品承诺：

- [首版验收记录](validation.md)：早期手机桥、SD 片段和本地网页验证。
- [无手机可行性探针](phone-free-probe.md)、[初期实时取流](live-capture.md)、[初期网页直播](browser-live.md)、[初期电量分析](battery.md)。
- [旧版三十分钟全部就绪方案](archive-readiness-plan.md)：目标已由批次发布和有限重试替代，不是待执行计划。
- [Android 助手设置](phone-setup.md)：仅用于受控对照，日常运行不需要手机。
- [脱敏证据目录](evidence/)：文件日期、样本数、测试数和镜像版本均属于当次记录；不据此推断目录完整或当前设备状态。

2026-11-04 将历史提交邮箱替换为 GitHub 隐私邮箱，提交编号随之改变。历史记录中的旧源码编号仍表示当时镜像的构建信息；镜像本身未改变。原历史和新旧编号对照保存在维护者私有备份，不进入仓库。
