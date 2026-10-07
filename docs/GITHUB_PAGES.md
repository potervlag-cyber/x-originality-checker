# GitHub Pages 部署说明

本项目可以部署为纯静态 Pages 网站。Python 检查规则由浏览器内的 Pyodide 运行，不需要远端 Python 服务。

## 首次发布

1. 将公开源码提交到 `potervlag-cyber/x-originality-checker` 的 `main` 分支。根 README 已包含公开使用说明。
2. 在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
3. 在 **Actions** 查看 **Publish browser app to GitHub Pages**。首次推送或手动 **Run workflow** 会先运行自动检查，再构建、上传并部署静态站点。
4. 部署成功后访问 <https://potervlag-cyber.github.io/x-originality-checker/>，上传虚构 X 归档 ZIP，核对自动解析、全量统计、原因和取消/更换文件。

公开仓库仅需本应用源码、测试、浏览器适配、固定运行时清单、材料说明模板及工作流。不要提交用户项目、报告、真实材料、`qa`、Obsidian 暂存、凭据或本机环境路径。构建器不会复制这些文件。

## 本地构建

```sh
python build_site.py --output _site --download-runtime
python -m http.server 8000 --directory _site
```

打开 <http://localhost:8000/>。首次构建只从固定 HTTPS 路径下载所需运行时，保存到 `vendor/pyodide` 缓存；再次构建复用经过 SHA-256 校验的缓存，可省略 `--download-runtime`。

为核验仓库子路径，可把输出指向独立目录 `pages-preview/x-originality-checker`，以 `pages-preview` 为 HTTP 根目录，然后打开 `/x-originality-checker/`。HTML、Worker、Python 模块和运行时都采用相对路径；不能通过双击 `index.html` 的 `file:` 方式运行。

构建固定使用 Pyodide 0.27.7 / CPython 3.12.7。浏览器访问同源 `vendor/pyodide`，不会临时请求第三方包；`deployment/runtime-lock.json` 记录许可来源、精确下载地址、长度和 SHA-256。更新运行时应先核验来源与浏览器功能，再同步修改版本和清单。

## 发布权限

工作流构建任务只需 `contents: read`；部署任务单独授予 `pages: write` 与 `id-token: write`。没有个人访问 token、X 登录或第三方分析服务。GitHub 官方 Actions 使用已核验的主版本发行标记；`main` 推送和 `workflow_dispatch` 是发布入口。

## 验收

- Actions 构建与 Pages 部署均成功，公开 URL 的界面和所有同源依赖返回成功。
- 在 `/x-originality-checker/` 路径打开页面，浏览器引擎完成初始化。
- 使用虚构 X 归档 ZIP 得到预期分类；全部记录数量、帖子类型、分片及媒体关联正确，不在 2,000 条截断。
- 300 MB 边界的有效 ZIP 可解析；超过边界明确拒绝。归档按片段读取，无整个文件 Base64 复制。
- 导入与运行期间网络只读取站点资源，不发送用户材料。
- 分析可取消和重新开始；错误后能重新选文件。主卡显示工具规则估计的原创通过概率、主观参考范围、判断把握与因素；未重复占比独立显示。规则尚未用真实审核结果校准，只估计原创部分；没有可比较本人正文时明确显示材料不足，官方概率字段仍为null。

公开访问与官方审核一致性属于不同验收项；发布成功只证明该页面可以访问并完成本工具已实现的检查。
