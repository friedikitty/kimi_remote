# kimi-remote 实现原理

本文档说明 kimi-remote 的工作原理:如何用一条命令把 kimi code 的对话
变成手机上可访问的加密 Web UI。

## 总体架构

```
手机浏览器
   │  HTTPS(加密隧道)
   ▼
隧道入口(Tailscale ts.net / Cloudflare trycloudflare.com)
   │  回源到 127.0.0.1:<port>(本机回环,不出网卡)
   ▼
kimi web 进程(REST + WebSocket + 静态 Web UI,同一端口同源)
   │
   ▼
kimi code 会话(~/.kimi-code/sessions/)
```

kimi_remote.py 只是一个**编排器(launcher)**:它本身不实现任何网络
协议,只负责按正确顺序拉起 `kimi web` 和隧道进程、拼出带 token 的
URL、打印二维码,并在退出时清理现场。

## 核心组件

### 1. kimi web(kimi code 自带)

`kimi web` 是 kimi CLI 的内置子命令,在前台启动一个本地服务器:

- REST API + WebSocket + Web UI 同端口同源;
- 默认只绑定 `127.0.0.1`,不暴露到局域网;
- **Bearer token 鉴权**:token 存于 `~/.kimi-code/server.token`,
  所有 API/WS 请求必须携带 `Authorization: Bearer <token>`;
- Web UI 支持 `#token=<token>` URL fragment 自动登录——fragment 只在
  浏览器本地解析,**不会随 HTTP 请求发到服务器或隧道商**,所以把
  token 放进 URL 是安全的分享方式;
- **DNS 重绑定防护**:校验请求 Host 头,非 localhost 的 Host 必须在
  `--allowed-host` 白名单里。支持逗号分隔、重复传入,且**前导点号
  表示后缀匹配**(`.trycloudflare.com` 匹配其所有子域名,实测验证);
- `kimi web rotate-token` 可重新生成 token,旧 token 立即失效。

### 2. Tailscale serve(默认模式,推荐)

Tailscale 是基于 WireGuard 的端到端加密 overlay 网络(tailnet)。

- `tailscale serve --bg --https=443 http://127.0.0.1:<port>` 让本机
  tailscaled 在 tailnet 内以 `https://<机器名>.<tailnet名>.ts.net`
  反向代理本地端口;
- ts.net 域名由 Tailscale 自动签发真实 TLS 证书(Let's Encrypt),
  手机浏览器无警告;
- 流量走 WireGuard,**只有同一 tailnet 的设备能到达**——不公开;
- 主机名固定,启动前即可通过 `tailscale status --json` 的
  `Self.DNSName` 拿到,因此可以提前传给 `--allowed-host`;
- 退出清理:`tailscale serve --https=443 off`;
- 代价:手机需装 Tailscale App 并登录同一账号;PC 端首次需
  `tailscale up` 浏览器登录。

### 3. Cloudflare quick tunnel(备选模式)

`cloudflared tunnel --url http://127.0.0.1:<port>` 无需账号,向
Cloudflare 边缘申请一个**随机四级域名** `https://<随机词组>.trycloudflare.com`
并建立 outbound 加密隧道(回源也是到本机回环)。

- URL 打印在 cloudflared 的 **stderr**,脚本用正则抓取;
  注意要排除 `api.trycloudflare.com`(控制面地址,不是隧道 URL);
- URL 是**公开的**:任何拿到完整 URL(含 token fragment)的人都能
  访问。token 是唯一防线,所以绝不使用 `--dangerous-bypass-auth`,
  且完整 URL 要当作秘密对待;
- 域名每次随机,只有隧道建立后才知道 → 要么先等 cloudflared 再启动
  kimi web,要么用后缀白名单 `--allowed-host .trycloudflare.com`
  提前放行(本项目采用后者实现并行启动)。

### 4. 编排与清理(kimi_remote.py)

1. 预检:`shutil.which` 定位 kimi / tailscale / cloudflared;
2. 读 token(没有就 `kimi web rotate-token` 生成);
3. 并行拉起隧道与 `kimi web --no-open --port P --allowed-host <host>`,
   轮询 127.0.0.1:P 直到端口就绪;
4. 打印 `<base>/#token=<token>` 与二维码(`qrcode` 包可选,缺失或
   控制台编码不支持块字符时优雅降级为只打印 URL);
5. 主循环监视子进程,任一异常退出即报错退出;
6. Ctrl-C / finally:terminate 所有子进程,tailscale 模式额外执行
   `serve --https=443 off`,不留残留配置。

### 5. PowerShell 入口

- `setup.ps1`:winget 安装 uv、Tailscale、cloudflared,引导
  `tailscale up` 登录;
- `start.ps1`:菜单选择隧道,检查/拉起 tailscale 守护进程,最后
  `uv run kimi_remote.py`。pyproject.toml 声明了 `qrcode` 依赖,使
  uv 环境下二维码始终可用。

## 安全模型小结

| 层 | 机制 |
| --- | --- |
| 传输加密 | WireGuard(tailscale)/ TLS(cloudflare,回源仅回环) |
| 网络可达性 | tailscale:仅 tailnet;cloudflare:公开,靠 URL 保密 |
| 应用鉴权 | kimi bearer token(可 rotate 立即失效) |
| Host 校验 | `--allowed-host` 防 DNS 重绑定 |
| 绝不使用 | `--dangerous-bypass-auth` |
