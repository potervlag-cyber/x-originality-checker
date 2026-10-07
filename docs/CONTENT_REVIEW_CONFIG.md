# 可选模型内容评估配置

模型评估使用独立的 OpenAI-compatible HTTPS Chat Completions API。现有搜索 key 不能替代模型 key。仅在服务端设置 `CONTENT_REVIEW_API_KEY`、`CONTENT_REVIEW_BASE_URL`（例如 `https://api.openai.com/v1`，服务会追加 `/chat/completions`）和供应商支持的 `CONTENT_REVIEW_MODEL`；不要将凭据放在网页、仓库、报告或聊天。`CONTENT_REVIEW_HOURLY_BUDGET` 默认 20，允许 1–10000，所有访问者共享滚动一小时模型请求预算；失败请求也占预算，POST 不自动重试。

未配置模型时搜索继续正常，帖子 `content_review.status` 为 `not_configured`，四项均明确未知。只读服务状态同时显示模型是否配置及剩余预算；配置就绪不证明模型调用成功，部署后必须以真实回读另行验证。此实现的自动测试只用注入的虚构 transport，不调用收费 API。

每次最多发送一条完整帖子（5000 字符）及最多三份实际抓取来源正文的前 6000 字符。来源正文视为不可信数据，无法通过内容指令改变请求或评分契约。输入 JSON 最多 160000 字节，响应最多 32000 字节、生成上限 1800 tokens，模型传输超时设置为 15 秒，系统 DNS 解析仍受系统超时影响。模型 endpoint 必须公开 HTTPS、标准端口，不允许口令、私网地址、查询参数或重定向；连接复用现有公开 DNS 验证与固定 IP/TLS 机制，不发送 cookies，不使用代理。

模型只对已取得来源正文且帖子完整的“原创贡献”，以及完整整帖是否完全围绕变现作辅助评分。模型没有创作流程、作者身份或许可材料，自动化和知识产权条款始终 `score:null, verdict:unknown`。缺来源、无搜索命中、正文截断或旧客户端未提供明确完整标记，不能换算为 100 分。客户端在状态接口返回 `content_review` 配置对象后，才额外发送一个 `text_complete` 布尔值；旧版服务、尚未完成或取消的状态请求继续使用四字段协议。服务端收到旧四字段调用时完整性默认未知，搜索继续兼容。

搜索全部失败、正文太短被跳过或实际检索文字不足输入正文时，服务跳过模型请求，四项保留未知；已配置模型的状态为 `failed`，原因代码为 `content_review_search_incomplete`，不占用模型预算。这样失败搜索的证据仍可被浏览器严格验证并纳入整批报告。尚未配置模型的状态继续为 `not_configured`。

响应保持搜索 `schema_version:1`，每条附加固定四项的 `content_review`。成功评分要求理由和帖子逐字摘录；原创贡献还要求引用实际发送模型的来源 URL 及逐字摘录。摘录最多 600 字符，理由最多 1000；后端严格验证 JSON、分数和证据，非法、超时或预算耗尽回传 `failed` 与稳定原因代码，不返回异常正文或 key。内容评分是已提供材料的模型辅助判断，不能表示官方符合认定、通过概率或全网原创证明；实际语义判断质量尚需独立评测。

## DeepSeek V4.1 Flash

2026-10-08 回读 DeepSeek 官方公开文档，以下页面均返回 HTTP 200。官方确认当前 V4.1 Flash 的 API 名称为 `deepseek-flash`，使用 OpenAI 兼容的 Chat Completions 格式：

- `CONTENT_REVIEW_BASE_URL`：`https://api.deepseek.com`，服务追加 `/chat/completions`。
- `CONTENT_REVIEW_MODEL`：`deepseek-flash`。
- `CONTENT_REVIEW_API_KEY`：在 Render 的 Environment 中填写该供应商的密钥，不放入网页、仓库或聊天。

官方默认启用 thinking、默认思考强度 high，且 thinking 模式忽略 temperature。本工具仅对官方 `api.deepseek.com` 主机且模型为 `deepseek-flash` 的请求加入 `thinking: {"type": "disabled"}`，包括根基址及 `/v1`；其他供应商或模型保留标准请求，不添加此供应商参数。评估理由要求简体中文，证据摘录保持来源原文。

官方支持 `response_format: {"type": "json_object"}`；JSON 指南要求提示中明确 JSON 格式，建议给出格式示例并合理设置 max_tokens，同时说明可能返回空内容。本工具保留 1800 tokens 的生成上限，空内容、截断或不满足固定四项契约的响应仍记为失败与未知，不自动重试。

官方价格按输入、输出 tokens 计费，并区分高峰与非高峰；小时请求预算不是货币硬上限。官方拥堵等待可能持续至十分钟仍未开始推理才关闭连接，本工具的模型传输超时设置仍为 15 秒，系统 DNS 解析受系统超时影响。这些是文档与离线请求回归验证，尚未用真实 DeepSeek API 验证响应时间、调用成功或模型判断质量。

- [官方基址与模型名称](https://api-docs.deepseek.com/)
- [Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Thinking 开关与参数兼容](https://api-docs.deepseek.com/guides/thinking_mode/)
- [JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)
- [当前价格](https://api-docs.deepseek.com/quick_start/pricing)
- [并发和等待限制](https://api-docs.deepseek.com/quick_start/rate_limit)
