# Docker 部署与升级

源码由 GitHub 管理，镜像通过 Docker Hub 的 `yungzzi/v360-web` 仓库分发。当前通过私有镜像仓库分发；本地开发电脑构建 Linux 镜像，云服务器拉取并运行。项目已使用此方式部署；新机器仍须单独验收网络、资源和设备兼容性。

## 文件与资源

`compose.yaml` 只运行一个后端实例，宿主机仅发布 `127.0.0.1:8080`，由已核对的 HTTPS 入口转发。容器内监听 `0.0.0.0:8080`，不启用网页关机接口。前端、FastAPI、后台调度、QEMU 和 FFmpeg 在同一资源组：1 CPU 时间额度、768 MiB 内存、同额 memory+swap（不借用 swap）、256 进程上限；非 root、默认 capabilities 全部移除、根文件系统只读，`/tmp` 为限额 64 MiB 的临时内存文件系统，使用量也计入内存。日志最多 3×10 MiB。停止宽限 2 分钟；后台和手动任务收到取消信号后释放自有进程。资源限额需要实际机器内核支持，不能只以配置文件作为验收。

数据是 Bind Mount：`/srv/v360-web/data` → `/app/data`，包含 SQLite、原片和回放缓存。`config` 和 `runtime` 单独只读挂载。完整 SQLite 目录而非单个数据库文件被挂载，以支持日志侧文件。目录须预先建立，Docker 不会替你创建误拼路径。文件归属必须匹配 `.env` 的 `V360_UID/GID`，私有 JSON 为 600，目录为 700。

`.env` 只保存镜像版本、路径、UID/GID、端口和 HTTPS Cookie 开关，账号密码不放这里。生产 `V360_SECURE_COOKIES=1`；仅本机 HTTP 验收设为 0。反向代理模板位于 `deploy/nginx-location.conf.example`，需要适配已有 HTTPS 入口，不能直接覆盖其他站点。`V360_TRUSTED_PROXIES` 仅按实际代理地址设置。

服务器档的原片、网页版本及直播缓存合计 15 GiB，另有系统 8 GiB 空闲底线；由应用控制，不是 Bind Mount 或 Docker 的磁盘硬配额。首次准备默认暂停自动保存，仍是本地轻量档；验收后在“管理与诊断”中选择服务器档并开启。批次发布是独立持久开关，新数据库默认关闭，见 [批次发布](scheduled-publication.md)。镜像、旧版本、构建缓存、备份及其他应用另计；先核实总磁盘预算，不因容器化而增加额度。

## 本地构建与私有目录准备

在 WSL 工程根目录操作，`.private`、`.tools`、data、Git 历史均被构建白名单排除。镜像只包含源码、锁定的 Python 依赖、系统 QEMU 和视频处理工具，不包含设备配置、录像或原厂 SDK。基础镜像固定 digest；Python 依赖来自 `uv.lock` 导出且校验 hashes，导出命令为 `uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file deploy/requirements.txt`。系统包接收 Debian 安全更新；下载慢时可用 `--build-arg APT_MIRROR=https://mirrors.ustc.edu.cn` 指定 Debian 官方列表中的镜像站，保留仓库签名校验，且不改整机配置；发布时以最终镜像 digest 和 Git revision 确认版本。

```bash
sudo docker build --build-arg VCS_REF="$(git rev-parse HEAD)" -t yungzzi/v360-web:VERSION .
.venv/bin/python tools/prepare_deployment.py .private/deployment --config .private/device.json
sudo docker compose --project-directory .private/deployment config --quiet
```

准备工具拒绝覆盖已有目录；新目录内包含 config、runtime、data、backups、Compose 和 release 工具。它复制已有本机原厂 SDK/Android 运行库和我们已经编译的 ARM64 worker，生成 hash 清单；不复制录像，也不连接摄像头。worker 源码变化时须先通过原有 `tools/probe_phone_free.py --build-worker` 重建并验收，再创建匹配的运行目录。私有运行依赖要单独迁移，不能推到 GitHub 或 Docker Hub。库版本变化时另做 runtime 版本备份和兼容验证。

准备工具生成的 `.env` 默认使用 `v360-web:deployment-dev`、本机绝对目录、当前 UID/GID、18089 端口和本地 HTTP Cookie 模式。根据要验证的本地镜像修改它，再启动：

```bash
sudo docker compose --project-directory .private/deployment up -d
curl http://127.0.0.1:18089/ready
sudo docker compose --project-directory .private/deployment logs --tail=100 v360
sudo docker compose --project-directory .private/deployment stop v360
```

`/ready` 只检查数据库可读取、进程状态和调度线程存活，返回构建 revision，绝不唤醒电池摄像头；它不证明设备在线、目录完整或录像已同步。健康状态 unhealthy 不会仅凭自身触发 Compose 自动重启；需要看日志/告警，摄像头离线不应成为重启整个服务的理由。`unless-stopped` 管理进程退出重启，不能修复坏配置；持续重启时先停止排查。进程锁跨容器保护同一 data 目录；不同 data 目录仍不能同时接入同一摄像头，只有一台摄像头生产服务可以在线。

## Docker Hub 发布与首次云部署

1. 确认 Docker Hub 私有仓库 `yungzzi/v360-web` 可用并完成本地登录；浏览器设备码登录可用于手工发布。服务器单独登录，推荐只读令牌。不要把凭据传进 Dockerfile、Git 或命令参数。
2. 功能分支检查与本地容器验收通过后，`--no-ff` 合并 main，按需建立发布 tag（不是构建或部署的必要条件）。构建时记录实际 Git revision，使用独立版本标签，发布后记录镜像 digest，不重复覆盖已发布标签。
3. 本地执行 `docker push yungzzi/v360-web:VERSION`。
4. 先只读核对服务器架构、Docker/cgroup 支持、其他服务峰值、磁盘/备份预算、Docker Hub 可达性、既有端口和 HTTPS。容器内摄像头连接成功不能从 WSL 宿主机结果推断，云端网络也要重新验收。
5. 通过 SSH/SFTP 受控迁移 config/runtime、部署配置和工具至 `/srv/v360-web`；首次可用新建的空 data，若要迁移本地录像，须停止源服务后统一复制数据库及原片。设置匹配非 root 用户的权限。服务器 `.env` 改为实际路径、镜像版本、端口以及 Secure Cookie=1。
6. `docker pull yungzzi/v360-web:VERSION`，再 `docker compose up -d`；确认 `/ready`、构建 revision、容器资源限额和进程实际用量。先小量检查直播、云台和 SD 取片，避免持续唤醒摄像头。
7. 在已有 HTTPS 入口添加经过核对的转发配置，测试远程登录、WebSocket、播放和下载认证。资源和其他应用表现正常后，再开启服务器档自动保存。

## 更新

GitHub 是源码管理和协作入口。当前发布在本地构建，再 push Docker Hub；服务器 pull 镜像，因此技术上不要求每次先 push GitHub。仍应同步已检查的源码，保证版本可追溯。Compose/工具/runtime 有变化时单独更新它们；镜像不能替代这些宿主机文件。

常规应用更新主要替换镜像；若该版本同时改变 Compose、宿主机部署工具或 runtime，也须迁移经过检查的对应文件，不能只更新镜像。服务器不原地修改容器源码。提前 pull 后，只替换 V360 服务，保留 Bind Mount 数据。部署目录内的 `tools/release.py` 仅需宿主机 Python3 标准库：

```bash
python3 /srv/v360-web/tools/release.py --home /srv/v360-web --image yungzzi/v360-web:NEW_VERSION
```

先在管理状态中确认无观看，暂停自动保存并等待下载/网页准备任务退出；更新后检查并恢复原策略。升级会短暂停机，不并行启动新旧两个生产服务。

工具检查明确版本、镜像 revision、专用挂载和回环端口；先拉取新镜像，成功后停止旧服务，拒绝继续处理被强杀的旧容器；使用旧镜像的备份工具制作并验证数据库/配置快照，再原子更新 `.env` 镜像名称、重建单个服务并检查 `/ready` 的新 revision 和调度状态。每次记录 old/new 镜像和快照到 backups 下。新服务未就绪则停止它并保留记录，不自动把旧程序套到可能已改变的数据库上。真实功能检查仍应在更新后执行，`/ready` 不是摄像头验收。

`--local-image` 跳过 pull，仅在目标机器已经单独拉取并核对目标镜像时使用，也可用于本地演练。默认更新会先 pull；不要用它绕过目标镜像核对。首次启动用 Compose，不用更新工具。

## 备份、回退与恢复

备份包含 SQLite 一致快照、设备/网页配置和 hash 清单，**不包含录像字节和私有 SDK**。同盘备份只保护误改/升级，不保护整盘故障；需要保护的录像应另行做有预算的异机备份。缓存可重建，无需备份。先保留最近 7 份元数据快照，旧快照人工确认后清理；工具不擅自删除任何备份或原片。

```bash
cd /srv/v360-web
sudo docker compose -f compose.yaml -f compose.ops.yaml run --rm --entrypoint python v360 -m v360_web.operations backup --data /app/data --config /app/config --backups /app/backups
```

可以在线执行 SQLite Backup API 快照；版本更新仍先停止任务，确保文件和元数据的对应关系易于核对。备份含设备密码和访问令牌，目录/文件权限分别为 700/600，异机保存时加密并单独管理权限。

纯程序回退且数据库/runtime 兼容时，可用更新工具指定已保留的上一稳定镜像版本。涉及不兼容数据库结构时，先停服务，使用下面的离线恢复方式；恢复旧库可能遗漏新录像索引，不应自动重启后立即清理。

```bash
# 将路径、UID/GID、镜像版本与 SNAPSHOT 替换为实际值；先 docker compose stop v360。
sudo docker run --rm --network none --read-only --user 1000:1000 \
  --mount type=bind,source=/srv/v360-web/data,target=/app/data \
  --mount type=bind,source=/srv/v360-web/config,target=/app/config \
  --mount type=bind,source=/srv/v360-web/backups,target=/app/backups \
  --entrypoint python yungzzi/v360-web:VERSION -m v360_web.operations restore \
  --data /app/data --config /app/config --backups /app/backups --snapshot /app/backups/SNAPSHOT
```

恢复工具拒绝在线服务、损坏备份和缺少对应原片的快照；先保存恢复前的安全备份，再替换数据库与配置。不会删除录像，自动保存策略强制暂停。核对旧版本兼容性、缺失/额外录像索引后再选择镜像启动，人工恢复自动保存。旧数据库格式尚无通用自动降级能力。

## 运维

在 `/srv/v360-web` 中运行 Compose 命令，或显式传 `--project-directory /srv/v360-web`，否则当前目录可能找不到配置：

```bash
cd /srv/v360-web
sudo docker compose ps
sudo docker compose logs --tail=100 v360
sudo docker stats --no-stream
```

`compose.yaml` 是常驻服务配置；`compose.ops.yaml` 仅在手动元数据备份时叠加，禁用该临时容器网络并增加 backups 挂载，不会启动第二个后端。监测服务器剩余空间、应用峰值内存、OOM/重启次数、最近成功保存时间和流量；不能把 Docker stats 的瞬时值等同长期峰值。只读检查限制值与实际 cgroup 一并确认。

保留当前和上一稳定镜像，按明确镜像 ID 人工清理更旧版本，不执行全局 `docker system prune` 或卷清理，以免影响同机其他服务。Compose 更换与停止不会删除 Bind Mount 文件；恢复和清理仍需遵守项目的录像保留规则。

## 历史本地验收记录（2026-10-31）

以下为当次样本，不是当前镜像大小、测试数量或云端状态。

112 项软件检查通过。Docker Desktop 的 Linux 容器完成真实摄像头认证/目录查询、两段原片保存、回放准备及 Range=206、短时 WebSocket 直播；最终一次首个视频数据约 1.49 秒返回。目录有 171 个有效条目，仍报告不完整。精简镜像约 564 MiB，只包含所需 QEMU 模拟器和 FFmpeg 共享库依赖；未包含私有挂载目录。

内核 cgroup 实测 `cpu.max=100000 100000`、`memory.max=805306368`、`memory.swap.max=0`、`pids.max=256`；该轮峰值内存约 149 MiB。版本替换前自动备份，替换后通过 revision/readiness 检查，SQLite 和原片继续保留。在线恢复被实例锁拒绝；停机后恢复成功，全部原片 hash 不变，自动保存保持暂停。Uvicorn 完成 shutdown，容器通过 SIGTERM 退出码 143 结束，未 OOM；不能将正常 SIGTERM=143 误报成强杀=137。

这一次实测只覆盖本地 Docker Desktop、一台摄像头和有界任务；后续已完成 Docker Hub 推拉与云端部署，但仍不能宣称完整目录同步、独立深睡唤醒或长期资源峰值已通过。数据见 [脱敏验收记录](evidence/docker-deployment-validation.json)。

## 复用现有 HTTPS 的子路径

部署在现有网站的 `/v360/` 下时，将 `.env` 的 `V360_ROOT_PATH=/v360`，使用 `deploy/nginx-subpath.conf.example` 作为已有 HTTPS server 内的 include。Nginx 向应用转发时去掉前缀；Uvicorn 通过 root_path 提供公开路径信息。页面从脚本 URL 推导前缀，API、直播 WebSocket、回放、原片和 Cookie 都保持在 `/v360/` 内。原有根路径部署仍受支持。前缀只能由简单字母、数字、横线、下划线组成，不带结尾斜杠。

本地实际 Nginx 代理与 Edge 验收通过：页面登录、历史文件 Cookie 回放（15.048 秒、readyState=4）、640×360 实时画面（readyState=4），短时观看后主动断开；114 项软件检查通过。服务器上的原有站点须先备份配置，检查没有冲突路由并通过 `nginx -t` 后再做平滑 reload。
