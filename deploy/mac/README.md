# Mac 后端部署（nex-stock，当前生产）

自 2026-11-08 起，FastAPI 后端和全部定时任务都跑在一台常开的 Mac 上，以原生 Python 3.11 进程运行，由 launchd 守护。前端静态文件和 nginx 仍在首尔（见 `deploy/seoul/README.md`）。东京的 Docker 部署（`deploy/tokyo/README.md`）保留为备用和回滚方案。

```
浏览器 --https--> 首尔 nginx (stock.nexorai.com.cn)
                   ├─ /            静态 SPA
                   └─ /api/        --ZeroTier 内网--> Mac <MAC_ZEROTIER_IP>:8000（launchd 守护的 python main.py）
                                                        └─ 每个请求带 user_session Cookie -> https://user.nexorai.com.cn/v1/session
```

进程命令与东京容器相同：`python main.py --serve --require-serve --schedule --no-run-immediately`。`--require-serve` 让 Web/API 端口被占用时最多等 30 秒重试，仍起不来就退出，由 launchd（KeepAlive）重新拉起，不会留下一个只跑调度、没有 API 的进程。一个进程同时承载 Web/API 和全部调度任务；飞书长连接只启动一次（参数含义见 `deploy/tokyo/README.md`）。

## 为什么用原生进程，不用 Docker

- **网络**：colima 里的容器端口没法直接只绑在宿主机的 ZeroTier IP 上，还要额外配端口转发。原生进程可以直接 `--host <MAC_ZEROTIER_IP>`，只在内网监听。
- **数据库**：SQLite（WAL 模式）放在 colima 与宿主机的共享目录里，有锁冲突的风险。原生进程的数据就在本地 APFS 磁盘上。
- **版本一致**：Python 3.11 与镜像（`python:3.11-slim`）一致。
- **代价**：本机没有 wkhtmltopdf 和镜像里的 CJK 字体，报告转图片（`MARKDOWN_TO_IMAGE_CHANNELS`）不可用。生产环境目前没有启用这个功能。

## 前提

- macOS（已在 Apple Silicon 上验证），装有 Homebrew 的 `python@3.11` 和 `git`。
- 已加入 ZeroTier 内网，`ifconfig` 里能看到本机的 ZeroTier IP（下文写作 `<MAC_ZEROTIER_IP>`）。
- Mac 常开、插电、不休眠（在系统设置里关闭休眠，或用 `caffeinate`），并保持用户登录：launchd 用户代理在登录后才会启动。

## 1. 目录、代码与 Python 环境

```bash
R=$HOME/nex-stock
mkdir -p "$R"/{config,data,logs,reports}
git clone --branch main <仓库地址> "$R/app"        # 生产只从 main 部署
# 代码里的相对路径 data/、logs/、reports/ 指向部署根目录，与容器的 /app/data 布局一致
for d in data logs reports; do ln -s "../$d" "$R/app/$d"; done
/opt/homebrew/bin/python3.11 -m venv "$R/venv"
"$R/venv/bin/python" -m pip install -U pip
"$R/venv/bin/python" -m pip install -r "$R/app/requirements.txt"
```

## 2. 配置

- **配置文件**：`$R/config/.env` 的键名与 `deploy/tokyo/env.production.example` 相同，包括 `AUTH_MODE=user_center`、`PUBLIC_ORIGIN`、`TUSHARE_TOKEN` 等。创建后执行 `chmod 600 "$R/config/.env"`。
- **路径不写进 `.env`**：数据库、日志路径和监听地址由 launchd 的环境变量指定。不要在 `.env` 里写容器路径（`/app/...`）。
- **设置页**：Web 设置页保存配置时会写回这个文件。
- **自定义选股策略**：`SCREENING_EXTRA_STRATEGIES_DIR=data/screening_strategies` 指向 `$R/data/screening_strategies/`。

## 3. 安装 launchd 守护

```bash
ZT_IP=<MAC_ZEROTIER_IP>
P=$HOME/Library/LaunchAgents/cn.nexorai.nex-stock.plist
sed -e "s#__HOME__#$HOME#g" -e "s#__ZT_IP__#$ZT_IP#g" "$R/app/deploy/mac/cn.nexorai.nex-stock.plist" > "$P"
plutil -lint "$P"
launchctl bootstrap gui/$(id -u) "$P"
```

`cn.nexorai.nex-stock.plist` 里的设置：

- `RunAtLoad`：登录后自动启动。
- `KeepAlive`：进程退出后 30 秒内自动重启。ZeroTier 晚于进程就绪导致绑定失败时，也会自动重试。
- `ENV_FILE`、`DATABASE_PATH`、`LOG_DIR`：指向部署根目录。
- `WEBUI_AUTO_BUILD=false`：前端由首尔提供，进程不再构建。
- `TZ=Asia/Shanghai`：Mac 的系统时区可能不是北京时间，调度时间按这个变量解析。

## 4. 检查

```bash
curl -fsS http://<MAC_ZEROTIER_IP>:8000/api/health                                             # {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' http://<MAC_ZEROTIER_IP>:8000/api/v1/history          # 期望 401
launchctl print gui/$(id -u)/cn.nexorai.nex-stock | grep -E "state =|pid ="                   # running
grep -E "AUTH_MODE=user_center|runtime scheduler" "$R"/logs/launchd.out.log | tail -3
```

还要在首尔主机上执行 `curl -fsS http://<MAC_ZEROTIER_IP>:8000/api/health`，确认内网能连通；然后把首尔 nginx 的 upstream 指向这台 Mac，步骤见 `deploy/seoul/README.md` 的「切换后端」。

## 日常运维

```bash
tail -f "$R"/logs/launchd.out.log                                  # 进程日志；应用日志在 $R/logs/stock_analysis_*.log
launchctl kill SIGTERM gui/$(id -u)/cn.nexorai.nex-stock          # 重启：旧进程退出后 KeepAlive 自动拉起新进程
grep "FastAPI 服务已启动" "$R"/logs/stock_analysis_$(date +%Y%m%d).log | tail -1   # 确认新进程的 API 已起来
launchctl bootout gui/$(id -u)/cn.nexorai.nex-stock                # 停止（重新启动用上面的 bootstrap）

# 升级（只从 main）
cd "$R/app" && git pull --ff-only
"$R/venv/bin/python" -m pip install -r requirements.txt
launchctl kill SIGTERM gui/$(id -u)/cn.nexorai.nex-stock
```

不要用 `launchctl kickstart -k` 重启：它可能在旧进程还占着 8000 端口时就启动新进程。没有 `--require-serve` 的旧版 plist 下，新进程会记录「启动 FastAPI 服务失败: FastAPI port is not available」后只跑调度和飞书，API 一直不可用；改了 plist 后要重新 `bootout` + `bootstrap` 一次才会生效。

前端单独发布到首尔（`deploy/seoul/README.md`），Mac 进程不提供对外的前端。

**备份**：先停掉服务，再复制 `$R/data`（SQLite 数据库，含 `-wal`/`-shm`）和 `$R/config/.env`。也可以不停服务，用 Python `sqlite3` 的 `backup()` 在线备份数据库。

## 迁移与回滚

**同一时间只能有一个后端实例在运行**，否则定时任务会重复执行（启用飞书长连接时，长连接也会重复）。

从东京迁到 Mac 的步骤（2026-11-08 实际执行过）：

1. 在东京执行 `docker compose stop stock`。容器策略是 `unless-stopped`，手动停止后，主机重启也不会再拉起。
2. 打包东京的 `config/`、`data/`（不含 `data/backups`）和 `reports/`，传到 Mac 的 `$R/` 下，核对两边 `stock_analysis.db` 的 sha256。
   - 东京到家宽的 ZeroTier 中转可能只有几十 KB/s。可以先让首尔从东京拉取（云到云很快），再转到 Mac。
   - 传完删除中转机上的临时包，包里有 `.env`。
3. 在 Mac 上执行 `chmod 600 config/.env`，然后启动服务并做第 4 节的检查。
4. 把首尔 nginx 的 upstream 改成 Mac：先备份，`nginx -t` 通过后只 reload。

回滚到东京：

1. 先停掉 Mac 上的服务，并把 Mac 上的新数据同步回东京。东京的数据还停在迁移那一刻。
2. 在东京执行 `docker compose start stock`。
3. 首尔恢复 nginx 配置备份（或把 upstream 改回东京），然后 reload。

## 注意

- Mac 休眠、断网或 ZeroTier 掉线时，`/api/*` 不可用，首尔会返回 502 或 504；静态页面不受影响。
- 首尔到 Mac 的内网往返约 30 ms，与原来到东京相当。国内用户在连接复用时，接口延迟约 50–70 ms。
- 回滚到东京前先读 `deploy/tokyo/README.md` 开头的资源警告：东京主机内存小、没有 swap，不要直接在上面构建镜像。
