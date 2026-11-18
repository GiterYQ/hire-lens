import json
import os
import re
import urllib.request
from typing import Any, Dict, List
from .pii_masker import PIIMasker
from .llm_client import call_llm, is_llm_available


# 本地启发式语义陷阱规则（无 API Key 或快速兜底时同步检测隐蔽套路）
_DISGUISED_TRAPS = [
    (r"派驻|驻点|甲方项目|客户现场办公|外派至", "veto", "隐形驻场/人力外派套壳"),
    (r"无责底薪\s*[1-4][kK千]|底薪\s*[2-4]\d{3}.*提成", "veto", "底薪极低靠高提成画饼（疑似虚标综合薪资）"),
    (r"免费试稿|无偿测试|完成完整方案后再安排一面|先做.*3天.*作业", "caution", "面试前要求重度无偿方案/试稿（警惕骗方案）"),
    (r"抗压.*随叫随到|24小时待命|接受常态化加班|自愿加班", "caution", "显式高压超时工作预警"),
    (r"一人独立负责.*(?:开发|产品).*(?:运营|剪辑|销售)", "caution", "一人身兼多职（警惕边界混乱离职坑）"),
]


class SemanticScoutCleaner:
    """
    Tier-2 语义排雷侦察兵（Dual-Funnel 第二层）：
    在通过 Tier-1 本地毫秒级硬词过滤后，使用轻量 Prompt（全程经 PIIMasker 脱敏）
    识别字面未写“外包/单休”但暗藏驻场套壳、虚标底薪、骗方案试稿、一人顶三岗的隐蔽坑位。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str = "deepseek-chat", provider: str = "", llm_enabled: bool = True):
        self.api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("OPENAI_MODEL", "deepseek-chat")
        self.provider = provider
        self.llm_enabled = llm_enabled
        self.masker = PIIMasker()

    def _local_semantic_scan(self, job_title: str, company_name: str, salary: str, jd_text: str) -> Dict[str, Any]:
        blob = f"{job_title}\n{company_name}\n{salary}\n{jd_text}"
        flags: List[str] = []
        verdict = "pass"
        score = 88

        # 检测薪资跨度异常虚标（例如 10-35K 跨度超过 2.6 倍）
        m_sal = re.search(r"(\d{1,2})\s*[-~至]\s*(\d{2,3})\s*[kK]", salary or blob)
        if m_sal:
            low, high = int(m_sal.group(1)), int(m_sal.group(2))
            if low > 0 and high / low >= 2.5:
                flags.append(f"薪资跨度过大（{low}-{high}K），通常仅按最低档定薪或含苛刻绩效")
                verdict = "warn"
                score -= 14

        for pat, level, desc in _DISGUISED_TRAPS:
            if re.search(pat, blob, flags=re.I):
                flags.append(desc)
                if level == "veto":
                    verdict = "veto"
                    score = min(score, 38)
                elif verdict != "veto":
                    verdict = "warn"
                    score = max(55, score - 12)

        return {
            "verdict": verdict,  # pass | warn | veto
            "tier2_score": max(15, score),
            "red_flags": flags,
            "salary_truth_note": flags[0] if flags else "薪资区间与职责边界基本正常，未见明显套壳话术",
            "source": "local_semantic_rules",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    def inspect(
        self,
        job_title: str,
        company_name: str,
        salary: str,
        jd_text: str,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        local_res = self._local_semantic_scan(job_title, company_name, salary, jd_text)
        if local_res["verdict"] == "veto" or not use_llm or not is_llm_available(self.api_key, self.base_url, self.llm_enabled):
            return local_res

        raw_blob = f"职位: {job_title}\n公司: {company_name}\n标称薪资: {salary}\nJD正文:\n{jd_text[:1800]}"
        masked_blob, mapping, _ = self.masker.mask_with_audit(raw_blob)

        prompt = f"""
你是求职防坑语义排雷专家。请快速审阅以下招聘 JD（已脱敏），识别未直接写明但暗藏的职场陷阱：
重点排查：
1. 隐形外包/驻场套壳（如派驻客户现场、与甲方面试）；
2. 虚标高薪（跨度极大、低底薪高提成对赌）；
3. 假招人骗免费试稿/重度测试题；
4. 一人顶三岗职责严重失衡或高压隐形单休。

【待检岗位】
{masked_blob}

请严格返回 JSON：
{{
  "verdict": "pass | warn | veto 三选一（仅当确凿外包驻场/纯销售套壳/严重欺诈时选 veto）",
  "tier2_score": 0到98之间的真实健康度整数,
  "red_flags": ["发现的具体隐蔽疑点，若无则返回空数组"],
  "salary_truth_note": "一句话判断薪资与岗位职责含金量是否实在（35字内）"
}}
"""
        try:
            llm_out = call_llm(
                messages=[
                    {"role": "system", "content": "你是客观犀利的招聘JD排雷审计员，只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                expect_json=True,
                temperature=0.15,
                timeout=28,
                provider=self.provider,
            )
            parsed = llm_out["json"] or {}
            usage = llm_out["usage"]

            unmasked = self.masker.unmask_obj(parsed, mapping)
            merged_flags = list(dict.fromkeys(local_res["red_flags"] + (unmasked.get("red_flags") or [])))
            verdict = unmasked.get("verdict", "pass")
            if verdict not in ("pass", "warn", "veto"):
                verdict = "warn" if merged_flags else "pass"

            return {
                "verdict": verdict,
                "tier2_score": int(unmasked.get("tier2_score", local_res["tier2_score"])),
                "red_flags": merged_flags[:5],
                "salary_truth_note": str(unmasked.get("salary_truth_note") or local_res["salary_truth_note"]),
                "source": "llm_semantic_scout",
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                },
            }
        except Exception:
            return local_res
