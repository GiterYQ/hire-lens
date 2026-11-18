import json
import os
import urllib.request
from typing import Any, Dict, List

from .matcher import apply_truth_guard
from .pii_masker import PIIMasker
from .llm_client import call_llm, is_llm_available


class CompanyIntelEngine:
    """
    企业背调情报与面试核验问题研判器：
    - 若配置 SERPER_API_KEY，自动抓取公开网页新闻与评价摘要；
    - 结合 JD 组织信号与大模型（经 PIIMasker 物理脱敏），输出主营研判、工时/外包隐患、切入抓手与 3 个必核底线问题；
    - 严守诚实原则：无联网检索结果时显式声明“基于 JD 文本特征研判”，绝不编造工商注册资本或融资轮次。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "deepseek-chat",
        serper_api_key: str = "",
        provider: str = "",
        llm_enabled: bool = True,
    ):
        self.api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("OPENAI_MODEL", "deepseek-chat")
        self.serper_api_key = serper_api_key or os.getenv("SERPER_API_KEY", "")
        self.provider = provider
        self.llm_enabled = llm_enabled
        self.masker = PIIMasker()

    def _fetch_serper_snippets(self, company_name: str) -> List[Dict[str, str]]:
        if not self.serper_api_key or not company_name or company_name == "未填公司":
            return []
        try:
            req = urllib.request.Request(
                "https://google.serper.dev/search",
                data=json.dumps({"q": f"{company_name} 业务 团队 评价", "gl": "cn", "hl": "zh-cn", "num": 5}).encode("utf-8"),
                headers={
                    "X-API-KEY": self.serper_api_key,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            snippets = []
            for item in (data.get("organic") or [])[:5]:
                snippets.append({
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "link": item.get("link", ""),
                })
            return snippets
        except Exception:
            return []

    def analyze(self, company_name: str, job_title: str, jd_text: str, salary: str = "") -> Dict[str, Any]:
        comp = (company_name or "目标企业").strip()
        snippets = self._fetch_serper_snippets(comp)
        provenance = (
            f"联网公开检索（命中 {len(snippets)} 条网页摘要）+ JD 组织信号分析"
            if snippets
            else "基于岗位 JD 文本与行业特征推演（未配置联网检索密钥，不虚构工商/融资数据）"
        )

        if not is_llm_available(self.api_key, self.base_url, self.llm_enabled):
            return self._offline_intel(comp, job_title, jd_text, salary, provenance, snippets)

        web_ctx = "\n".join(f"- {s['title']}: {s['snippet']}" for s in snippets) or "（无外部网页检索摘要，请严格仅依据 JD 事实分析，不要编造成立年份或融资金额）"
        raw_input = f"企业名称: {comp}\n招聘岗位: {job_title}\n薪资: {salary}\n外部检索摘要:\n{web_ctx}\n\nJD原文:\n{jd_text[:1800]}"
        masked_in, mapping, _ = self.masker.mask_with_audit(raw_input)

        prompt = f"""
你是一名严谨务实的求职背调顾问。请根据以下信息（已脱敏），为候选人生成一份客观、不编造事实的企业与岗位背景研判卡。
注意：如果外部检索摘要为空，严禁猜测或编造该企业的注册资本、成立时间或融资轮次，只从 JD 用词推断其业务阶段与管理风格。

{masked_in}

请严格返回 JSON：
{{
  "business_stage": "从用词与职责推断该团队的主营方向、业务阶段（如初创探索/成熟团队扩编/项目制交付）",
  "culture_and_workload": "从福利、考勤描述与职责颗粒度判断工时压力与协作规范度",
  "entry_leverage": "候选人切入该团队最有说服力的1~2个实战筹码（说人话）",
  "red_flag_watch": ["需要留意的潜在坑点1（若无明显坑点则写常规关注项）", "关注点2"],
  "must_verify_questions": [
    "沟通或一面时建议核实的务实问题1（如劳动合同签署主体/试用期时长与薪资折扣）",
    "核实问题2（如团队当前人员构成与汇报线）",
    "核实问题3（如日常考勤制度与核心交付考核指标）"
  ]
}}
"""
        try:
            llm_out = call_llm(
                messages=[
                    {"role": "system", "content": "你是实事求是的求职背调分析师，严禁捏造未提供的工商融资数据，只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                expect_json=True,
                temperature=0.2,
                timeout=32,
                provider=self.provider,
            )
            parsed = llm_out["json"] or {}
            usage = llm_out["usage"]

            unmasked = self.masker.unmask_obj(parsed, mapping)
            unmasked["entry_leverage"] = apply_truth_guard(unmasked.get("entry_leverage", ""))
            unmasked["provenance"] = provenance
            unmasked["web_snippets"] = snippets
            unmasked["usage"] = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
            }
            return unmasked
        except Exception:
            return self._offline_intel(comp, job_title, jd_text, salary, provenance, snippets)

    def _offline_intel(
        self,
        comp: str,
        job_title: str,
        jd_text: str,
        salary: str,
        provenance: str,
        snippets: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        has_double_off = "双休" in jd_text
        has_outsource = any(k in (comp + jd_text) for k in ("外包", "驻场", "派遣"))
        return {
            "business_stage": f"围绕「{job_title}」岗位招聘，职责偏向具体业务落地与工具实操交付（{comp}）。",
            "culture_and_workload": (
                "⚠️ 存在外包/驻场关键词，需高度警惕甲方派驻模式。"
                if has_outsource
                else ("✅ JD 明确提及双休保障，制度相对清晰。" if has_double_off else "JD 未显式写明双休/打卡细节，建议沟通时顺带确认。")
            ),
            "entry_leverage": "直接展示已上架 App Store 应用或过往 300+ 企业项目推进台账实证，用已跑通作品降低对方试错顾虑。",
            "red_flag_watch": [
                "核实岗位是新增编制还是紧急替补离职空缺",
                f"确认标称薪资（{salary or '面议'}）中的固定底薪与绩效占比",
            ],
            "must_verify_questions": [
                "请问该岗位签署劳动合同与缴纳五险一金的主体就是贵司本部吗？试用期几个月、薪资是否打折？",
                "目前负责这块业务的团队大概几个人，入职后主要和哪几个角色配合？",
                "平时团队上下班时间与周末作息是怎么安排的？",
            ],
            "provenance": provenance,
            "web_snippets": snippets,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
