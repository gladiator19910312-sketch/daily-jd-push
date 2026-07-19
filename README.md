# Sunny Daily Agent Job Radar

每天北京时间 08:30 搜索公开招聘信息，按职业目标进行确定性筛选，并通过钉钉自定义机器人推送新岗位。

岗位发现以官方 ATS 为主：国内覆盖 DeepSeek、阶跃星辰、月之暗面的 Moka；海外覆盖 OpenAI、Anthropic、Waymo、Scale AI、xAI、Perplexity、Sierra、Harvey 和 Waabi 的 Ashby / Greenhouse / Lever。Bing RSS 只用于补漏官方链接，搜索摘要不会被当成已验活岗位。

## 筛选逻辑

- 主轴：Agent / Agentic 产品、Evals、Benchmark、Quality、Reliability、Safety。
- 加分：多模态、驾驶/交通、高频真实场景、工具调用、MCP、context/memory、成本与延迟。
- 淘汰：FDE/FDSE、售前、驻场交付、纯工程岗位、高频差旅。
- 薪酬红线：公开薪酬明确低于私密底线时不推送；薪酬未披露的高匹配岗位允许进入，但会提示前置核验。`N 薪`只将 12 个月视为高确定固定现金。
- 可行性：全球岗位需要更高 Fit 门槛，每日最多占 2 个席位，避免淹没国内可行机会。
- 去重：GitHub Actions cache 保存已推送岗位。首次真实运行只发 Top N，同时将其余存量岗位设为基线，避免连续数天补推旧岗位。

评分是“是否值得进一步核验”，不是招聘成功率，也不会把未披露的双休、工时或薪酬推测成事实。

## GitHub Secrets

在仓库 `Settings → Secrets and variables → Actions` 创建：

- `DINGTALK_WEBHOOK`：完整的钉钉机器人 webhook。
- `DINGTALK_SECRET`：钉钉机器人加签密钥。
- `CURRENT_FIXED_CASH_WAN`：当前高确定固定现金底线，单位万元。
- `TARGET_TOTAL_COMP_WAN`：风险调整后可兑现总包目标，单位万元。

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

1. 第一次保留 `dry_run=true`，检查搜索和评分输出。
2. 确认无误后用 `dry_run=false` 手动发送测试消息。
3. 定时任务始终按真实推送运行。

## 调整

- 搜索词、评分阈值和公司偏好在 [`config.json`](config.json)；个人薪酬红线只从 GitHub Secrets 读取。
- 运行时间在 [`.github/workflows/daily-job-radar.yml`](.github/workflows/daily-job-radar.yml)；GitHub cron 使用 UTC。
- 系统只依赖 Python 标准库，不需要额外 API key 或付费搜索服务。

公开搜索可能受索引延迟、招聘网站反爬和动态页面影响。系统会在部分来源失败时继续执行，并在消息末尾标明覆盖异常。
