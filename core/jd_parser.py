import re
from typing import Dict, Any

# 常见招聘网页垃圾噪声行（复制整页时常混入）
_NOISE_PATTERNS = [
    r"^\s*\[?(?:微信扫码分享|举报|感兴趣|继续沟通|立即沟通|完善在线简历|新增附件简历|查看全部职位|下载App)\]?\s*$",
    r"^\s*上BOSS直聘，直接谈\s*$",
    r"^\s*以下是图片中提取的招聘JD文字内容[:：]?\s*$",
    r"^\s*[-_=]{3,}\s*$",
]

_CITIES = [
    "深圳", "香港", "Hong Kong", "广州", "上海", "北京", "杭州", "成都",
    "珠海", "东莞", "佛山", "澳门", "新加坡", "Singapore", "远程", "Remote"
]

def parse_raw_job_posting(raw_text: str) -> Dict[str, Any]:
    """
    毫秒级本地招聘原文智能解析器：
    支持直接粘贴 BOSS直聘 / 猎聘 / JobsDB / LinkedIn 整页复制文本或截图 OCR 文本，
    自动拆解出职位名称、公司名称、薪资、城市、HR 活跃度并净化正文。
    """
    if not raw_text or not raw_text.strip():
        return {
            "job_title": "",
            "company_name": "",
            "salary": "",
            "city": "深圳",
            "hr_active_status": "今日活跃",
            "platform": "manual",
            "jd_text": ""
        }

    text = raw_text.replace("\r\n", "\n").strip()

    # 1. 识别平台来源
    lower = text.lower()
    if "boss直聘" in lower or "zhipin.com" in lower or "立即沟通" in text:
        platform = "boss"
    elif "猎聘" in text or "liepin.com" in lower:
        platform = "liepin"
    elif "jobsdb" in lower or "hk$" in lower:
        platform = "jobsdb"
    elif "linkedin" in lower:
        platform = "linkedin"
    elif "51job" in lower or "前程无忧" in text:
        platform = "51job"
    elif "智联" in text or "zhaopin.com" in lower:
        platform = "zhilian"
    else:
        platform = "manual"

    # 2. 提取薪资区间（支持 12-18K·14薪 / 10-15k / HK$20,000 - $30,000 / 200-300元/天 / 8000-12000元/月）
    salary = ""
    sal_patterns = [
        r'\b\d{1,3}\s*[-~～至]\s*\d{1,3}\s*[kK千](?:\s*[·*×x]\s*\d{2}\s*薪)?',
        r'(?:HK\$|HKD|\$|¥|￥)\s*\d[\d,]*\s*[-~～至to]+\s*(?:HK\$|HKD|\$|¥|￥)?\s*\d[\d,]*(?:\s*/\s*(?:month|mo|月|yr|year))?',
        r'\d{3,5}\s*[-~～至]\s*\d{3,5}\s*元\s*/\s*(?:天|月|日|周)',
        r'\d{1,2}(?:\.\d)?\s*[-~～至]\s*\d{1,2}(?:\.\d)?\s*万(?:/月|/年)?',
    ]
    for pat in sal_patterns:
        m = re.search(pat, text, re.I)
        if m:
            salary = re.sub(r'\s+', '', m.group(0))
            break

    # 3. 提取城市
    city = "深圳"
    for c in _CITIES:
        if re.search(re.escape(c), text, re.I):
            city = "香港" if c.lower() == "hong kong" else ("远程" if c.lower() == "remote" else c)
            break

    # 4. 提取 HR 在线/活跃标签
    hr_active_status = "今日活跃"
    hr_m = re.search(r'(刚刚活跃|当前在线|今日活跃|昨日活跃|本周活跃|\d+日内活跃|\d+周内活跃|\d+月内活跃|半年前活跃|近半年活跃|\d+月前活跃)', text)
    if hr_m:
        hr_active_status = hr_m.group(1)

    # 5. 提取 URL（若文中带有招聘链接）
    url_m = re.search(r'https?://(?:www\.)?(?:zhipin\.com|liepin\.com|hk\.jobsdb\.com|jobsdb\.com|linkedin\.com|jobs\.51job\.com|zhaopin\.com)[^\s\)>\]"]*', text, re.I)
    job_url = url_m.group(0) if url_m else ""

    # 6. 按行清洗并提取职位名与公司名
    raw_lines = [ln.strip() for ln in text.split("\n")]
    clean_lines = []
    for ln in raw_lines:
        if not ln:
            if clean_lines and clean_lines[-1] != "":
                clean_lines.append("")
            continue
        if any(re.match(p, ln, re.I) for p in _NOISE_PATTERNS):
            continue
        clean_lines.append(ln)

    non_empty = [re.sub(r'[\*#`\[\]]+', '', ln).strip() for ln in clean_lines if ln.strip()]

    # 提取公司名称
    company_name = ""
    # 优先匹配显式标签或典型公司后缀
    comp_explicit = re.search(r'(?:公司名称|招聘企业|企业名称|Company)[\s:：]+([^\n|｜]{2,40})', text, re.I)
    if comp_explicit:
        company_name = comp_explicit.group(1).strip()
    else:
        for ln in non_empty:
            # 清理掉 [公司Logo] 等前缀
            candidate = re.sub(r'^公司Logo\s*', '', ln).strip()
            comp_m = re.search(
                r'([\u4e00-\u9fa5A-Za-z0-9（）\(\)·\-]{2,32}(?:科技有限公司|网络科技有限公司|信息技术有限公司|文化传媒有限公司|设计有限公司|实业有限公司|集团有限公司|工作室|科技|智能|传媒|网络|集团|Limited|Ltd\.?|Inc\.?|Corp\.?|Studio|Labs?))',
                candidate
            )
            if comp_m and len(candidate) <= 42 and not any(w in candidate for w in ["岗位职责", "任职要求", "工作经验", "学历要求", "负责"]):
                company_name = comp_m.group(1).strip()
                break

    # 提取职位名称
    job_title = ""
    title_explicit = re.search(r'(?:职位名称|招聘岗位|岗位名称|应聘岗位|Position|Role)[\s:：]+([^\n|｜]{2,40})', text, re.I)
    if title_explicit:
        job_title = title_explicit.group(1).strip()
    else:
        skip_prefixes = ("职位描述", "岗位职责", "工作内容", "任职要求", "任职资格", "公司基本信息", "福利待遇", "我们提供", "关于我们", "工作地址")
        for ln in non_empty[:8]:
            # 若第一行含职位 + 薪资（如：会展活动执行  9-12K）
            stripped = ln
            if salary:
                stripped = stripped.replace(salary, "").strip()
            stripped = re.sub(r'\b\d{1,3}\s*[-~～]\s*\d{1,3}\s*[kK千].*$', '', stripped).strip()
            stripped = re.sub(r'[📍💼🎓🏢👤].*$', '', stripped).strip()
            if (
                2 <= len(stripped) <= 36
                and stripped != company_name
                and not stripped.startswith(skip_prefixes)
                and not stripped.startswith(("1.", "2.", "1、", "2、", "•", "-"))
            ):
                job_title = stripped
                break

    cleaned_jd = "\n".join(clean_lines).strip()

    return {
        "job_title": job_title,
        "company_name": company_name,
        "salary": salary,
        "city": city,
        "hr_active_status": hr_active_status,
        "job_url": job_url,
        "platform": platform,
        "jd_text": cleaned_jd
    }
