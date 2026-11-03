# 原客户端录像获取流程复核（2026-11-03）

本轮用REA/JADX重新追踪原APK；只做静态分析，没有修改设备或部署。
计划与逐方法证据：recording-flow-review.json、recording-flow-details.json、recording-flow-final.json及results目录同名label输出。

1. SD目录：PlayBackListViewModel.getPlayBack -> P2pSDK.getPlayBackListByDay，所选日00:00:00至23:59:59。目录仍有异常，不能宣称完整。
2. SD直接回放：PlayerFragment.startPlayBack -> ViewModel.startPlayBack -> P2pSDK.playVideoRecord。先启动回放读取，再发动作16；通道3，传目录时间与事件类型。旧平台event1/3/4映射selector1，其余2。这条路径不先请求完整MP4文件。
3. SD文件下载：ViewModel.startDownLoadFile -> P2pSDK.downLoadFile，先请求；33069应答仅在fileSize>0且未取消时调用startDownLoadFile启动读取。接收线程检查开始/结束标记，写文件，失败清理本地不完整文件。-1本身不说明被覆盖、SD异常或会话兼容问题。
4. 停止下载：ViewModel.stopDownLoad -> P2pSDK.stopDownLoadFile，33070/33071，4字节小端通道。静态引用仅见下载对话框lambda（需进一步确认具体UI触发）；不能据此声称每次正常结束都会调用停止。
5. 回放读取异常时，Connect设置dateErrorNeedResetConnect=true。这是连接恢复流程的进一步追踪入口，尚未证明能解决当前拒绝下载。
6. CloudTF播放器直接用账号服务提供的播放地址，独立于设备两条通路。此前本人登录查询相关日期返回空记录，当前不是已证实可用的替代来源。

判断：当前文件下载主要顺序与原App一致，未找到收到-1后仍能强制下载的后门。完整会话结束/异常恢复仍需对照。当前没有实机验证新增修复，不代表旧片已恢复。

下一步：补查dateErrorNeedResetConnect的使用处与回放读取/时间戳处理；用已成功、同日失败、旧日失败样本比较独立连接/连续下载/取消后下载；SD回放作为候选补救必须验证实际画面时间、时长、连续性。此前回放探针返回过错误时间画面，不能充作目标录像。
