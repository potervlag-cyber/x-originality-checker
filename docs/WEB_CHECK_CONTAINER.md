# 部署联网查重服务容器

本制品只运行搜索后端。GitHub Pages 页面继续在浏览器解析归档，服务不接收 ZIP。需要一个能运行容器、配置环境变量并提供 HTTPS 的平台；本仓库不预设平台，也不会使用其他项目的凭据。

## 构建和运行

从仓库根目录构建：

```sh
docker build -t originality-webcheck .
```

镜像使用固定版本 `python:3.12.12-slim`，没有额外 Python 依赖，以 UID/GID `10001` 运行。构建上下文采用允许列表，只包含两个后端模块、两个启动/健康检查脚本和 Docker 制品；归档、测试输出、Git 记录和 secret 文件不进入上下文。基础镜像的 registry digest 和实际容器启动仍需在部署环境验证。

在平台的 secret/environment 设置中配置以下变量，变量含义见 [联网服务说明](WEB_CHECK.md)：

| 变量 | 配置 |
| --- | --- |
| `WEBCHECK_PROVIDER` | `tavily` 或 `brave` |
| `TAVILY_API_KEY` / `BRAVE_SEARCH_API_KEY` | 仅选定 provider 的真实 key |
| `WEBCHECK_ACCESS_TOKEN` | 至少 32 字符的服务访问 token，与 provider key 不同 |
| `WEBCHECK_ALLOWED_HOSTS` | 反向代理实际传入的 Host，可含端口；多个以逗号分隔 |
| `WEBCHECK_ALLOWED_ORIGINS` | 当前页面为 `https://potervlag-cyber.github.io`，没有项目路径 |
| `WEBCHECK_HOURLY_QUERY_BUDGET` | 如 `100`；单进程滚动小时查询上限 |
| `PORT` | 平台指定的监听端口，默认 `8787`，范围 1–65535 |

不要把 provider key 或服务 token 填入 Dockerfile、构建参数、网页配置或 GitHub Pages。已有 `deployment/webcheck.env.example` 仅是变量名模板。自管服务器可创建未跟踪的 `deployment/webcheck.env` 并用 `--env-file`；不要在 shell 命令里写真实 secret。

```sh
docker run --rm --name originality-webcheck \
  --env-file deployment/webcheck.env \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --memory=256m --cpus=1 --pids-limit=64 \
  -p 127.0.0.1:8787:8787 originality-webcheck
```

本例只向宿主 loopback 映射端口，由宿主 HTTPS 代理转发。若修改 `PORT`，同时修改容器端口映射。平台不支持这些 Docker 选项时，使用其等价设置。不要为容器挂载归档、凭据目录或 Docker socket。默认运行一个实例；多实例需要平台共享预算限制，进程内预算不能当成集群全局额度。

HTTPS 代理必须把 Host 转发成 `WEBCHECK_ALLOWED_HOSTS` 中的明确值。服务不会信任 `X-Forwarded-Host` 自动放行请求。检查浏览器来源的 CORS，确保该 Origin 得到明确匹配的 `Access-Control-Allow-Origin`。TLS、域名与代理均由部署平台负责，容器自身不提供 TLS。

## 就绪与上线验收

容器健康检查对 loopback 的 `/api/webcheck/status` 请求使用配置的第一个 Host，并携带服务 token；它不会跳过 Host 检查，也不会发送付费查询。HTTP 200、`ok: true`、`schema_version: 1`、`ready: true` 才通过。没有 provider key 时服务仍可启动，状态为 `ready: false`、健康检查失败，实际查询返回 HTTP 503 `provider_not_configured`。

`ready: true` 只表示 provider 配置存在。它不证明 key 有效、额度足够、外部搜索成功或浏览器能访问。因此完成部署后还必须执行以下验收：

1. 回读实际 HTTPS `/api/webcheck/status`，确认 provider 与限额；用正确 Host/Origin/token 检查请求成功。
2. 浏览器填写 HTTPS 服务地址和服务访问 token，连接并明确同意联网。先查少量公开测试文本，回读真实 provider 候选、来源正文或摘要及失败状态；不要把本地 fixture 当成真实搜索。
3. 确认错误 Host/Origin/token 被拒绝、secret 不进入平台日志、长文/抓取失败保留未检查范围。更新 `deployment/webcheck-config.json` 的公开 endpoint 时只填写服务地址，不填写任何 token。
4. 保存真实 HTTP、浏览器与来源证据，再将“真实联网查询”记为通过。健康检查通过或容器成功启动不能单独作为此项完成证据。

部署制品对应的宿主启动/健康检查测试：

```sh
python -m unittest discover -s tests -p test_webcheck_deployment.py -v
```

这些测试使用真实本机 HTTP 服务与虚构配置，验证启动、PORT、Host 和缺少 provider 时的失败行为；没有发出真实搜索请求。实际 Docker build/run、TLS、真实 provider key、公开 endpoint 和浏览器 CORS 需要在选定的部署环境验证。

有 Docker 的 Linux 环境可进一步执行实际容器验收；GitHub Actions 的 `ubuntu-latest` runner 也可运行同一脚本：

```sh
python deployment/smoke_webcheck_container.py
```

脚本实际构建镜像，以非 root 用户启动带只读根目录的容器，仅映射 `127.0.0.1:18787`。它显式清空两种 provider key，使用公开的虚构服务 token，验证实际 UID、应用文件允许列表、状态未就绪、合法 Origin 的 CORS、非法 Host/Origin/token 拒绝、查询返回 503，以及健康检查退出 1。无论通过还是失败，都只清理本轮创建的确切容器和镜像，不做全局 prune。成功输出 JSON 是 Docker 与本机 HTTP 验收证据，不是搜索成功证据；真实 API、HTTPS 和浏览器仍需单独验证。
