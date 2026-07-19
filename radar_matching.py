"""Deterministic career-fit, readiness, compensation, and risk scoring."""

from __future__ import annotations

import re
from typing import Any, Iterable

from radar_types import Assessment, Job, Salary


PRODUCT_ROLE_TERMS = (
    "product manager", "product lead", "product owner", "technical product manager",
    "deployed product manager", "ai product builder", "agent product", "evals lead",
    "evaluation lead", "benchmark lead", "technical deployment lead", "产品经理",
    "产品专家", "产品负责人", "产品总监",
)
TARGET_TITLE_TERMS = (
    "agent", "agentic", "autonomous", "eval", "benchmark", "quality", "reliability",
    "safety", "security", "model behavior", "model performance", "multimodal", "codex",
    "智能体", "大模型", "评测", "可靠性", "安全", "多模态", "模型质量", "模型效果",
    "模型评估", "模型测评",
)
SENIOR_ROLE_TERMS = (
    "senior", "staff", "principal", "lead", "head", "director", "expert", "owner",
    "高级", "资深", "专家", "负责人", "总监", "首席", "p7", "p8", "p9",
)
OWNERSHIP_ROLE_TERMS = (
    "product", "business", "strategy", "platform", "application", "产品", "业务", "战略",
    "平台", "应用", "质量",
)
ENGINEERING_TITLE_TERMS = (
    "engineer", "engineering", "developer", "scientist", "researcher", "architect",
    "工程师", "工程专家", "研发", "算法", "研究员", "科学家", "架构师", "开发",
)
DISALLOWED_TITLE_TERMS = (
    "product marketing", "growth product", "gtm ", "go-to-market", "customer success",
    "business development", "partnerships", "product operations", "sales", "产品运营",
    "运营负责人", "运营专家", "商务拓展", "业务拓展", "渠道销售", "增长产品", "市场营销",
)

EXPERIENCE_PATTERNS = (
    # Ranges use the lower bound: "6-8 years of experience" means a six-year floor.
    r"(?i)(?<!\d)(\d{1,2})\s*[-–—]\s*\d{1,2}\s*(?:years?|yrs?)"
    r"(?:\s+of)?[^.\n;]{0,36}\b(?:experience|background)\b",
    r"(?i)(?<![-–—\d])(\d{1,2})\s*(?:\+|or\s+more)?\s*(?:years?|yrs?)"
    r"(?:['’])?(?:\s+of)?[^.\n;]{0,36}\b(?:experience|background)\b",
    r"(?i)\b(?:at\s+least|minimum(?:\s+of)?|experience\s*(?::|required\s*:?)?)\s*"
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b",
    r"(?i)(?<![-–—\d])(\d{1,2})\s*\+\s*(?:years?|yrs?)\b"
    r"(?=\s+(?:of|in|building|working|leading|managing|designing|developing|shipping)\b)",
    r"(?<!\d)(\d{1,2})\s*[-–—至到]\s*\d{1,2}\s*年[^\n。；;]{0,24}经验",
    r"(?<![-–—至到\d])(\d{1,2})\s*(?:\+)?\s*年(?:以上|及以上|或以上)?"
    r"[^\n。；;]{0,24}经验",
    r"(?:经验要求|工作经验|工作年限|相关经验)\s*(?:[:：]|(?:为|不少于|至少))?\s*"
    r"(\d{1,2})(?:\s*[-–—至到]\s*\d{1,2})?\s*(?:\+)?\s*年"
    r"(?:以上|及以上|或以上)?",
)

PEOPLE_MANAGEMENT_PATTERNS = (
    r"(?i)\b(?:direct\s+reports?|people\s+manager|people\s+management|performance\s+reviews?|"
    r"succession\s+planning|organizational\s+development)\b",
    r"(?i)\b(?:own|conduct|responsible\s+for)\s+(?:employee|team|people)?\s*"
    r"(?:performance\s+management|performance\s+reviews?)\b",
    r"(?i)\b(?:manage|build|grow|hire|lead)\s+(?:and\s+\w+\s+){0,2}(?:a\s+)?"
    r"(?:team|organization|org)\s+of\s+\d+",
    r"(?i)\b(?:manage|lead|build|grow)\s+(?:and\s+lead\s+)?(?:a\s+)?"
    r"(?:product\s+managers?|product\s+management\s+team)\b",
    r"(?i)\b(?:responsible\s+for\s+)?hiring[^.\n;]{0,50}"
    r"(?:coach|develop|performance|organization|org\s+design)\b",
    r"(?:直属|直接)下属|人员管理|人才梯队|组织建设|"
    r"(?:员工|人员|团队|下属)[^\n。；;]{0,5}绩效管理|绩效管理[^\n。；;]{0,5}(?:员工|人员|团队|下属)",
    r"(?:管理|带领|负责)\s*\d+\s*人(?:以上)?(?:的)?团队",
    r"(?:管理|带领|搭建|组建)[^\n。；;]{0,12}(?:产品经理团队|产品团队)",
    r"(?:招聘|招募)[^\n。；;]{0,20}(?:绩效|培养|人才梯队|组织建设)",
)


def contains_any(text: str, terms: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def count_matches(text: str, groups: Iterable[tuple[Iterable[str], int]]) -> int:
    return sum(weight for terms, weight in groups if contains_any(text, terms))


def required_experience_years(text: str) -> int | None:
    """Return the strictest explicitly stated experience floor in a JD."""
    values = [
        int(match.group(1))
        for pattern in EXPERIENCE_PATTERNS
        for match in re.finditer(pattern, text)
        if 1 <= int(match.group(1)) <= 30
    ]
    return max(values, default=None)


def has_people_management_requirement(text: str) -> bool:
    """Detect explicit people-management ownership, not project leadership."""
    without_negated_requirements = re.sub(
        r"(?i)\b(?:no|without)\s+direct\s+reports?\b|"
        r"\bnot\s+(?:a\s+)?people\s+manager\b|"
        r"\bno\s+people\s+management\b|"
        r"(?:无|没有|不设)(?:直属|直接)下属|"
        r"不(?:需|涉及|承担)(?:人员|团队)管理",
        "",
        text,
    )
    return any(re.search(pattern, without_negated_requirements) for pattern in PEOPLE_MANAGEMENT_PATTERNS)


def _travel_percentage(text: str) -> tuple[int | None, str]:
    patterns = (
        r"(?i)(?:travel|business\s+travel)[^%\n.]{0,24}?(\d{1,3})\s*%",
        r"(?i)(\d{1,3})\s*%[^\n.]{0,12}(?:travel|business\s+travel)",
        r"(?:出差|差旅)(?:比例|频率|要求)?[^%\n。]{0,12}?(\d{1,3})\s*%",
        r"(\d{1,3})\s*%[^\n。]{0,10}(?:出差|差旅)",
    )
    values: list[tuple[int, str]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            value = int(match.group(1))
            if not 0 <= value <= 100:
                continue
            context = text[max(0, match.start() - 20): min(len(text), match.end() + 20)].casefold()
            if re.search(r"less\s+than|under|低于|少于|小于|<", context):
                comparator = "less_than"
            elif re.search(r"up\s+to|at\s+most|不超过|至多|以内|≤", context):
                comparator = "up_to"
            elif re.search(r"at\s+least|more\s+than|不少于|至少|超过|以上|>", context):
                comparator = "at_least"
            else:
                comparator = "stated"
            values.append((value, comparator))
    if not values:
        return None, ""
    return max(values, key=lambda item: item[0])


def work_boundary_assessment(text: str) -> tuple[str, str | None]:
    """Summarize only explicit work-boundary evidence and return any hard conflict."""
    no_travel = bool(
        re.search(
            r"(?i)\b(?:no|without|zero)\s+(?:business\s+)?travel\b|"
            r"\b(?:does\s+not|doesn't|not)\s+require\s+(?:business\s+)?travel\b|"
            r"\btravel\s+(?:is\s+)?not\s+required\b|"
            r"(?:无|无需|不需|不涉及|不要求|零)(?:任何)?(?:出差|差旅)",
            text,
        )
    )
    no_on_call = bool(
        re.search(
            r"(?i)\b(?:no|without|not(?:\s+an?)?)\s+on[- ]call\b|"
            r"\bon[- ]call\s+(?:is\s+)?not\s+required\b|"
            r"(?:无需|不需|不涉及|没有|无)(?:on[- ]call|值班|轮值|待命)",
            text,
        )
    )
    double_weekend_denied = bool(
        re.search(r"(?:不|非|无法|不保证|不是)[^\n。；;]{0,6}双休|单双休", text)
    )
    double_weekend = not double_weekend_denied and bool(
        re.search(
            r"(?i)\b(?:five[- ]day\s+work(?:week|\s+week)|weekends?\s+off)\b|"
            r"双休|周末双休|五天工作制",
            text,
        )
    )
    travel_percentage, travel_comparator = _travel_percentage(text)
    frequent_travel = bool(
        re.search(
            r"(?i)\b(?:frequent|extensive|heavy)\s+(?:business\s+)?travel\b|"
            r"\btravel\s+(?:frequently|extensively)\b|"
            r"(?:高频|频繁|长期|经常)[^\n。；;]{0,5}(?:出差|差旅)",
            text,
        )
    )
    if no_travel and not frequent_travel and (travel_percentage or 0) == 0:
        travel_percentage = None
    else:
        no_travel = False
    travel_required = not no_travel and bool(
        frequent_travel
        or travel_percentage is not None
        or re.search(
            r"(?i)\b(?:travel\s+(?:is\s+)?required|requires?\s+(?:business\s+)?travel|"
            r"willing(?:ness)?\s+to\s+travel|business\s+travel)\b|"
            r"(?:需要|需|要求|接受)[^\n。；;]{0,5}(?:出差|差旅)|(?:出差|差旅)要求",
            text,
        )
    )
    on_call = not no_on_call and bool(
        re.search(r"(?i)\bon[- ]call\b|值班|轮值|待命", text)
    )
    customer_site = bool(
        re.search(
            r"(?i)\b(?:customer|client)[- ]site\b|\bon[- ]site\s+(?:at|with)\s+(?:customers?|clients?)\b|"
            r"客户现场|客户驻场|现场交付",
            text,
        )
    )
    cross_timezone = bool(
        re.search(
            r"(?i)\b(?:cross[- ]time[- ]zone|across\s+(?:global|multiple)\s+time\s+zones?|"
            r"global\s+time[- ]zone)\b|跨时区|海外时区",
            text,
        )
    )
    weekend_or_night = bool(
        re.search(
            r"(?i)\b(?:weekend|evening|night)\s+(?:work|coverage|shift)s?\b|"
            r"\bwork(?:ing)?\s+(?:on\s+)?weekends?\b|"
            r"周末(?:需|需要|安排|加班)|晚间(?:需|需要|工作|加班)|夜班",
            text,
        )
    )
    hard_schedule = bool(
        re.search(r"(?i)\b(?:six[- ]day\s+workweek)\b|大小周|单双休|单休|六天工作制", text)
    )
    high_intensity = contains_any(
        text, ("fast-paced", "startup environment", "high intensity", "快节奏", "高强度")
    )

    positives: list[str] = []
    risks: list[str] = []
    if double_weekend:
        positives.append("明确双休")
    if no_travel:
        positives.append("明确无出差")
    if no_on_call:
        positives.append("明确无 on-call")
    if on_call:
        risks.append("on-call/值班")
    if customer_site:
        risks.append("客户现场")
    if cross_timezone:
        risks.append("跨时区协作")
    if travel_percentage is not None:
        label = {
            "less_than": f"差旅上限 <{travel_percentage}%",
            "up_to": f"差旅上限 ≤{travel_percentage}%",
            "at_least": f"差旅至少 {travel_percentage}%",
            "stated": f"差旅约 {travel_percentage}%",
        }.get(travel_comparator, f"差旅约 {travel_percentage}%")
        risks.append(label)
    elif travel_required:
        risks.append("有差旅要求")
    if weekend_or_night:
        risks.append("周末/晚间工作")
    if high_intensity:
        risks.append("快节奏/高强度")

    if hard_schedule:
        exclusion = "大小周/单休与生活边界冲突"
    elif frequent_travel or (
        travel_percentage is not None
        and (
            (travel_comparator == "at_least" and travel_percentage >= 25)
            or (travel_comparator == "stated" and travel_percentage >= 30)
            or (travel_comparator in {"less_than", "up_to"} and travel_percentage > 25)
        )
    ):
        exclusion = "高频差旅与生活边界冲突"
    else:
        exclusion = None
    if positives or risks:
        return "；".join([*positives, *risks]), exclusion
    return "工时/差旅未披露", exclusion


def career_asset_description(text: str) -> str:
    """Describe the concrete, externally legible asset this role can leave."""
    has_agent = contains_any(text, ("agent", "agentic", "智能体"))
    has_eval = contains_any(text, ("eval", "evaluation", "benchmark", "评测", "失败归因"))
    has_loop = contains_any(text, ("launch", "roadmap", "end-to-end", "ownership", "上线", "路线图", "端到端"))
    if has_agent and has_eval and has_loop:
        return "Agent Benchmark、失败归因与上线闭环案例"
    if has_agent and has_transport_domain(text) and contains_any(text, ("multimodal", "vision", "多模态", "视觉")):
        return "安全敏感多模态 Agent 的可靠性产品案例"
    if has_agent and contains_any(text, ("tool use", "tool calling", "mcp", "api", "sdk", "工具调用")):
        return "Agent 工具调用/API 平台与评测资产"
    if has_eval:
        return "AI 评测标准、失败归因与验收方法论"
    if has_agent:
        return "Agent 产品路线、实验与上线闭环案例"
    return "复杂 AI 产品问题定义与落地案例"


def looks_like_product_job(job: Job) -> bool:
    return contains_any(job.title, PRODUCT_ROLE_TERMS)


def has_target_title(title: str) -> bool:
    return contains_any(title, TARGET_TITLE_TERMS) or bool(
        re.search(r"(?i)(?<![a-z])AI(?![a-z])", title)
    )


def has_substantive_target_duties(job: Job) -> bool:
    """Recover differently named PM roles only when the JD assigns target ownership."""
    return bool(
        re.search(
            r"(?is)(?:\bown|\blead|\bdefine|responsible\s+for|负责|主导|定义)"
            r"[^.\n。]{0,100}(?:"
            r"(?:agent|agentic|智能体)[^.\n。]{0,45}(?:eval|evaluation|benchmark|评测|可靠性)|"
            r"(?:eval|evaluation|benchmark|评测|可靠性)[^.\n。]{0,45}(?:agent|agentic|智能体)"
            r")",
            job.summary,
        )
    )


def has_substantive_job_description(job: Job) -> bool:
    """Reject title-only/thin official records before they can become action cards."""
    compact = " ".join(job.summary.split())
    if len(compact) < 20:
        return False
    duty = contains_any(
        compact,
        (
            "responsible for", "you will", "own ", "lead ", "define ", "design ",
            "build ", "drive ", "负责", "主导", "定义", "设计", "推动", "搭建",
            "requirements", "qualifications", "岗位职责", "任职要求",
        ),
    )
    evidence_groups = (
        ("agent", "agentic", "智能体", "eval", "evaluation", "benchmark", "评测", "大模型", "llm", "multimodal", "多模态"),
        ("roadmap", "end-to-end", "launch", "business outcome", "product outcome", "ownership", "路线", "端到端", "上线", "业务结果", "任务闭环", "产品闭环", "验收"),
        ("reliability", "safety", "tool calling", "failure attribution", "cost", "latency", "python", "可靠性", "安全", "工具调用", "失败归因", "成本", "延迟"),
    )
    evidence_count = sum(contains_any(compact, group) for group in evidence_groups)
    return duty and evidence_count >= (1 if len(compact) >= 120 else 2)


def is_early_career_job(job: Job) -> bool:
    """Reject only high-confidence campus, internship, and new-graduate roles."""
    title = job.title.strip()
    title_or_url = f"{title}\n{job.url}"
    folded_url = job.url.casefold()
    if re.search(
        r"(?:/campus(?:/|$)|/intern(?:ship)?(?:/|$)|[?&](?:highlighttype|recruittype)=campus)",
        folded_url,
    ):
        return True
    if re.search(
        r"(?i)(?:实习生|实习岗位|暑期实习|校招|校园招聘|应届(?:生|毕业生)?|"
        r"管培生|管理培训生|20\d{2}届|\bintern(?:ship)?\b|\bnew\s*grad(?:uate)?\b|"
        r"\bearly[\s_-]*career\b|\buniversity\s+graduate\b|"
        r"\bcampus[\s_-]*(?:hire|recruit(?:ment)?)\b|"
        r"\b(?:20\d{2}\s+)?graduate\s+(?:program|programme|scheme|role|position|hire|"
        r"product\s+manager)\b|\bmanagement\s+trainee\b)",
        title_or_url,
    ):
        return True
    text = job.text
    return bool(
        re.search(
            r"(?:面向|招聘|招募|仅限|欢迎)[^。；;\n]{0,24}"
            r"(?:20\d{2}届|应届(?:生|毕业生)?|在校生|实习生)",
            text,
        )
        or re.search(
            r"(?i)\b(?:designed\s+for|seeking|hiring|open\s+to)\b[^.\n]{0,60}"
            r"\b(?:new\s*grads?|recent\s+graduates?|students?|interns?)\b",
            text,
        )
    )


def looks_like_candidate_job(job: Job) -> bool:
    """Broad pre-score gate for senior product and adjacent AI ownership roles."""
    if is_early_career_job(job):
        return False
    if looks_like_product_job(job):
        return True
    title = job.title
    if contains_any(title, ENGINEERING_TITLE_TERMS):
        return False
    return (
        has_target_title(title)
        and contains_any(title, (*SENIOR_ROLE_TERMS, *OWNERSHIP_ROLE_TERMS))
    )


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
        r"(?!\s*(?:users?|customers?|requests?|tokens?|qps|rpm|tpm|dau|mau|records?|samples?|"
        r"用户|客户|请求|令牌|记录|样本|次)\b)"
        r"(?:\s*[·xX×*]\s*(\d{2})\s*薪)?",
        compact,
        flags=re.IGNORECASE,
    )
    if monthly:
        low, high = float(monthly.group(1)), float(monthly.group(2))
        months_text = monthly.group(3)
        fixed_low, fixed_high = round(low * 12 / 10, 1), round(high * 12 / 10, 1)
        if not months_text:
            return Salary(
                None,
                None,
                fixed_low,
                fixed_high,
                f"{low:g}–{high:g}K/月（12月固定估算；奖金/股票未披露）",
            )
        months = int(months_text)
        annual_low, annual_high = round(low * months / 10, 1), round(high * months / 10, 1)
        explicit_total = bool(
            re.search(r"(?i)总包|年包|total\s+compensation|\bTC\b|all[- ]in", compact)
        )
        return Salary(
            annual_low if explicit_total else None,
            annual_high if explicit_total else None,
            fixed_low, fixed_high,
            (
                f"{low:g}–{high:g}K×{months}（明确总包口径）"
                if explicit_total
                else f"{low:g}–{high:g}K×{months}（名义年现金 {annual_low:g}–{annual_high:g}万；股权未披露）"
            ),
        )

    annual_wan = re.search(
        r"(?<!\d)(\d{2,4}(?:\.\d+)?)\s*[-–—~至]\s*(\d{2,4}(?:\.\d+)?)\s*万(?:元)?\s*/?\s*年",
        compact,
    )
    if annual_wan:
        low, high = float(annual_wan.group(1)), float(annual_wan.group(2))
        context = compact[
            max(0, annual_wan.start() - 56):min(len(compact), annual_wan.end() + 56)
        ]
        explicit_total = bool(
            re.search(r"(?i)总包|年包|total\s+compensation|\bTC\b|all[- ]in", context)
        )
        explicit_fixed = bool(
            re.search(r"(?i)基本工资|基本薪资|固定工资|固定薪资|年固定|base(?:\s+salary|\s+pay)?|\bfixed\b", context)
        )
        if explicit_total:
            return Salary(
                total_low_wan=low,
                total_high_wan=high,
                label=f"{low:g}–{high:g}万/年（明确总包口径）",
            )
        if explicit_fixed:
            return Salary(
                fixed_low_wan=low,
                fixed_high_wan=high,
                label=f"{low:g}–{high:g}万/年固定现金",
            )
        return Salary(
            label=f"{low:g}–{high:g}万/年（年现金口径待确认）",
            annual_cash_low_wan=low,
            annual_cash_high_wan=high,
        )

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
        fixed_low = round(low * usd_cny / 10, 1)
        fixed_high = round(high * usd_cny / 10, 1)
        return Salary(
            None, None, fixed_low, fixed_high,
            f"US${low:g}K–{high:g}K base（汇率估算）",
        )
    return Salary()


def salary_gate(salary: Salary, config: dict[str, Any]) -> tuple[str, bool]:
    floor = float(config["current_fixed_cash_wan"])
    target = float(config["target_total_comp_wan"])
    if salary.fixed_high_wan is not None and salary.fixed_high_wan < floor:
        return f"12个月固定上沿 {salary.fixed_high_wan:g} 万，低于当前 {floor:g} 万底线", True
    if salary.annual_cash_high_wan is not None:
        if salary.annual_cash_high_wan < floor:
            return (
                f"公开年现金上沿 {salary.annual_cash_high_wan:g} 万，低于当前固定现金 {floor:g} 万底线",
                True,
            )
        return (
            f"公开年现金上沿 {salary.annual_cash_high_wan:g} 万；固定现金、奖金和股权拆分待核验",
            False,
        )
    if salary.total_high_wan is None:
        if salary.fixed_high_wan is not None:
            return (
                f"12个月固定区间上沿 {salary.fixed_high_wan:g} 万；"
                "奖金/股票未披露，必须前置核验风险调整后总包",
                False,
            )
        return "薪酬未披露，必须前置核验", False
    if salary.total_high_wan < target:
        return f"名义上沿 {salary.total_high_wan:g} 万，低于 {target:g} 万红线", True
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
    target_title = has_target_title(job.title)
    target_duties = has_substantive_target_duties(job)
    fit = 10 + count_matches(
        text,
        [
            (("agent", "agentic", "智能体"), 12),
            (("eval", "evaluation", "评测", "benchmark", "quality", "可靠性", "模型质量", "模型效果"), 16),
            (("multimodal", "多模态", "vision", "视觉"), 7),
            (("tool use", "tool calling", "mcp", "context", "memory", "工具调用", "上下文", "记忆"), 8),
            (("safety", "security", "high-consequence", "安全", "风控"), 7),
            (("prototype", "builder", "api", "python", "typescript", "原型"), 6),
            (("cost", "latency", "roi", "business outcome", "成本", "延迟", "业务结果"), 6),
            (("roadmap", "end-to-end", "ownership", "launch", "路线图", "端到端", "上线"), 8),
        ],
    )
    fit += 18 if looks_like_product_job(job) else 0
    fit += 22 if target_title else 12 if target_duties else -25
    if (target_title or target_duties) and contains_any(job.title, SENIOR_ROLE_TERMS):
        fit += 18
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
    if contains_any(job.title, SENIOR_ROLE_TERMS):
        ready += 6

    strengths: list[str] = []
    gap_candidates: list[tuple[int, str]] = []
    if contains_any(text, ("eval", "评测", "benchmark", "quality")):
        strengths.append("Benchmark/评测主轴匹配")
    if job_has_transport_domain(job):
        strengths.append("驾驶/交通真实场景可直接迁移")
    if contains_any(text, ("multimodal", "多模态", "vision", "视觉")):
        strengths.append("多模态经验匹配")
    if contains_any(text, ("tool use", "mcp", "context", "memory", "工具调用", "上下文", "记忆")):
        gap_candidates.append((3, "需证明 Agent 系统栈与工具调用深度"))
    if contains_any(text, ("software development", "engineering background", "coding", "python", "typescript", "代码", "工程背景")):
        ready -= 12
        gap_candidates.append((1, "亲手编码/原型证据不足"))
    if contains_any(text, ("developer platform", "sdk", "api product", "开发者平台", "开放平台")):
        ready -= 7
        gap_candidates.append((2, "开发者平台/API 产品经验需补齐"))
    if contains_any(job.title, ("数据策略", "数据产品")):
        fit -= 8
        ready -= 1
        gap_candidates.append((4, "偏数据策略，需核验端到端产品与评测闭环"))
    if contains_any(text, ("trust & safety", "integrity", "safeguard", "regulated", "安全测量", "合规")):
        ready -= 7
        gap_candidates.append((2, "正式 AI Safety/风险度量经验不足"))
    experience_years = required_experience_years(text)
    if experience_years is not None and experience_years >= 5:
        if experience_years >= 8:
            ready -= min(14, 4 + (experience_years - 8) * 2)
        gap_candidates.append(
            (0, f"JD 硬门槛：{experience_years} 年以上相关经验，需用完整履历证明")
        )
    people_management = has_people_management_requirement(text)
    if people_management:
        fit -= 18
        ready -= 8
        gap_candidates.append((0, "岗位核心含直属团队、招聘绩效或组织建设"))
    if contains_any(
        f"{text}\n{job.source}\n{job.company}",
        config.get("preferred_companies", ()),
    ):
        fit += 4

    work_risk, work_exclusion = work_boundary_assessment(text)
    exclusion = None
    if is_early_career_job(job):
        exclusion = "校招/应届/实习岗位，不属于目标社招范围"
    elif contains_any(job.title, DISALLOWED_TITLE_TERMS):
        exclusion = "GTM/增长/营销产品身份偏离目标"
    elif contains_any(text, ("forward deployed engineer", "fde ", "fdse", "solutions engineer", "售前", "驻场", "客户交付", "customer implementation")):
        exclusion = "FDE/售前/驻场交付身份冲突"
    elif contains_any(job.title, ENGINEERING_TITLE_TERMS) and not looks_like_product_job(job):
        exclusion = "工程岗位，不是产品 IC"
    elif contains_any(text, ("software engineer", "machine learning engineer", "applied ai engineer", "算法工程师")) and not looks_like_product_job(job):
        exclusion = "工程岗位，不是产品 IC"
    elif people_management:
        exclusion = "重人员管理/招聘绩效/组织建设与高级产品 IC 定位冲突"
    elif work_exclusion:
        exclusion = work_exclusion

    salary = parse_salary(text, float(config.get("usd_cny", 7.0)))
    gate_label, salary_reject = salary_gate(salary, config)
    if salary_reject:
        exclusion = exclusion or "公开薪酬低于风险调整后总包红线"

    fit = max(0, min(100, fit))
    ready = max(0, min(100, ready))
    asset = career_asset_description(text)
    strengths = strengths or ["复杂问题定义与跨团队落地可迁移"]
    gap_candidates.extend(
        (
            (8, "Agent 原型/API 实验闭环证据需补齐"),
            (9, "trace、失败归因与成本/延迟权衡需补齐"),
        )
    )
    gaps = tuple(
        dict.fromkeys(value for _, value in sorted(gap_candidates, key=lambda item: item[0]))
    )[:2]
    return Assessment(
        job, fit, ready, asset, salary, gate_label, responsibility_tags(text),
        tuple(strengths[:3]), gaps, work_risk, exclusion, experience_years,
    )


def rank_assessments(assessments: Iterable[Assessment], config: dict[str, Any]) -> list[Assessment]:
    fit_threshold = int(config["fit_threshold"])
    ready_threshold = int(config["ready_threshold"])
    global_threshold = int(config.get("global_fit_threshold", 88))
    require_official = bool(config.get("require_official_source", True))
    eligible: list[Assessment] = []
    for item in assessments:
        if (
            not item.eligible
            or not has_substantive_job_description(item.job)
            or item.fit < fit_threshold
            or item.ready < ready_threshold
        ):
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
