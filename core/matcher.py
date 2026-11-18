import os
import json
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any
from .pii_masker import PIIMasker
from .veto_engine import VetoEngine
from .llm_client import call_llm, is_llm_available
from .profile_loader import resolve_resume_path

RESUMES_DIR = Path(__file__).resolve().parent.parent / "resumes"

_COMMON_ATS_TERMS = [
    "AI", "Agent", "LLM", "大模型", "RAG", "Prompt", "提示词", "Function Calling", "MCP",
    "产品经理", "Product Manager", "PRD", "原型", "Figma", "MVP", "0到1", "用户调研",
    "Cursor", "Claude", "Codex", "n8n", "Dify", "Coze", "工作流", "Workflow",
    "Python", "FastAPI", "Next.js", "React", "TypeScript", "Swift", "SwiftUI", "Flutter",
    "SQLite", "PostgreSQL", "SQL", "Docker", "Git", "GitHub", "Vercel", "OCR", "API",
    "飞书", "多维表格", "Notion", "SOP", "项目统筹", "跨部门", "项目管理", "数据分析",
    "行政", "会务", "接待", "公文", "OA", "台账", "采购", "供应商", "差旅", "报销",
    "编导", "策划", "文案", "剪辑", "拍摄", "Premiere", "Photoshop", "新媒体", "活动执行",
    "粤语", "Cantonese", "英语", "English", "Bilingual"
]

_HYPE_DOWNGRADES = [
    (r"高规格(?:会务)?接待", "会务协调与服务配合"),
    (r"赋能", "辅助"),
    (r"降本增效", "减少重复劳动、提升效率"),
    (r"顶层设计", "整体流程梳理"),
    (r"行业标杆", "扎实可用"),
    (r"矩阵式打法", "多渠道配合"),
]

def apply_truth_guard(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    for pat, repl in _HYPE_DOWNGRADES:
        cleaned = re.sub(pat, repl, cleaned)
    return cleaned

def strip_json_fence(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()

def ensure_front_loaded_greeting(greeting: str, track_key: str, language: str = "zh-CN") -> str:
    """
    兜底守护：如果模型不小心把“您好/你好/Hello”写在了最前面浪费前 16 字黄金曝光位，
    自动补齐赛道核心前置钩子，确保招聘端消息列表第一眼看到硬实力。
    """
    g = (greeting or "").strip()
    if not g:
        return g
    if g.startswith(("【", "[")):
        return g
    if re.match(r"^(?:您好|你好|哈喽|嗨|Hello|Hi|Dear)\b[,，！!\s]*", g, re.I):
        hooks = {
            "track_ai_builder": {"zh-CN": "【独立上架iOS·AI全栈】", "zh-HK": "【獨立上架App·AI實踐】", "en": "[Shipped iOS & AI Builder] "},
            "track_ai_ops": {"zh-CN": "【5年统筹·AI数字化流】", "zh-HK": "【5年統籌·AI工作流】", "en": "[5Y Ops & AI Workflow] "},
            "track_admin": {"zh-CN": "【4年政企行政·39家闭环】", "zh-HK": "【4年政企行政·專職聯絡】", "en": "[4Y Exec & Admin Ops] "},
            "track_media_pr": {"zh-CN": "【广电编导·全案宣推物料】", "zh-HK": "【廣電編導·全案宣推】", "en": "[Media BA & Event PR] "},
        }
        prefix = hooks.get(track_key, hooks["track_ai_builder"]).get(language, "【核心匹配】")
        return f"{prefix}{g}"
    return g


class JobMatcher:
    """
    HireLens (聘镜) 核心路由与招聘端视角生成引擎：
    站在招聘官真实第一眼屏幕（消息列表前 16 字截断区 + BOSS 推荐牛人卡片前 52 字「个人优势」区）
    倒推生成不吹牛、不被折叠埋没的求职文案。
    """

    TRACK_META = {
        "track_ai_builder": {
            "name": "AI 产品经理 / AI Agent 应用开发",
            "file": "master_ai_builder.md",
            "keywords": [
                "ai产品", "产品经理", "product manager", "pm", "agent", "智能体", "大模型",
                "llm", "rag", "prompt", "cursor", "claude", "vibe coding", "全栈", "mvp",
                "原型", "app store", "swiftui", "flutter", "next.js", "react", "dify", "coze", "技术员", "应用开发"
            ]
        },
        "track_ai_ops": {
            "name": "项目运营助理 / 数字化工作流 / AI办公实践",
            "file": "master_ai_ops.md",
            "keywords": [
                "项目运营", "运营助理", "工作流", "workflow", "数字化", "飞书", "多维表格",
                "n8n", "自动化", "知识库", "效率", "sop", "看板", "业务运营", "项目助理"
            ]
        },
        "track_admin": {
            "name": "行政综合 / 行政BP / 专职联络",
            "file": "master_admin.md",
            "keywords": [
                "行政", "考勤", "资产", "差旅", "接待", "总务", "后勤", "高管助理",
                "总经理助理", "总裁助理", "办公环境", "会务", "公文", "秘书", "oa", "印章"
            ]
        },
        "track_media_pr": {
            "name": "品牌物料 / 视频编导 / 企文宣传",
            "file": "master_media_pr.md",
            "keywords": [
                "策划", "物料", "剪辑", "编导", "视频", "摄影", "宣传", "新媒体",
                "展会", "会展", "文案", "采访", "公众号", "活动执行", "pr", "品牌"
            ]
        }
    }

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str = "deepseek-chat", provider: str = "", llm_enabled: bool = True):
        self.api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("OPENAI_MODEL", "deepseek-chat")
        self.provider = provider
        self.llm_enabled = llm_enabled
        self.masker = PIIMasker()
        self.veto_engine = VetoEngine()
        self.resumes = self._load_resumes()

    def _load_resumes(self) -> Dict[str, str]:
        resumes = {}
        for track_key, meta in self.TRACK_META.items():
            path = resolve_resume_path(meta["file"])
            if path.exists():
                resumes[track_key] = path.read_text(encoding="utf-8")
            else:
                resumes[track_key] = f"【{meta['name']} 简历母本暂缺】"
        return resumes

    def _extract_local_ats(self, jd_text: str, track_key: str) -> Dict[str, Any]:
        resume_blob = self.resumes.get(track_key, "").lower()
        jd_lower = jd_text.lower()
        matched, missing = [], []
        for term in _COMMON_ATS_TERMS:
            if term.lower() in jd_lower:
                if term.lower() in resume_blob:
                    matched.append(term)
                else:
                    missing.append(term)
        return {
            "matched_keywords": matched[:10],
            "missing_keywords": missing[:6],
            "bridge_strategy": (
                f"已命中 {len(matched)} 项岗位关键词；对缺失项（{'、'.join(missing[:3]) or '无明显硬缺口'}），"
                "建议用已上线产品闭环与快速上手新工具的实证来诚实补位，不强行编造经验。"
            )
        }

    def match_and_generate(
        self,
        job_title: str,
        jd_text: str,
        company_name: str = "",
        hr_active_status: str = "今日活跃",
        language: str = "zh-CN",
        preferred_track: str = "",
    ) -> Dict[str, Any]:
        veto_report = self.veto_engine.evaluate(job_title, company_name, jd_text, hr_active_status)

        all_resumes_raw = "\n\n".join(
            f"=== 赛道代码 `{tk}`（{self.TRACK_META[tk]['name']}）===\n{txt[:900]}"
            for tk, txt in self.resumes.items()
        )
        masked_resumes_blob, resume_map, resume_audit = self.masker.mask_with_audit(all_resumes_raw)
        masked_jd, jd_map, jd_audit = self.masker.mask_with_audit(f"{job_title}\n{company_name}\n{jd_text}")

        merged_map = {**resume_map, **jd_map}
        pii_audit = {
            "masked_count": resume_audit["masked_count"] + jd_audit["masked_count"],
            "categories": list(dict.fromkeys(resume_audit["categories"] + jd_audit["categories"])),
        }

        if not is_llm_available(self.api_key, self.base_url, self.llm_enabled):
            res = self._heuristic_fallback(job_title, jd_text, company_name, veto_report, language, preferred_track)
            res["pii_audit"] = pii_audit
            res["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
            return res

        lang_instruction = {
            "zh-CN": (
                "所有输出字段均使用【简体中文】。"
                "1) greeting（75~95字）：招聘端消息列表在未点开前仅显示前 14~18 个字！严禁以“您好/看到贵司招聘”开头浪费前16字，必须第1个字就用【10~14字最硬核匹配钩子】开场！"
                "2) profile_advantage（95~135字，严格≤140字）：用于填写 BOSS直聘在线简历的「个人优势」（展示在招聘官的推荐牛人信息流卡片上，默认只露出前2行约52字，且命中搜索词会高亮）。前50字必须直接亮出硬核作品/量化战绩，后半段自然嵌入该岗位高频检索技术/业务词，严禁写“性格开朗、吃苦耐劳”等无效空话。"
            ),
            "zh-HK": (
                "所有輸出字段均使用地道香港職場【繁體中文】。"
                "1) greeting（75~95字）：第1個字必須以【核心硬實力標籤】重點前置，確保招聘端預覽前16字直擊要點；"
                "2) profile_advantage（95~135字）：用於求職平台個人摘要/推薦卡，前50字亮出真實作品與經驗，自然融入崗位關鍵詞與粵普雙語優勢。"
            ),
            "en": (
                "Write all output fields in clear, natural English. "
                "1) greeting (45-65 words): MUST start with a bracketed hard-proof hook in the first 35 characters (e.g., '[Shipped App Store iOS + AI Builder]') so inbox previews don't cut off at 'Hi, I saw your job...'. "
                "2) profile_advantage (55-80 words): Recruiter talent-feed summary card—front-load shipped apps/coord metrics in the first line and weave in exact JD search keywords naturally."
            )
        }.get(language, "所有输出使用简体中文，greeting 前16字重点前置，profile_advantage ≤135字。")

        forced_track_hint = (
            f"用户已手动指定赛道为 `{preferred_track}`，请固定将 selected_track 设为 `{preferred_track}`。"
            if preferred_track in self.TRACK_META else
            "请从 4 个赛道中选出最契合该 JD 的 1 个赛道代码。"
        )

        prompt = f"""
你是候选人 `[CANDIDATE_NAME]` 的招聘端视角求职顾问（HireLens）。请站在招聘官每天刷几百条消息和推荐卡片的真实屏幕视角，基于候选人的真实母本生成诊断与文案。

【候选人表达与事实边界铁律】
1. 严格忠于下方提供的真实母本事实，绝不捏造母本中不存在的项目、指标或头衔；未正式上线的模块必须如实标注为原型验证阶段。
2. 严禁互联网黑话：禁止“赋能、降本增效、顶层设计、矩阵式打法、抓手”等套话；禁止在个人优势里写“性格开朗、吃苦耐劳、有团队精神”等凑字数废话。

【候选人四大真实赛道母本（已物理脱敏）】
{masked_resumes_blob}

【目标岗位（已脱敏）】
职位名称: {job_title}
公司名称: {company_name or '招聘企业'}
HR活跃状态: {hr_active_status}
前置排雷提示: {veto_report['advice']}
岗位描述:
{masked_jd}

【语言与招聘端曝光规则】
- {forced_track_hint}
- {lang_instruction}

请严格返回以下 JSON 对象：
{{
  "selected_track": "track_ai_builder | track_ai_ops | track_admin | track_media_pr 四选一",
  "match_score": 0到96之间的客观整数总分,
  "dimension_scores": {{
    "skill_fit": 0到100整数,
    "exp_fit": 0到100整数,
    "growth_value": 0到100整数
  }},
  "ats_matrix": {{
    "matched_keywords": ["候选人真实具备且命中JD的搜索关键词，最多8个"],
    "missing_keywords": ["JD要求但候选人目前欠缺的项，如实列出，最多5个"],
    "bridge_strategy": "一句话大白话策略：如何用现有真实作品或跨界经验诚实弥补缺失项"
  }},
  "fit_reasons": [
    "契合依据1：引用具体真实项目或经历细节",
    "契合依据2：对应解决该岗位的哪项具体日常工作",
    "契合依据3：跨界优势或工作习惯"
  ],
  "greeting": "打招呼短破冰语（第1个字起必须是【重点前置钩子】，确保前16字截断区直露硬实力）",
  "profile_advantage": "针对该岗定制的 BOSS直聘「个人优势」文案（95~135字，前50字直出核心成果供牛人推荐卡片外露，后段自然埋入JD关键词供HR搜索标红）",
  "cover_letter": "用于邮件/JobsDB/LinkedIn 投递的 160~220 字精炼自荐信"
}}
"""
        try:
            llm_out = call_llm(
                messages=[
                    {"role": "system", "content": "你是精通招聘端推荐机制与真实职场表达的求职顾问。严守事实边界，只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                expect_json=True,
                temperature=0.25,
                timeout=42,
                provider=self.provider,
            )
            raw_res = llm_out["json"] or {}
            usage = llm_out["usage"]

            unmasked = self.masker.unmask_obj(raw_res, merged_map)
            track_key = unmasked.get("selected_track")
            if track_key not in self.TRACK_META:
                track_key = preferred_track if preferred_track in self.TRACK_META else "track_ai_builder"

            greeting_clean = ensure_front_loaded_greeting(
                apply_truth_guard(unmasked.get("greeting", "")), track_key, language
            )
            advantage_clean = apply_truth_guard(unmasked.get("profile_advantage", ""))[:140]
            cover_clean = apply_truth_guard(unmasked.get("cover_letter", ""))
            reasons_clean = [apply_truth_guard(r) for r in (unmasked.get("fit_reasons") or [])]

            dim = unmasked.get("dimension_scores") or {}
            dimension_scores = {
                "skill_fit": int(dim.get("skill_fit", 82)),
                "exp_fit": int(dim.get("exp_fit", 80)),
                "growth_value": int(dim.get("growth_value", 85)),
                "safety_score": veto_report["safety_score"],
            }

            ats = unmasked.get("ats_matrix") or self._extract_local_ats(jd_text, track_key)
            ats["bridge_strategy"] = apply_truth_guard(ats.get("bridge_strategy", ""))

            final_score = int(unmasked.get("match_score", 84))
            if veto_report["risk_level"] == "veto":
                final_score = min(final_score, 55)

            return {
                "selected_track": track_key,
                "track_name": self.TRACK_META[track_key]["name"],
                "match_score": final_score,
                "risk_level": veto_report["risk_level"],
                "veto_report": veto_report,
                "dimension_scores": dimension_scores,
                "ats_matrix": ats,
                "fit_reasons": reasons_clean,
                "greeting": greeting_clean,
                "profile_advantage": advantage_clean,
                "cover_letter": cover_clean,
                "pii_audit": pii_audit,
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                }
            }

        except Exception as e:
            res = self._heuristic_fallback(job_title, jd_text, company_name, veto_report, language, preferred_track)
            res["pii_audit"] = pii_audit
            res["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
            res["error_note"] = f"已使用本地离线引擎（云端接口提示: {str(e)[:80]}）"
            return res

    def _heuristic_fallback(
        self,
        job_title: str,
        jd_text: str,
        company_name: str,
        veto_report: Dict[str, Any],
        language: str = "zh-CN",
        preferred_track: str = "",
    ) -> Dict[str, Any]:
        content = f"{job_title} {jd_text}".lower()

        scores = {}
        for track_key, meta in self.TRACK_META.items():
            scores[track_key] = sum(1 for kw in meta["keywords"] if kw.lower() in content)

        if preferred_track in self.TRACK_META:
            best_track = preferred_track
        else:
            best_track = max(scores, key=scores.get)
            if scores[best_track] == 0:
                best_track = "track_ai_builder"

        hit_cnt = scores.get(best_track, 1)
        base_score = min(92, 74 + hit_cnt * 3)
        if veto_report["risk_level"] == "veto":
            base_score = min(base_score, 52)

        ats = self._extract_local_ats(jd_text, best_track)

        templates = {
            "track_ai_builder": {
                "zh-CN": (
                    f"【独立上架iOS·AI全栈】您好！针对贵司{job_title}，我已独立用Cursor/Claude上架App Store应用并搭过防幻觉AI解读网关与本地Agent，做事踏实闭环，盼发简历交流！",
                    "独立上架App Store工具应用，具0→1原型、Prompt防幻觉与AI Agent工作流实战。能用Cursor/Claude开发SwiftUI/Flutter/Next.js/Python；4年300+企业项目统筹与SOP拆解经验，粤普流利。",
                    f"您好！关注到贵司正在招聘 {job_title} 岗位。\n\n过去一年我独立借助 Cursor / Claude / Codex 完成了多款产品从 0 到 1 开发，包含已通过苹果审核上架 App Store 的 iOS 应用（含 OCR 与“确定性计算+AI解读”防幻觉接口）、跨城通勤辅助原型及全栈交互原型。\n\n我习惯发现实际痛点后直接动手跑通原型，并具备 4 年多方项目协调底子，希望能将简历与在线作品发您审阅！"
                ),
                "zh-HK": (
                    f"【獨立上架App·AI實踐】您好！針對貴司{job_title}，本人已用AI工具獨立開發並上架App Store應用及雲端後端，粵普流利、處事踏實有交帶，盼發履歷與作品集交流！",
                    "獨立開發並上架App Store工具應用，具0→1原型設計、Prompt防幻覺與AI工作流實戰。熟悉Cursor/Claude開發SwiftUI/Flutter/Next.js/Python；4年跨部門項目統籌閉環經驗，粵普流利。",
                    f"您好！留意到貴司正招聘 {job_title} 一職，特此致函應徵。\n\n過去一年我運用 Cursor / Claude 等 AI 工具獨立完成了多款產品的 0→1 開發，包括已正式上架 App Store 的個人數據工具及多款全棧交互原型。加上過往 4 年跨部門項目統籌經驗，能快速理解業務需求並轉化為可用原型。\n\n隨函附上履歷與在線作品連結，期待有機會與您面談交流！"
                ),
                "en": (
                    f"[Shipped App Store iOS + AI Builder] Hi! Applying for {job_title}. Independently built & shipped an iOS app on the App Store plus Next.js/Python AI workflows with anti-hallucination guards. Grounded, bilingual—happy to share my CV & live demos!",
                    "0-to-1 AI Product Builder with a live iOS app on the App Store . Hands-on in Prompt engineering, anti-hallucination guardrails, AI Agent workflows, and rapid prototyping with Cursor/Claude (SwiftUI, Flutter, Next.js, Python). Backed by 4+ yrs of multi-stakeholder project ops; fluent in Mandarin & Cantonese.",
                    f"Hi Hiring Team,\n\nI'm applying for the {job_title} role at {company_name or 'your team'}. Over the past year, I've independently built and shipped full-stack products from 0 to 1 using AI coding workflows, including a live iOS utility on the App Store and interactive web prototypes. I'd love to share my portfolio and discuss how I can contribute."
                ),
                "reasons": [
                    "具备已上架 App Store 的 0→1 独立产品闭环经验（SwiftUI + Next.js + 订阅与推送）",
                    "实操过防幻觉计算隔离、接口双维限流与本地双向 PII 隐私脱敏等 AI 工程边界设计",
                    "4年政企多方统筹底子 + 熟悉深港双语环境，擅长把模糊业务拆成清晰 SOP 与可用原型"
                ]
            },
            "track_ai_ops": {
                "zh-CN": (
                    f"【5年项目统筹·AI工作流】您好！针对贵司{job_title}，曾统筹300+企业台账闭环，持续用大模型、飞书多维表格与自动化流减少重复工作，盼发简历交流！",
                    "5年多方项目统筹与跨部门协调经验，曾维护300+企业台账与39家单位督办闭环；熟练运用大模型、飞书多维表格、n8n与Python搭建数字化工作流与SOP看板，具独立输出交互原型能力。",
                    f"您好！看到贵司正在招聘 {job_title}。\n\n我拥有 5 年项目综合统筹与跨部门协调经验，曾维护 300+ 企业台账推进闭环；近年持续将大模型、飞书多维表格与自动化脚本融入日常工作流，独立做过本地提效工作台与线上产品原型。希望能将简历发您进一步了解！"
                ),
                "zh-HK": (
                    f"【5年項目統籌·AI工作流】您好！針對貴司{job_title}，擅長多線程SOP梳理與大模型/多維表格自動化提效，粵普流利、處事穩妥，盼發履歷溝通！",
                    "5年多方項目統籌與跨部門協調經驗，曾跟進300+企業台賬閉環；熟練運用大模型、飛書多維表格與n8n自動化梳理業務SOP，具獨立搭建交互原型能力，粵普流利。",
                    f"您好！留意到貴司招聘 {job_title}。我具備 5 年項目統籌及跨部門協調經驗，熟悉 SOP 流程梳理與 AI 工具落地實踐，期待有機會與您進一步交流！"
                ),
                "en": (
                    f"[5Y Project Ops + AI Workflow] Hi! Applying for {job_title}. Coordinated 300+ enterprise accounts and built practical LLM/Feishu/n8n workflows to streamline SOPs. Detail-oriented—happy to share my CV!",
                    "5+ years in multi-stakeholder project operations (300+ enterprise accounts, 39 partner units). Skilled in SOP standardization, Feishu Bitable dashboards, n8n/LLM automation workflows, and rapid web prototyping. Fluent in Mandarin & Cantonese.",
                    f"Hi Team,\n\nI'm writing to apply for the {job_title} position. With 5 years of experience in project coordination and hands-on practice building AI-assisted workflows, I turn multi-threaded tasks into trackable processes."
                ),
                "reasons": [
                    "5年多方项目统筹与 300+ 企业台账跟进经验，擅长多线程并行下的优先级管理",
                    "熟练运用 AI 大模型、飞书多维看板与自动化流程替代重复文书工作，明确人机审核边界",
                    "具备独立搭建 Web/移动端工具原型的动手能力，跨团队沟通顺畅"
                ]
            },
            "track_admin": {
                "zh-CN": (
                    f"【4年政企行政·39家闭环】您好！针对贵司{job_title}，曾任专职领导联络员并参与大型招商活动会务配合，公文OA与保密扎实，盼发简历沟通！",
                    "4年政府综合行政与领导专职联络经验，统筹对接39家单位双台账闭环，参与千人级招商大会会务配合与300+企业联络。精通办文办会、公文纪要、差旅调度、供应商结算与OA印章流转，守密细致，善用飞书与AI提效。",
                    f"您好！看到贵司招聘 {job_title} 岗位。\n\n我具备 4 年政府机构综合行政经验，曾担任专职领导联络员并统筹对接 39 家成员单位台账闭环，熟悉办文办会、会务现场配合、差旅调度、供应商结算与 OA 归档，保密意识强。随信盼能奉上简历供您评估！"
                ),
                "zh-HK": (
                    f"【4年政企行政·專職聯絡】您好！針對貴司{job_title}，曾統籌39家機構台賬閉環及大型會務配合，公文保密紮實、粵普流利，盼發履歷交流！",
                    "4年政府綜合行政及專職領導聯絡經驗，統籌對接39家單位台賬閉環與大型活動會務配合。熟悉日程差旅、公文紀要、供應商驗收報銷與檔案管理，守密穩重，粵普流利，善用數碼化看板工具。",
                    f"您好！應徵貴司 {job_title} 一職。本人擁有 4 年綜合行政與專職聯絡經驗，處事細心穩重、守密有分寸，粵語及普通話流利，期待有機會面談！"
                ),
                "en": (
                    f"[4Y Gov/Exec Admin · 39-Unit Ops] Hi! Applying for {job_title}. Former dedicated executive liaison experienced in conference logistics, vendor settlement, OA & strict confidentiality. Mandarin & Cantonese!",
                    "4 years of comprehensive administrative & executive liaison experience coordinating schedules, travel, conference support (300+ enterprises), vendor settlements, and tracking ledgers across 39 partner units. Strong document drafting, confidentiality, and digital workflow skills.",
                    f"Dear Hiring Team,\n\nI am applying for the {job_title} role, bringing 4 years of administrative and liaison experience across 39 partner organizations with calm execution and strict discretion."
                ),
                "reasons": [
                    "曾任专职领导联络员，具备扎实的日程差旅协调、会务服务配合与严格保密意识",
                    "统筹对接 39 家单位双台账督办闭环，供应商对接与票据报销规范细致",
                    "公文纪要功底扎实，叠加数字化看板工具习惯，能把繁杂事务打理得井井有条"
                ]
            },
            "track_media_pr": {
                "zh-CN": (
                    f"【广电编导本科·全案宣推】您好！针对贵司{job_title}，具多年文案脚本、专题片摄制剪辑及大型活动物料落地经验，参与过《投资指南》编制，盼发简历评估！",
                    "全日制广播电视编导本科，具选题策划、人物专访、脚本分镜、现场拍摄、Premiere/PS剪辑及大型展会宣介物料全流程交付经验；参与过官方《投资指南》编制，兼具文字功底与AI工具实操能力。",
                    f"您好！关注到贵司招聘 {job_title}。\n\n我毕业于广播电视编导专业，具备从选题策划、采访脚本、拍摄剪辑到线下展会宣传物料落地的全流程经验，期待发送简历与作品进一步沟通！"
                ),
                "zh-HK": (
                    f"【廣電編導本科·全案宣推】您好！針對貴司{job_title}，具多年文案策劃、專題片攝製剪輯及大型活動物料統籌經驗，粵普流利，盼發履歷審閱！",
                    "廣播電視編導全日制本科，獨立完成選題、採訪腳本、拍攝、Premiere/PS剪輯及大型活動宣推物料全流程交付；曾參與官方《投資指南》編製，文字與視覺審美紮實，粵普流利。",
                    f"您好！應徵貴司 {job_title} 職位。本人為廣播電視編導本科畢業，熟悉專題片策劃拍攝、後期剪輯及活動宣傳物料全流程執行，期待與您進一步交流！"
                ),
                "en": (
                    f"[Media BA · End-to-End Video & PR] Hi! Applying for {job_title}. Experienced in scriptwriting, documentary shooting/editing (Premiere/PS), and large-scale event visual collateral!",
                    "BA in Broadcast & TV Directing with end-to-end experience across topic planning, scriptwriting, interviewing, video shooting/editing (Premiere Pro, Photoshop), and offline exhibition/conference collateral execution.",
                    f"Hello,\n\nI am applying for the {job_title} position, bringing a BA in Broadcast & TV Directing and full-cycle video, copywriting, and event PR production experience."
                ),
                "reasons": [
                    "广电编导全日制本科出身，文案脚本、人物专访与视听语言功底扎实",
                    "独立跑通选题、拍摄、Premiere/PS 后期与大型活动现场宣介物料全流程",
                    "参与过官方《投资指南》与招商活动宣发配合，兼顾内容质感与按期交付"
                ]
            }
        }

        t_pack = templates[best_track]
        greeting, advantage, cover = t_pack.get(language, t_pack["zh-CN"])

        return {
            "selected_track": best_track,
            "track_name": self.TRACK_META[best_track]["name"],
            "match_score": base_score,
            "risk_level": veto_report["risk_level"],
            "veto_report": veto_report,
            "dimension_scores": {
                "skill_fit": min(94, base_score + 2),
                "exp_fit": max(68, base_score - 3),
                "growth_value": 90 if best_track in ("track_ai_builder", "track_ai_ops") else 78,
                "safety_score": veto_report["safety_score"],
            },
            "ats_matrix": ats,
            "fit_reasons": t_pack["reasons"],
            "greeting": greeting,
            "profile_advantage": advantage,
            "cover_letter": cover,
        }
