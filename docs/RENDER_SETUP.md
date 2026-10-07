# Render 免费版 + Tavily 联网查重

本仓库的 `render.yaml` 只创建一个免费 Web Service，运行现有搜索容器，不创建数据库或付费资源。GitHub Pages 继续提供归档分析页面；Tavily key 只保存在 Render。

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2Fpotervlag-cyber%2Fx-originality-checker)

## 你需要做的四步

1. 注册或登录 [Tavily](https://app.tavily.com/)，在账户后台取得 API key，保留免费额度设置。
2. 点击上面的 **Deploy to Render**，注册或登录 Render。确认只有 `originality-webcheck` 一个服务，计划为 **Free**；在 `TAVILY_API_KEY` 提示处粘贴 key，然后部署，等待服务变为 **Live**。不要选择付费计划。
3. 在 Render 服务的 **Environment** 页面查看自动生成的 `WEBCHECK_ACCESS_TOKEN`。这是网站使用的服务访问码，与 Tavily key 不同。在本机文件 `C:\Users\1\Desktop\X写作\x-originality-checker\deployment\webcheck.env` 中保存下面这一项；若文件已存在，保留其它内容，只新增或替换这一项。这个文件已由 Git 忽略，不要放 Tavily key：

   ```dotenv
   WEBCHECK_ACCESS_TOKEN=这里替换为Render生成的服务访问码
   ```

   在网站首页打开默认关闭的联网开关，再选择 ZIP。系统先在本机读取、分析全部归档，随后在大弹窗中列出本人主帖（普通发帖、长文和引用帖，不含回复或普通转帖）。滚动或点击“加载更多”，每次在本机追加最多 50 条，可一直浏览至全部主帖。正文关键词或帖子 ID 搜索覆盖全部主帖，支持全部主帖／可检索／已选筛选，继续加载及搜索保留勾选。手动勾选恰好 10 条可检索主帖，无需填写链接。弹窗内填写同一个服务访问码，必要时展开高级连接设置修改服务地址，并确认文字发送许可；确认后自动联网并生成综合报告，访问码不用发到聊天里。正文缺失或不完整、正文过短、ID 冲突的主帖可见但禁用，并显示原因；正文展开和实际查询均最多使用前 5,000 字。选择只在本机完成，确认后仅发送这 10 条的必要帖子字段，不自动补选或查询全归档。10 条结果不代表全部归档或全网覆盖，合格主帖不足 10 条时可点“仅查看本地报告”；本地报告继续覆盖包括回复和转帖在内的全部归档记录。
4. 把 Render 服务实际公开地址发回聊天，例如 `https://你的服务名.onrender.com`。只发地址即可。我会配置网站默认连接、执行真实搜索与浏览器验收，并回读结果；密钥和访问码都不需要发到聊天里。

API key 和访问码填写完成、服务变为 Live，只代表部署与配置完成。真实搜索需要成功调用 Tavily、获取真实候选、回传正文或摘要证据并完成浏览器连接；在这些步骤实际执行前，仍标为“待验证”。

## 免费版使用边界

Render 免费 Web Service 在 15 分钟无访问后休眠，下次访问约需一分钟启动。首次连接可能需要等待或重新连接；不要靠定时唤醒绕过免费版限制。每个 workspace 每月共享 750 小时免费实例时长，另有带宽和构建额度；异常高的对外 API/网页流量可能触发服务暂停。Tavily 免费额度由其账户后台显示，服务自己的默认预算为每滚动小时 100 次搜索，一条最多两次；这不等于平台或 Tavily 保证的免费额度。

本服务不持久保存帖子或搜索结果。休眠、重启和部署会重置进程内小时预算；不要把它当成跨重启的付费硬上限。先查少量公开文本并查看 Tavily 使用量，遇到额度不足应暂停后续批次。该方案适合个人低频测试；免费服务不能承诺持续可用。

## Blueprint 已配置的内容

`runtime: docker` 使用仓库根目录 `Dockerfile` 和构建上下文；单实例、`plan: free`、监听 `0.0.0.0:$PORT`，端口配置为 Render 默认的 `10000`。未指定区域，使用平台默认选择。自动部署关闭；仓库更新后可在 Render 手动部署所需提交。

`WEBCHECK_ALLOWED_HOSTS` 通过 Blueprint 自引用得到平台提供的 `RENDER_EXTERNAL_HOSTNAME`，不猜测服务子域，也不使用通配符或信任请求中的 forwarded Host。Render 官方文档明确说明：

> You can also make a service reference itself to expose a default environment variable (such as RENDER_EXTERNAL_HOSTNAME) under another key.

同页示例使用 `fromService` 的服务自身名称、`type: web` 和 `envVarKey: RENDER_EXTERNAL_HOSTNAME`。来源是 [Blueprint 官方说明](https://render.com/docs/blueprint-spec#self-referencing-environment-variables)。服务名称与这处引用保持一致。网站 Origin 固定为 `https://potervlag-cyber.github.io`，不含项目路径。

Render 为 `generateValue: true` 生成 base64 编码的 256 位随机值，即 44 字符，满足服务访问码至少 32 字符的要求；它不会进入 YAML 或静态网页。`sync: false` 只在首次 Blueprint 创建时提示输入 Tavily key；以后修改 key，使用该服务的 Environment 页面。

平台健康路径为 `/api/webcheck/status`。未配置 key 时该接口返回 HTTP 200、`ready: false`，而实际查重返回 HTTP 503；Render 只检查 HTTP 状态，因此 **Live 不等于可查询**。容器自身健康脚本还检查 `ready: true`。两种检查都不验证 key 有效性或真实搜索成功。

没有自定义域时，Render 健康检查的 Host 为服务的 `onrender.com` 子域，与自引用值匹配。本 Blueprint 不添加自定义域。以后如添加域名，Render 可能以该域名发健康检查，需把实际使用的明确域名加入 Host 允许列表并重新验证，不能改成通配符。

## 验证与官方依据

已于 2026-10-07 回读以下 Render 官方页面及公开 JSON Schema，均返回 HTTP 200：

- [Blueprint 语法](https://render.com/docs/blueprint-spec)：Docker 路径、Free 计划、随机值、secret 输入、自引用默认变量和自动部署设置。
- [部署按钮](https://render.com/docs/deploy-to-render)：使用明确 `repo` 参数，指向本仓库。
- [默认环境变量](https://render.com/docs/environment-variables)：平台服务域名与默认端口。
- [健康检查](https://render.com/docs/health-checks)：Host 与 2xx/3xx 判定。
- [免费实例限制](https://render.com/docs/free)：休眠、冷启动、共享时长及外部流量限制。
- [Blueprint JSON Schema](https://render.com/schema/render.yaml.json)：用于本地结构校验。

本地 Schema 校验只能证明配置结构符合公开规范，不能代替登录后平台部署、Tavily key 验证或真实 HTTPS/CORS/搜索验收。通用容器与本机启动方式见 [容器部署说明](WEB_CHECK_CONTAINER.md) 和 [联网接口说明](WEB_CHECK.md)。
