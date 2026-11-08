# 首尔前端部署（stock.nexorai.com.cn）

首尔主机只放静态前端和 nginx：`/` 提供 SPA，`/api/` 通过 ZeroTier 内网反向代理到东京后端（见 `deploy/tokyo/README.md`）。登录走 NexorAI 用户中心（`user_session` Cookie，作用域 `.nexorai.com.cn`），前端与 API 同源（`https://stock.nexorai.com.cn`），所以**构建时不要设置 `VITE_API_URL`**（默认空 = 相对路径 `/api/...`）。

目录约定：

```
/opt/nex_stock_frontend/
├── acme/                    # certbot webroot（/.well-known/acme-challenge/）
├── releases/<时间戳>/       # 每次发布一个目录
└── current -> releases/<时间戳>   # nginx 的 root，发布时切换软链
```

## 1. 构建前端

在任意有 Node 20 的机器上（本机、CI 或首尔主机）：

```bash
cd apps/dsa-web
npm ci
npm run build
```

Vite 配置把产物输出到仓库根目录的 `static/`（`apps/dsa-web/../../static`，不是 `apps/dsa-web/dist`），其中包含 `index.html`、`assets/`、`stocks.index.json`、`build-info.json`。

## 2. 上传并切换版本

```bash
TS=$(date +%Y%m%d%H%M%S)
# 在构建机上：
rsync -a --delete static/ <首尔主机>:/opt/nex_stock_frontend/releases/$TS/

# 在首尔主机上：
sudo ln -sfn /opt/nex_stock_frontend/releases/$TS /opt/nex_stock_frontend/current
# 保留最近几个版本用于回滚，旧的可以手动清理：ls -1dt /opt/nex_stock_frontend/releases/* | tail -n +6
```

回滚：`sudo ln -sfn /opt/nex_stock_frontend/releases/<上一个时间戳> /opt/nex_stock_frontend/current`。静态文件切换不需要重载 nginx（`index.html` 带 `no-store`，`assets/` 文件名带哈希）。

## 3. 首次部署：证书与 nginx

DNS 需要先把 `stock.nexorai.com.cn` 解析到首尔主机。

```bash
sudo mkdir -p /opt/nex_stock_frontend/{acme,releases}

# (a) 先装只有 80 端口的引导配置（443 的证书此时还不存在，完整配置 nginx -t 会失败）
sudo cp deploy/seoul/stock.nexorai.com.cn.http-bootstrap.conf /etc/nginx/conf.d/stock.nexorai.com.cn.conf
sudo nginx -t && sudo systemctl reload nginx

# (b) 签发证书
sudo certbot certonly --webroot -w /opt/nex_stock_frontend/acme -d stock.nexorai.com.cn

# (c) 换成完整配置，并把占位符替换为东京主机的 ZeroTier 内网 IP
sudo cp deploy/seoul/stock.nexorai.com.cn.conf /etc/nginx/conf.d/stock.nexorai.com.cn.conf
sudo sed -i 's/__BACKEND_ZEROTIER_IP__/<东京 ZeroTier IP>/' /etc/nginx/conf.d/stock.nexorai.com.cn.conf
sudo nginx -t && sudo systemctl reload nginx
```

> 只用 `reload`，不要 `restart`：这台 nginx 上还有其他站点。`nginx -t` 失败时不要重载，先修配置。
>
> 证书续期沿用 certbot 的定时任务；webroot 续期不需要停 nginx。续期后用 `sudo systemctl reload nginx` 让新证书生效（可在 `/etc/letsencrypt/renewal-hooks/deploy/` 里放一个 reload 钩子）。

## 4. 验证

```bash
curl -sI http://stock.nexorai.com.cn/ | head -3                 # 301 -> https
curl -sI https://stock.nexorai.com.cn/ | head -5                # 200，index.html 带 no-store
curl -s https://stock.nexorai.com.cn/api/health                 # {"status":"ok",...}，说明到东京的内网链路通
curl -s -o /dev/null -w '%{http_code}\n' https://stock.nexorai.com.cn/api/v1/history     # 未登录期望 401
curl -sI https://stock.nexorai.com.cn/api/login | grep -i '^location'    # 指向 user.nexorai.com.cn/login?return=...
```

浏览器打开 `https://stock.nexorai.com.cn/`：未登录会跳转到用户中心登录页，登录后回到站点；非管理员账号显示「无权限」页。

## nginx 配置说明

- `/assets/`：哈希文件，`Cache-Control: public, immutable` + 1 年。
- `/index.html`：`no-store`。
- `/api/`：反向代理到 `nex_stock_backend`（东京 `:8000`），透传 `Host`、`X-Real-IP`、`X-Forwarded-For`、`X-Forwarded-Proto`、`Cookie`；`proxy_redirect off`，保证 `/api/login`、`/api/logout` 的 302 指向用户中心；读超时 600 秒。
- `/api/v1/agent/chat/stream`（问股流式回答，POST）与 `/api/v1/analysis/tasks/stream`（任务 SSE）：`proxy_buffering off`，读超时分别为 600 / 3600 秒。
- `/`：`try_files $uri $uri/ /index.html`（SPA 路由回退）。`/stocks.index.json` 取构建产物里的静态副本。
- 后端的 `/docs`、`/redoc`、`/openapi.json` 没有对外代理。
