# 公开网页联网文字查重

归档导入与本地分析仍在浏览器中执行。联网检查是用户单独选择的步骤，仅向所配置的查重服务发送选中的帖子 `id`、正文、帖子链接和发布时间；不发送 ZIP、私信、联系人或媒体。服务把最多两个正文关键片段发送给搜索 API，再读取搜索结果中的公开网页并比较文本。服务访问 token 与搜索 API key 是不同凭据；搜索 key 只能保存在后端环境变量。

GitHub Pages 只能发布静态网页，不能运行这个 Python 服务。本仓库提供完整服务与测试，但部署到 Pages 不代表搜索服务已经部署或配置成功。搜索服务未配置、请求失败或预算不足时，页面应保留明确的未检查状态。真实搜索验收需要已配置的合法搜索 API 和可从浏览器访问的服务地址。

需要公开 HTTPS 服务时，可使用 [容器部署制品](WEB_CHECK_CONTAINER.md)。容器不携带搜索密钥，由选定平台配置环境变量、TLS 与实际域名。

## 本机使用

Python 3.12+，无需额外依赖。从源码目录启动：

```powershell
$env:WEBCHECK_PROVIDER = 'tavily'
# 通过当前终端或系统 secret 管理设置 TAVILY_API_KEY，不要粘贴到源码或提交记录。
python webcheck_server.py --host 127.0.0.1 --port 8787
```

也支持 Brave Search API：设置 `WEBCHECK_PROVIDER=brave` 和 `BRAVE_SEARCH_API_KEY`。Tavily 使用官方 `https://api.tavily.com/search`；Brave 使用官方 `https://api.search.brave.com/res/v1/web/search`。没有自动购买、注册、绕过验证或抓取受限搜索页面的操作。

浏览器连接 `http://127.0.0.1:8787` 可能需要允许网站访问本地网络，具体取决于浏览器设置。HTTPS 网站访问 HTTP 服务也可能受到浏览器混合内容限制；这时在本机网站中使用，或部署 HTTPS 后端。

## HTTPS 服务部署

`deployment/webcheck.env.example` 列出变量名，不含真实凭据。在自己的服务器或托管平台上设置搜索 API key、访问 token、明确的 Host 与 Origin。推荐由 HTTPS 反向代理向本机监听的 `127.0.0.1:8787` 转发；确保代理传入的 Host 与 `WEBCHECK_ALLOWED_HOSTS` 完全一致。公开绑定如 `--host 0.0.0.0` 时，缺失至少 32 字符 token、Host 或 Origin 任一配置都会拒绝启动。

如需生成新的服务访问 token，在自己的终端运行 `python webcheck_server.py --generate-access-token`，直接保存到托管平台的 secret 设置。将这个访问 token 分享给需要使用服务的人时，使用自己的安全渠道。前端输入的服务访问 token 仅用于 `Authorization: Bearer …`；不能输入 Tavily/Brave API key。请勿把 token 作为 URL 参数。

设置 `WEBCHECK_ALLOWED_ORIGINS=https://potervlag-cyber.github.io` 即只允许当前 Pages 来源；Origin 不包含 `/x-originality-checker/` 路径。多个来源或 Host 用逗号分隔，不允许通配符。默认只监听 loopback，并允许本服务本机来源和当前 Pages 来源。

默认每滚动小时最多预留 100 次搜索、最多两个批次同时运行；一条帖子最多两次搜索，每批最多十条。预算在启动进程的内存中维护，重启会重置；多进程部署必须在代理或平台添加共享限额，不能把每个进程的限额误当全局共享。上游失败仍占预留预算，因为失败不保证没有 API 消耗。断开前端请求不会撤回已发出的上游查询，当前有界批次可能继续完成；取消会阻止浏览器发起后续批次。

## 接口

`GET /api/webcheck/status` 返回 `ready`、`provider`、`requires_access_token` 和非敏感 `limits`。它不返回搜索 key、访问 token、帖子内容或上游响应正文。

`POST /api/webcheck` 要求 JSON、明确的 `consent: true`。每条仅接受四个字段，不能提交整个项目对象：

```json
{
  "consent": true,
  "posts": [{
    "id": "sample-1",
    "text": "选中的公开帖子正文",
    "url": "https://x.com/example/status/123456789",
    "created_at": "2026-10-07T08:00:00+08:00"
  }]
}
```

输入正文最多 50,000 字符，实际只检查前 5,000 字符。返回 `original_chars`、`checked_chars` 和 `text_truncated` 显示范围。客户端也可先截取正文并在本地计划中保留原始范围。归一化正文不足 24 字符时跳过；不能将常用短语当作可靠重复证据。

返回 `schema_version: 1`、`provider`、`checked_at`、`coverage`、`posts` 和 `limitations`。`coverage.web_coverage` 永远为 `unknown`。帖子状态含 `matched`、`no_match`、`partial`、`failed`、`skipped`；搜索或抓取失败保留 `issues[].code`，没有相似证据时 `max_similarity` 为 `null`。`no_match` 只表示本次有限检索没有证据，不表示原创。

`matches` 最多保留三个候选，另用 `matches_total` 记录已找到数量。每条证据返回安全链接、标题、`score`（0–1，文本重合参考值）、对应片段、匹配字符数、来源类型和时间线索：

- `source_kind=page_body` 表示实际获取并提取了公开网页正文，`page_status=fetched`。
- `source_kind=search_snippet` 表示正文获取失败，证据仅来自搜索结果摘要；`page_status` 保留失败代码，不能当作阅读全文。
- `published_at_basis=page_metadata` 或 `search_result` 表示来源声明的时间线索；没有可靠时区时 `temporal_relation=unknown`。
- 当前同一 X status id 的 `x.com` / `twitter.com` 命中放入单独的 `same_post`，排除外部重复证据。身份、转载授权、共同来源与真正首发仍需人工核实。

每次查询最多返回五个候选；两次查询按结果排名交错选取并按 URL 去重，总共仍最多尝试五个来源。例如先选第一查询的第一项，再选第二查询的第一项，然后各自第二项；第一查询不能占满全部来源预算而排除第二查询。未选中的候选仍计入 `candidates_found`，候选超限保留 `partial` 状态。

原始 API 响应中的 `source_checks` 最多五项，记录实际尝试的来源、标题、读取状态、正文或摘要类型、来源截断与已达到报告阈值的相似分数/字符数；无相似证据为 `score: null`，不代表原创。它不返回查询、正文或凭据。被安全校验阻断或 DNS 失败的来源只保留失败状态，不保留可点击 URL 或标题；原有 `issues` 继续记录失败代码。同一原帖重定向标记 `same_post`，不计外部重复。此字段用于服务验收诊断，当前浏览器结果与下载报告只保留原有匹配证据，不包含这份诊断列表。

网络传输只允许 HTTP/HTTPS 默认端口；禁止 URL 凭据、私网与混合 DNS 答案。连接固定到已验证的公网 IP，HTTPS 使用原主机名进行证书验证；每次重定向重新核验，不能通过 DNS 重绑定访问内部系统。搜索 provider 不跟随重定向，避免 key 外传。抓取正文最大 2 MiB，提取后只比对前 100,000 字符，返回 `source_text_truncated`；有限候选、读超时、CORS、Host、访问 token、并发与小时预算共同限制资源消耗。服务不输出导入正文、搜索 query、凭据、上游错误正文或请求日志。

## 验收与证据范围

```sh
python -m unittest discover -s tests -p test_webcheck.py -v
```

测试启动真实本地 HTTP 搜索响应与 HTML 来源 fixture，验证 POST → 两种 provider 协议 → 来源读取 → 正文提取 → 文本比对 → 证据回传。fixture 只用于验证协议与处理行为，不证明真实公开搜索成功。安全测试覆盖混合公网/私网 DNS、地址固定、私网重定向、provider key 不随重定向发送、正文限长、来源类型、同一帖子排除、CORS/Host/token、用户同意、预算与缺少 provider 的失败状态。

上线验收还需要单独读取真实 provider 状态、真实候选网页与时间依据，确认 HTTPS 与浏览器 CORS 请求正常，并记录已实际执行的结果。未执行的真实搜索步骤应保持“待验证”，不能把本地 fixture 测试写成全网查重 PASS。

部署后设置本机 `WEBCHECK_ACCESS_TOKEN` 环境变量，再执行：

```sh
python deployment/check_webcheck_live.py --endpoint https://实际查重服务 --output qa/webcheck-live-verification.json
```

此命令发送 Python 官方教程的一段公开引文；它会实际消耗最多两次搜索查询。只有服务配置存在、真实查询返回成功次数、至少一个来源正文被读取且有可比较重合证据才通过。搜索摘要、空结果、配置就绪或 HTTP 200 单独不能通过。它不读取真实用户归档，也不把服务访问 token 写进验收文件。浏览器实际访问、CORS 与发布源码身份仍需另行验证。
