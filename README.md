# Sunny Daily Agent Job Radar

每天早间搜索公开招聘信息，按 Sunny 的 Agent 产品 / Evals 高级 IC 目标进行确定性筛选，并通过钉钉自定义机器人推送。默认的 GitHub Actions 定时任务只读取企业官方 ATS / 招聘站与可公开访问的 Web 来源；需要登录态的 Agent Reach 搜索在本机执行，只能以脱敏 JSON 补充包并入报告。

系统把「北京 / 天津的可行动岗位」与「其他城市、海外、时效待核验岗位和内容平台的行业信号」分开。商业招聘平台、公众号或小红书的搜索摘要不会被冒充为已验活岗位。

## 推送分层

钉钉报告分为四个独立区域：

1. **北京 / 天津｜社招优先可行动岗位**：地点明确包含北京或天津，来源、招聘状态和时效符合要求；优先展示不同雇主，每日最多 6 个。
2. **非主推｜其他城市 / 海外 / 时效待核验**：用于观察角色、技术栈和人才需求；北京 / 天津岗位只有在时效不足以进入主池时才会出现在这里，每日最多 4 个。
3. **社招高阶线索｜招聘平台 / 公共就业 / 人才网**：只接收已读到有效正文、有可识别职责的职位详情页。搜索聚合页、SEO 模板页、安全验证页和仅有搜索摘要的链接一律不输出；当前默认配额为 0。
4. **行业报告 / 公众号 / 小红书｜趋势参考**：本机 Agent Reach 实际检索到并脱敏后的薪酬、人才和行业内容，每日最多 4 条，不占用岗位名额。原始链接依赖临时登录凭证时，报告只展示实读摘要而不生成失效链接。

只有第一区域作为当日优先核验和投递候选。

## 地点与时效规则

- **地点优先级：**北京、天津为主池；其他国内城市和海外岗位只进非主推池。地点未披露、「全国远程」等无法确认属地的岗位不进北京 / 天津主池。
- **90 天优先：**有可信发布 / 创建日期且不超过 90 天的岗位优先。
- **180 天硬上限：**91–180 天的岗位会标注「需再次确认」；超过 180 天不进入任何岗位池。
- **更新日不冒充发布日：**官网只给出「更新日」时，90 天内可进主池，但会明确标注「非发布时间」；91–180 天只作趋势参考。
- **日期未知保守处理：**官方源能确认当前在招、但没有可用日期时，只进趋势池。
- **状态优先于日期：**已截止、已关闭、已过期或无法确认仍在招的岗位不作为可行动机会。

## 来源覆盖与边界

### 官方岗位源

国内直接读取当前开放的官方 ATS / 招聘接口：

- DeepSeek、阶跃星辰、月之暗面、北京智源研究院（BAAI）、元戎启行（DeepRoute）的 Moka 官方职位源。
- 字节跳动、阿里巴巴、美团和腾讯的企业招聘站社招接口；字节、阿里使用官网正常匿名 CSRF / XSRF 会话，美团会用详情接口补首次发布时间和岗位要求。
- 接口按 Agent、智能体、评测、大模型产品、多模态、模型质量和高阶产品责任等关键词低频扫描；单个关键词失败不会丢掉同源已取得的结果。

海外趋势池直接读取 OpenAI、Anthropic、Waymo、Scale AI、xAI、Perplexity、Sierra、Harvey、Waabi 和 Mistral AI 的 Ashby / Greenhouse / Lever 官方职位源。

系统还会用公开搜索为企业招聘站做兜底发现。只有雇主自己的招聘域名可以通过详情页验活后升级为「企业官方」；国聘、就业在线、人社平台和人才网即使页面存在，也不会被误标成企业官网岗位。

### GitHub Actions 的公开来源

GitHub 托管运行器没有用户本机的登录会话。定时任务因此不声称“已覆盖” BOSS 直聘、小红书、微信公众号或脉脉的登录后内容，也不把搜索引擎摘要当成职位正文。

- 岗位来源以企业官方 ATS / 招聘站和可公开验活的雇主详情页为主。
- 公共就业和人才网可用于公开 Web 发现，但不会逆向国聘、就业在线等站点的签名或加密接口。
- 聚合页、列表页、安全验证页、空正文和无法直达的 SEO 链接不会出现在推送中。

### 本机 Agent Reach 补充源

本机收集器使用用户已登录的 Agent Reach / OpenCLI 会话搜索，然后只导出报告所需的标题、日期、摘要、来源和覆盖计数。浏览器 Cookie、Authorization、`xsec_token`、搜狗临时 token、用户 ID 和原始响应不得进入补充包、GitHub Secrets 或 Actions 日志。

- **小红书：**可在本机搜索并实读笔记正文。依赖 `xsec_token` 的原始链接不会输出，因此报告可能只展示已读摘要。
- **微信公众号：**可导出搜索结果中的标题、日期和摘要；只有稳定的 `mp.weixin.qq.com` 原文链接会保留，搜狗签名跳转链接会被丢弃。
- **BOSS 直聘：**当前补充入口保持关闭，直到实现可验证的正文实读证明；未登录、落到安全页或只取得搜索模板时显示为未执行 / 需登录，不会造出线索。
- **脉脉：**当前 OpenCLI 适配器不支持职位或“职言”搜索，覆盖状态必须标记为 `unsupported`，不得用 0 条冒充已搜索。

报告会区分 `ok`、`no_results`、`auth_required`、`unsupported`、`error` 和 `skipped`，并展示查询次数、原始结果数、相关结果数和正文实读数。没有本机补充包的普通定时运行会明确标记这些渠道未执行，而不是报告 0 条。

### 每日调度与云端兜底

本机 Codex automation 每天先运行 `scripts/dispatch_agent_reach.py --send --wait`：使用现有登录态采集、在本机完成脱敏和严格校验，再触发 GitHub Actions。浏览器 Cookie 和平台临时 token 始终留在本机。Mac 关机、网络中断或登录失效时，本机任务会失败并提醒，不会发送伪造的 0 结果。

GitHub Actions 的 08:30 定时任务保留为云端兜底：若两小时内已有一次本机补充包触发的成功推送，就只记录“近期已推送”并跳过；若本机任务没有成功，云端仍会发送企业官网岗位，并诚实标记登录态渠道未执行。

## 职业匹配规则

- **主轴：**Agent / Agentic 产品、Evals、Benchmark、Quality、Reliability、Safety。
- **宽召回：**除标准产品经理标题外，也接纳 AI 应用负责人、智能体业务负责人、模型质量负责人、Head of Evaluation、Staff / Principal / Lead 等非标准高阶 IC 标题；薪资未披露和非预设创业公司不会在发现层被提前淘汰。
- **加分：**多模态、驾驶 / 交通、高频真实场景、工具调用、MCP、context / memory、成本与延迟。
- **淘汰：**实习、校招、应届、New Grad、管培生；FDE / FDSE、售前、驻场交付、纯工程岗位、高频差旅。
- **薪酬红线：**公开薪酬明确低于私密底线时不推送；薪酬未披露的高匹配岗位允许进入，但会提示前置核验。`N 薪`只将 12 个月视为高确定固定现金。
- **可归因评估：**报告会输出岗位重点、Fit / Ready、两年职业资产、现有优势、关键缺口、薪酬口径以及强度 / 差旅风险。

评分表示「是否值得进一步核验」，不是招聘成功率，也不会把未披露的双休、工时、差旅或薪酬推测成事实。

## 去重与失败处理

- GitHub Actions cache 保存各条记录的最后发现时间；消失超过 180 天的记录会过期，使同一 requisition 真正复开后可以再次提醒。
- 同一岗位从趋势池变成北京 / 天津可行动岗位时，可再推送一次。
- 已验活的平台岗位线索和行业内容使用独立配额并按来源轮换；**岗位平台**搜索摘要、空页或临时签名链接不会写入基线，也不会占用推送配额。公众号公开索引摘要会明确标注为“检索摘要”，仅作为行业参考。
- 单个来源失败不会中断整轮扫描；报告末尾会显示异常总数，企业官网成功率低于一半时会明确给出来源健康告警。
- 官方源、官网搜索兜底和平台信号各有独立时间预算；Moka、腾讯及大厂关键词接口都有硬分页上限。坏网或限流时会停止剩余查询并在报告中告警，避免整轮被 Actions 强制终止。

## GitHub Secrets

在仓库 `Settings → Secrets and variables → Actions` 创建：

- `DINGTALK_WEBHOOK`：完整的钉钉机器人 webhook。
- `DINGTALK_SECRET`：钉钉机器人加签密钥。
- `CURRENT_FIXED_CASH_WAN`：当前高确定固定现金底线，单位万元。
- `TARGET_TOTAL_COMP_WAN`：风险调整后可兑现总包目标，单位万元。

**Agent Reach 补充包无需新增 Secret、搜索 API key 或付费服务。**仍只需上述四项 GitHub Secrets。补充包通过手动运行的普通 workflow input 传入，该字段不是密文保管库，所以其内容必须先脱敏。

不要把这些值写进代码、Issue、Actions 日志或 `.env`。如果机器人凭证曾在聊天、截图或日志中以明文出现，应在钉钉后台轮换后再保存到 GitHub Secrets。CI 会扫描常见的钉钉凭证形式并拒绝提交。

## 运行

本地 dry-run，不会发送钉钉：

```bash
python3 -m unittest discover -s tests -v
CURRENT_FIXED_CASH_WAN='<private>' \
TARGET_TOTAL_COMP_WAN='<private>' \
python3 job_radar.py --dry-run --force-all
```

GitHub 页面进入 `Actions → Daily Agent Job Radar → Run workflow`：

1. 第一次保留 `dry_run=true`，检查搜索、分池和评分输出。
2. 确认无误后用 `dry_run=false` 手动发送测试消息。
3. 定时任务始终按真实推送运行。

### 手动携带 Agent Reach 补充包

先在已登录 Agent Reach / OpenCLI 的本机生成脱敏 JSON：

```bash
python3 scripts/collect_agent_reach.py --output /tmp/agent-reach-supplement.json
python3 -m json.tool /tmp/agent-reach-supplement.json
```

确认文件中没有 Cookie、Authorization、token、个人联系方式、用户 ID 或原始响应后，在 macOS 上将单行 base64 复制到剪贴板：

```bash
base64 < /tmp/agent-reach-supplement.json | tr -d '\n' | pbcopy
```

然后在 `Run workflow` 面板中：

1. 保持 `dry_run=true`、`force_all=false`。
2. 把剪贴板内容粘贴到 `agent_reach_supplement_b64`。
3. 运行后检查“Agent Reach 实际覆盖”计数和每条摘要；确认无敏感内容再改为 `dry_run=false` 发送。

Actions 只在该字段非空时解码补充包，文件权限限制为当前用户可读写，并使用 `--supplement --require-supplement` 严格校验；编码内容不会被打印。无补充包的定时运行不受影响。

也可以让本机安全封装一次完成采集、校验、触发和等待结果：

```bash
python3 scripts/dispatch_agent_reach.py --send --wait
```

## 调整

- 地点、时效窗口、官方源、搜索词、趋势站点、评分阈值和每日上限在 [`config.json`](config.json)；个人薪酬红线只从 GitHub Secrets 读取。
- 运行时间在 [`.github/workflows/daily-job-radar.yml`](.github/workflows/daily-job-radar.yml)；GitHub cron 使用 UTC。
- GitHub Actions 扫描器只依赖 Python 标准库；本机补充收集另需要已安装并完成相应渠道登录的 Agent Reach / OpenCLI。

公开搜索可能受索引延迟、站点收录范围、招聘平台反爬和动态页面影响。本机实读也不等于企业背书；平台或社交内容中的薪酬、工时和岗位状态始终需要在企业官网、面试和反向背调中二次确认。
