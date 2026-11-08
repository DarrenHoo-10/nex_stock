# 东京后端部署（nex-stock，备用 / 回滚）

> **当前状态（2026-11-08 起）**：生产后端已迁到 Mac，见 `deploy/mac/README.md`。东京的 `nex-stock` 容器已停止，`/data/nex-stock` 下的代码和数据保留，用于回滚。
>
> **资源警告**：东京主机内存 3.8 GB、没有 swap，同机还运行 hindsight 等服务。在上面直接执行 `docker compose up -d --build` 曾导致整机失去响应，只能在 AWS 控制台重启。回滚到这里之前，先加 swap，或在其他机器上构建好镜像再导入；切换回来前，先把 Mac 上的数据同步回东京。

FastAPI 后端 + 全部定时任务跑在东京主机的**一个** Docker 容器里；前端静态文件和 nginx 在首尔（见 `deploy/seoul/README.md`）。

```
浏览器 --https--> 首尔 nginx (stock.nexorai.com.cn)
                   ├─ /            静态 SPA
                   └─ /api/        --ZeroTier 内网--> 东京 <STOCK_BIND_ADDR>:8000 (本容器)
                                                        └─ 每个请求带 user_session Cookie -> https://user.nexorai.com.cn/v1/session
```

## 为什么是一个容器、一个进程

`docker/docker-compose.yml` 里有 `analyzer`（`python main.py --schedule`）和 `server`（`--serve-only`）两个服务：两者都会启动飞书长连接，也各自带一套调度器，同时起会让飞书长连接和定时任务重复运行。

本目录的 `docker-compose.yml` 只有一个服务，命令是：

```
python main.py --serve --schedule --no-run-immediately --host 0.0.0.0 --port 8000
```

- `--serve`：在后台线程启动 FastAPI（Web/API）。
- `--schedule`：强制开启 Web/API 进程内的 runtime scheduler（`RuntimeSchedulerService`）。它和 `--schedule` 单独运行时是同一套任务：每日分析、17:00 板块流程、每周行业成分刷新、08:15 盘前版、资讯轮询与每日资讯抓取、事件提醒。
- 飞书长连接由 `start_bot_stream_clients` 在 API 启动后启动**一次**（`FEISHU_STREAM_ENABLED=true` 时）。
- `--no-run-immediately`：容器重启时不立即跑一轮全量分析。
- compose 里的 `init: true` 让 PID 1 转发 SIGTERM，`docker compose stop` 能快速退出。

## 1. 准备目录与代码

```bash
sudo mkdir -p /data/nex-stock/{app,config,data,logs,reports}
# 容器内以 uid/gid 1000（dsa）运行：这四个目录都要能被它写入。
# config 目录必须可写：设置页保存配置时会原子替换 .env。
sudo chown -R 1000:1000 /data/nex-stock/{config,data,logs,reports}

cd /data/nex-stock
sudo git clone --branch main <仓库地址> app      # 生产只从 main 部署
# 以后更新：cd /data/nex-stock/app && sudo git pull --ff-only
```

## 2. 创建应用配置

```bash
cd /data/nex-stock/app/deploy/tokyo
sudo cp env.production.example /data/nex-stock/config/.env
sudo chown 1000:1000 /data/nex-stock/config/.env
sudo chmod 600 /data/nex-stock/config/.env
sudoedit /data/nex-stock/config/.env      # 填 PUBLIC_ORIGIN、TUSHARE_TOKEN、FEISHU_APP_ID/SECRET 等
```

`env.production.example` 里是需要设置的键名（密钥留空）：认证（`AUTH_MODE=user_center`、`PUBLIC_ORIGIN`、`USER_CENTER_URL`、`USER_CENTER_REQUIRED_ROLE`）、`TUSHARE_TOKEN`、资讯开关、飞书、调度开关与各任务时间。**LLM 渠道**登录后在 Web「设置 → AI 模型」里配置，保存会写回 `/data/nex-stock/config/.env`。

> 前提：`PUBLIC_ORIGIN`（`https://stock.nexorai.com.cn`）已在用户中心（nex_user）的返回地址白名单中，且用户中心的 `user_session` Cookie 作用域是 `.nexorai.com.cn`。

## 3. 配置 Compose 变量

```bash
cd /data/nex-stock/app/deploy/tokyo
sudo cp compose.env.example .env
ip -4 addr show | grep -A2 zt            # 找到 ZeroTier 网卡上的内网 IP
sudoedit .env                             # STOCK_BIND_ADDR=<该内网 IP>
```

- `STOCK_BIND_ADDR` 决定宿主机把 8000 端口发布在哪个地址。默认 `127.0.0.1`（只有本机能访问）；设成 ZeroTier IP 后首尔 nginx 才能访问。**不要设成 `0.0.0.0`**，后端端口不应暴露在公网。
- ZeroTier 网卡晚于 Docker 启动时，容器会因绑定失败而按 `restart: unless-stopped` 自动重试，直到网卡就绪。
- 同时建议在东京安全组 / 防火墙里只放行首尔主机访问该端口。

## 4. 构建并启动

```bash
cd /data/nex-stock/app/deploy/tokyo
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs -f --tail=100 stock
```

构建会在镜像里打包前端（`static/`，仅用于后端直接访问时的兜底；对外的前端由首尔 nginx 提供）。首次构建需要几分钟。

## 5. 检查

```bash
# 容器健康（healthcheck 访问 /api/health）
sudo docker compose ps          # STATUS 显示 healthy

# 本机直接访问：健康检查不需要登录
curl -fsS http://127.0.0.1:8000/api/health            # 若 STOCK_BIND_ADDR 不是 127.0.0.1，用该地址

# 受保护接口在没有 user_session 时必须是 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/history     # 期望 401

# user_center 模式已生效（日志里应有 "AUTH_MODE=user_center: user center=... public origin=..."）
sudo docker compose logs stock | grep "AUTH_MODE=user_center"

# 调度器与飞书长连接各只有一份
sudo docker compose logs stock | grep -iE "runtime scheduler|Feishu Stream"
```

从首尔主机验证内网连通：`curl -fsS http://<STOCK_BIND_ADDR>:8000/api/health`。

## 6. 一次性冷启动回填（60 个交易日）

需要 `TUSHARE_TOKEN`。回填会按 Tushare 频控逐日请求，耗时较长，可断点续跑；**只运行一次**，不要反复重跑。

```bash
cd /data/nex-stock/app/deploy/tokyo
sudo docker compose exec -u dsa stock python scripts/backfill_market_snapshot.py --days 60
# 日志在容器里输出 [backfill] 进度；中断后重新执行同一条命令会跳过已完整的日期。
# 之后 17:00 的板块流程会按交易日自动补全，也可手动补跑：
#   sudo docker compose exec -u dsa stock python scripts/run_board_pipeline.py
```

务必带 `-u dsa`，避免以 root 写出属主为 root 的数据库/日志文件。

## 日常运维

```bash
cd /data/nex-stock/app/deploy/tokyo
sudo docker compose logs -f --tail=200 stock     # 日志（同时落盘在 /data/nex-stock/logs）
sudo docker compose restart stock                # 重启（修改 AUTH_MODE / PUBLIC_ORIGIN 等需要重启）
sudo docker compose down                         # 停止并删除容器（数据都在 /data/nex-stock，不受影响）

# 升级
cd /data/nex-stock/app && sudo git pull --ff-only
cd deploy/tokyo && sudo docker compose up -d --build

# 备份：停服务后复制 /data/nex-stock/data（SQLite 数据库）和 /data/nex-stock/config/.env
```

## 回滚

```bash
cd /data/nex-stock/app && sudo git log --oneline -5     # 找到上一个可用提交
sudo git checkout <commit>                              # 或 git revert 问题提交后重新部署
cd deploy/tokyo && sudo docker compose up -d --build
```

数据目录与配置在宿主机，回滚镜像不会丢数据；如新版本改过数据库结构，回滚前先备份 `data/`。

## 故障排查

| 现象 | 排查 |
| --- | --- |
| 所有 `/api/*` 返回 503 `AUTH_MISCONFIGURED` | `config/.env` 缺少 `PUBLIC_ORIGIN`（必须是 `https://域名`，不带路径），或 `USER_CENTER_REQUIRED_ROLE` 取值不是 `admin` / `user` |
| 返回 503 `USER_CENTER_UNAVAILABLE` | 容器访问不到 `USER_CENTER_URL`（3 秒超时）；在容器里 `curl -sS -o /dev/null -w '%{http_code}' $USER_CENTER_URL/v1/session` 应返回 401。注意代理环境变量 `HTTP(S)_PROXY` / `NO_PROXY` |
| 登录后又跳回登录页 | 用户中心的 Cookie 作用域不是 `.nexorai.com.cn`，或 nginx 没透传 `Cookie`；`PUBLIC_ORIGIN` 没在白名单里 |
| 非管理员登录后显示「无权限」 | 预期行为；用管理员账号登录 |
| POST 返回 403 `ORIGIN_NOT_ALLOWED` / `CSRF_INVALID` | `PUBLIC_ORIGIN` 与浏览器地址不完全一致（协议、域名、端口），或请求没有带 `X-CSRF-Token`（前端旧缓存，强刷） |
| 设置页保存配置失败 | `/data/nex-stock/config` 不可被 uid 1000 写入（见第 1 步 `chown`） |
| MCP 管家 / 外部脚本调用 `/api/v1/*` 返回 401 | user_center 模式没有服务令牌通道：所有 `/api/*`（除健康检查）都要求管理员浏览器会话；这类调用需要在内网另行处理 |
