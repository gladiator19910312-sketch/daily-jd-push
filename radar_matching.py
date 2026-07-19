"""Deterministic career-fit, readiness, compensation, and risk scoring."""

from __future__ import annotations

import re
from typing import Any, Iterable

from radar_types import Assessment, Job, Salary


PRODUCT_ROLE_TERMS = (
    "product manager", "product lead", "product owner", "technical product manager",
    "deployed product manager", "ai product builder", "agent product", "evals lead",
    "evaluation lead", "benchmark lead", "technical deployment lead", "产品经理",
    "产品专家", "产品负责人",
)
TARGET_TITLE_TERMS = (
    "agent", "agentic", "autonomous", "eval", "benchmark", "quality", "reliability",
    "safety", "security", "model behavior", "model performance", "multimodal", "codex",
    "智能体", "大模型", "评测", "可靠性", "安全", "多模态",
)
DISALLOWED_TITLE_TERMS = (
    "product marketing", "growth product", "gtm ", "go-to-market", "customer success",
    "sales", "产品运营", "增长产品", "市场营销",
)


def contains_any(text: str, terms: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def count_matches(text: str, groups: Iterable[tuple[Iterable[str], int]]) -> int:
    return sum(weight for terms, weight in groups if contains_any(text, terms))


def looks_like_product_job(job: Job) -> bool:
    return contains_any(job.title, PRODUCT_ROLE_TERMS)


def has_target_title(title: str) -> bool:
    return contains_any(title, TARGET_TITLE_TERMS) or bool(re.search(r"(?i)\bAI\b", title))


def has_transport_domain(text: str) -> bool:
    return contains_any(
        text,
        (
            "vehicle", "autonomous driving", "self-driving", "transportation", "mobility",
            "cockpit", "驾驶", "交通", "座舱", "出行", "自动驾驶",
        ),
    )


def job_has_transport_domain(job: Job) -> bool:
    return has_transport_domain(job.title) or contains_any(job.source, ("Waymo", "Waabi"))


def parse_salary(text: str, usd_cny: float = 7.0) -> Salary:
    compact = text.replace(",", "")
    monthly = re.search(
        r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*[-–—~至]\s*(\d{1,3}(?:\.\d+)?)\s*[kKＫ]"
        r"(?:\s*[·xX×*]\s*(\d{2})\s*薪)?",
        compact,
    )
    if monthly:
        low, high = float(monthly.group(1)), float(monthly.group(2))
        months = int(monthly.group(3) or 12)
        return Salary(
            round(low * months / 10, 1), round(high * months / 10, 1),
            round(low * 12 / 10, 1), round(high * 12 / 10, 1),
            f"{low:g}–{high:g}K×{months}（名义）",
        )

    annual_wan = re.search(
        r"(?<!\d)(\d{2,4}(?:\.\d+)?)\s*[-–—~至]\s*(\d{2,4}(?:\.\d+)?)\s*万(?:元)?\s*/?\s*年",
        compact,
    )
    if annual_wan:
        low, high = float(annual_wan.group(1)), float(annual_wan.group(2))
        return Salary(low, high, None, None, f"{low:g}–{high:g}万/年")

    usd_k = re.search(
        r"\$\s*(\d{2,4}(?:\.\d+)?)\s*[kK]\s*[-–—~]\s*\$?\s*(\d{2,4}(?:\.\d+)?)\s*[kK]",
        compact,
    )
    usd_full = re.search(
        r"\$\s*(\d{5,7}(?:\.\d+)?)\s*[-–—~]\s*\$?\s*(\d{5,7}(?:\.\d+)?)",
        compact,
    )
    match = usd_k or usd_full
    if match:
        divisor = 1 if usd_k else 1000
        low, high = float(match.group(1)) / divisor, float(match.group(2)) / divisor
        return Salary(
            round(low * usd_cny / 10, 1), round(high * usd_cny / 10, 1),
            round(low * usd_cny / 10, 1), round(high * usd_cny / 10, 1),
            f"US${low:g}K–{high:g}K base（汇率估算）",
        )
    return Salary()


def salary_gate(salary: Salary, config: dict[str, Any]) -> tuple[str, bool]:
    floor = float(config["current_fixed_cash_wan"])
    target = float(config["target_total_comp_wan"])
    if salary.total_high_wan is None:
        return "薪酬未披露，必须前置核验", False
    if salary.total_high_wan < target:
        return f"名义上沿 {salary.total_high_wan:g} 万，低于 {target:g} 万红线", True
    if salary.fixed_high_wan is not None and salary.fixed_high_wan < floor:
        return f"12个月固定上沿 {salary.fixed_high_wan:g} 万，低于当前 {floor:g} 万底线", True
    if salary.total_low_wan is not None and salary.total_low_wan >= target:
        return "公开区间下沿已达到总包目标", False
    return "仅公开区间上沿达到目标，需核验保底现金", False


def responsibility_tags(text: str) -> tuple[str, ...]:
    groups = (
        (("eval", "evaluation", "benchmark", "评测", "badcase", "失败归因"), "任务定义、Benchmark 与失败归因"),
        (("tool use", "tool calling", "mcp", "context", "memory", "multi-agent", "工具调用", "记忆", "多智能体"), "Agent 系统栈与工具使用"),
        (("roadmap", "end-to-end", "ownership", "launch", "路线图", "端到端", "上线"), "产品路线与端到端闭环"),
        (("safety", "reliability", "security", "安全", "可靠性", "可控性"), "安全、可靠性与可控性"),
        (("prototype", "api", "python", "typescript", "vibe coding", "原型"), "原型、API 与实验验证"),
        (("cost", "latency", "roi", "business outcome", "成本", "延迟", "业务结果"), "成本、延迟与业务结果"),
    )
    tags = [label for terms, label in groups if contains_any(text, terms)]
    return tuple(tags[:3]) or ("复杂 AI 产品的问题定义与落地",)


def assess_job(job: Job, config: dict[str, Any]) -> Assessment:
    text = job.text
    fit = 10 + count_matches(
        text,
        [
            (("agent", "agentic", "智能体"), 12),
            (("eval", "evaluation", "评测", "benchmark", "quality", "可靠性"), 16),
            (("multimodal", "多模态", "vision", "视觉"), 7),
            (("tool use", "tool calling", "mcp", "context", "memory", "工具调用", "上下文", "记忆"), 8),
            (("safety", "security", "high-consequence", "安全", "风控"), 7),
            (("prototype", "builder", "api", "python", "typescript", "原型"), 6),
            (("cost", "latency", "roi", "business outcome", "成本", "延迟", "业务结果"), 6),
            (("roadmap", "end-to-end", "ownership", "launch", "路线图", "端到端", "上线"), 8),
        ],
    )
    fit += 18 if looks_like_product_job(job) else 0
    fit += 22 if has_target_title(job.title) else -25
    if contains_any(job.title, ("eval", "evaluation", "benchmark", "评测")):
        fit += 16

    ready = 42 + count_matches(
        text,
        [
            (("product manager", "product lead", "产品经理", "产品专家"), 10),
            (("eval", "评测", "benchmark", "quality"), 9),
            (("multimodal", "多模态", "vision", "视觉"), 8),
            (("consumer", "c端", "large scale", "millions", "规模化"), 5),
        ],
    )
    if job_has_transport_domain(job):
        ready += 16

    strengths: list[str] = []
    gaps: list[str] = []
    if contains_any(text, ("eval", "评测", "benchmark", "quality")):
        strengths.append("Benchmark/评测主轴匹配")
    if job_has_transport_domain(job):
        strengths.append("驾驶/交通真实场景可直接迁移")
    if contains_any(text, ("multimodal", "多模态", "vision", "视觉")):
        strengths.append("多模态经验匹配")
    if contains_any(text, ("tool use", "mcp", "context", "memory", "工具调用", "上下文", "记忆")):
        gaps.append("需证明 Agent 系统栈与工具调用深度")
    if contains_any(text, ("software development", "engineering background", "coding", "python", "typescript", "代码", "工程背景")):
        ready -= 12
        gaps.append("亲手编码/原型证据不足")
    if contains_any(text, ("developer platform", "sdk", "api product", "开发者平台", "开放平台")):
        ready -= 7
        gaps.append("开发者平台/API 产品经验需补齐")
    if contains_any(job.title, ("数据策略", "数据产品")):
        fit -= 8
        ready -= 1
        gaps.append("偏数据策略，需核验是否拥有端到端产品与评测闭环")
    if contains_any(text, ("trust & safety", "integrity", "safeguard", "regulated", "安全测量", "合规")):
        ready -= 7
        gaps.append("正式 AI Safety/风险度量经验不足")
    if contains_any(text, ("8+ years", "八年以上", "8年以上")):
        ready -= 6
        gaps.append("总相关年限需确认满足硬门槛")
    if contains_any(
        f"{text}\n{job.source}\n{job.company}",
        config.get("preferred_companies", ()),
    ):
        fit += 4

    exclusion = None
    if contains_any(job.title, DISALLOWED_TITLE_TERMS):
        exclusion = "GTM/增长/营销产品身份偏离目标"
    elif contains_any(text, ("forward deployed engineer", "fde ", "fdse", "solutions engineer", "售前", "驻场", "客户交付", "customer implementation")):
        exclusion = "FDE/售前/驻场交付身份冲突"
    elif contains_any(text, ("software engineer", "machine learning engineer", "applied ai engineer", "算法工程师")) and not looks_like_product_job(job):
        exclusion = "工程岗位，不是产品 IC"
    elif contains_any(text, ("frequent travel", "heavy travel", "高频出差", "长期出差")):
        exclusion = "高频差旅与生活边界冲突"

    work_risk = "工时/差旅未披露"
    if contains_any(text, ("fast-paced", "startup environment", "high intensity", "快节奏", "高强度")):
        work_risk = "快节奏/高强度信号，需核验晚间与周末"
    if contains_any(text, ("on-call", "travel", "出差", "客户现场")):
        work_risk = "存在 on-call/差旅/客户现场信号"

    salary = parse_salary(text, float(config.get("usd_cny", 7.0)))
    gate_label, salary_reject = salary_gate(salary, config)
    if salary_reject:
        exclusion = exclusion or "公开薪酬低于风险调整后总包红线"

    fit = max(0, min(100, fit))
    ready = max(0, min(100, ready))
    asset_points = count_matches(
        text,
        [
            (("eval", "评测", "benchmark", "quality"), 2),
            (("agent", "agentic", "智能体"), 1),
            (("safety", "reliability", "安全", "可靠性"), 1),
            (("launch", "roadmap", "end-to-end", "上线", "路线图", "端到端"), 1),
        ],
    )
    if contains_any(text, ("售前", "驻场", "customer implementation", "forward deployed")):
        asset_points -= 2
    asset = "极高" if asset_points >= 5 else "高" if asset_points >= 3 else "中"
    strengths = strengths or ["复杂问题定义与跨团队落地可迁移"]
    gaps = gaps or ["Agent 原型、trace 与失败归因深度需核验"]
    return Assessment(
        job, fit, ready, asset, salary, gate_label, responsibility_tags(text),
        tuple(strengths[:3]), tuple(dict.fromkeys(gaps))[:3], work_risk, exclusion,
    )


def rank_assessments(assessments: Iterable[Assessment], config: dict[str, Any]) -> list[Assessment]:
    fit_threshold = int(config["fit_threshold"])
    ready_threshold = int(config["ready_threshold"])
    global_threshold = int(config.get("global_fit_threshold", 88))
    require_official = bool(config.get("require_official_source", True))
    eligible: list[Assessment] = []
    for item in assessments:
        if not item.eligible or item.fit < fit_threshold or item.ready < ready_threshold:
            continue
        if require_official and not item.job.official:
            continue
        relocation = contains_any(item.job.text, ("visa sponsorship", "relocation", "签证", "搬迁"))
        if item.job.scope == "global" and item.fit < global_threshold and not relocation:
            continue
        eligible.append(item)
    return sorted(eligible, key=lambda item: item.fit * 0.65 + item.ready * 0.35, reverse=True)


def select_for_push(items: Iterable[Assessment], max_jobs: int, max_global: int) -> list[Assessment]:
    selected: list[Assessment] = []
    global_count = 0
    for item in items:
        if item.job.scope == "global":
            if global_count >= max_global:
                continue
            global_count += 1
        selected.append(item)
        if len(selected) >= max_jobs:
            break
    return selected


def select_diverse_assessments(
    items: Iterable[Assessment],
    max_items: int,
) -> list[Assessment]:
    """Prefer different employers before using a second slot for one source."""
    values = list(items)
    selected: list[Assessment] = []
    seen_sources: set[str] = set()
    for item in values:
        source = item.job.company or item.job.source_key or item.job.source
        if source.casefold() in seen_sources:
            continue
        selected.append(item)
        seen_sources.add(source.casefold())
        if len(selected) >= max_items:
            return selected
    for item in values:
        if item not in selected:
            selected.append(item)
        if len(selected) >= max_items:
            break
    return selected
