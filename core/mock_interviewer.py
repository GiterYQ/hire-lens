import json
import os
import urllib.request
from typing import Any, Dict, List

from .matcher import JobMatcher, apply_truth_guard
from .pii_masker import PIIMasker
from .llm_client import call_llm, is_llm_available
from .profile_loader import resolve_resume_path


class MockInterviewer:
    """
    多轮实战模拟面试考官引擎（超越静态题库，支持实时追问、单轮即时点评与终局成绩单）：
    全程通过 PIIMasker 双向脱敏，严禁诱导编造虚假项目数据。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str = "deepseek-chat", provider: str = "", llm_enabled: bool = True):
        self.api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("OPENAI_MODEL", "deepseek-chat")
        self.provider = provider
        self.llm_enabled = llm_enabled
        self.masker = PIIMasker()

    def _load_resume(self, track_key: str) -> str:
        meta = JobMatcher.TRACK_META.get(track_key, JobMatcher.TRACK_META["track_ai_builder"])
        p = resolve_resume_path(meta["file"])
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def start_interview(self, job: Dict[str, Any]) -> Dict[str, Any]:
        title = job.get("job_title") or "AI 产品经理"
        company = job.get("company_name") or "招聘团队"
        track = job.get("selected_track") or "track_ai_builder"
        jd = job.get("jd_text") or ""
        resume_md = self._load_resume(track)

        if not is_llm_available(self.api_key, self.base_url, self.llm_enabled):
            first_q = (
                f"你好！我是 {company}「{title}」岗位的一面业务考官。看到你本科是广电编导，"
                f"但过去一年独立上架了 App Store 应用并做过几款 AI 工作流工具。"
                f"请先用 1 分钟简单介绍一下：你做这些项目时最核心解决的一个具体痛点是什么？"
                if track in ("track_ai_builder", "track_ai_ops")
                else f"你好！我是 {company}「{title}」岗位面试官。看到你有 4 年政企机构项目协调与 39 家单位台账闭环经验，请挑一件多线程最繁杂的具体事情讲讲你是怎么推进落地的？"
            )
            return {
                "interviewer_message": first_q,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }

        m_res, r_map = self.masker.mask(resume_md[:1800])
        m_jd, j_map = self.masker.mask(f"{company} - {title}\n{jd[:1200]}")
        merged_map = {**r_map, **j_map}

        prompt = f"""
你现在扮演 `{company}` 招聘 `{title}` 岗位的一线业务主管面试官。
请阅读候选人简历（已脱敏）与岗位 JD，直接以自然口语开启第一轮提问：
简短问好后，针对候选人真实履历与本岗位的结合点（或跨专业转型真实动手细节）抛出**第 1 个具体、有区分度的问题**（不要一次问三四个问题，控制在 110 字内）。

【岗位JD】
{m_jd}

【候选人简历】
{m_res}

严格返回 JSON：{{"interviewer_message": "你的开场提问话术"}}
"""
        try:
            llm_out = call_llm(
                messages=[
                    {"role": "system", "content": "你是真实犀利但不端架子的业务面试官，只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                expect_json=True,
                temperature=0.35,
                timeout=30,
                provider=self.provider,
            )
            parsed = llm_out["json"] or {}
            usage = llm_out["usage"]
            msg = self.masker.unmask(parsed.get("interviewer_message", ""), merged_map)
            return {
                "interviewer_message": msg,
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                },
            }
        except Exception:
            return {
                "interviewer_message": f"你好！我是 {company}「{title}」业务面试官。请结合你简历里最代表性的一个实操项目，讲讲你是怎么拆解需求并把控交付质量的？",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }

    def evaluate_and_followup(
        self,
        job: Dict[str, Any],
        history: List[Dict[str, Any]],
        user_answer: str,
    ) -> Dict[str, Any]:
        title = job.get("job_title") or "AI 产品经理"
        track = job.get("selected_track") or "track_ai_builder"
        resume_md = self._load_resume(track)
        turn_idx = sum(1 for m in history if m.get("role") == "candidate") + 1

        if not is_llm_available(self.api_key, self.base_url, self.llm_enabled):
            fallback_qs = [
                "追问一下细节：在这个过程里，如果大模型返回了不符合预期的胡编结果或者接口超时，你在代码或流程上做了什么兜底？",
                "如果入职我们团队后，遇到业务方需求只有一句话、非常模糊，而且希望三天内看到可用方案，你会怎么推进？",
                "最后一个问题：你对自己接下来 1~2 年在这个岗位上的核心成长预期是什么？你有什么想反问我的吗？",
            ]
            next_q = fallback_qs[min(turn_idx - 1, len(fallback_qs) - 1)]
            score = min(92, 74 + min(16, len(user_answer) // 10))
            return {
                "coach_feedback": {
                    "turn_score": score,
                    "strength": "回答态度诚恳，没有堆砌空洞互联网黑话。",
                    "improvement": "建议补充 1 个具体的工程边界或数字指标（如限流阈值、39家单位闭环或 App Store 过审细节），让说服力更强。",
                    "sample_upgrade": "可补充说明：‘当时我把核心统计用传统确定性代码算好，只让大模型负责通俗解释，从而把数字错误降到了零。’",
                },
                "next_question": next_q,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }

        convo_txt = "\n".join(
            f"{'面试官' if m['role']=='interviewer' else '候选人'}: {m['content']}"
            for m in history[-6:]
        )
        raw_blob = f"目标岗位: {title}\n候选人母本参考:\n{resume_md[:1400]}\n\n历史对话:\n{convo_txt}\n\n候选人最新回答:\n{user_answer}"
        masked_blob, mapping, _ = self.masker.mask_with_audit(raw_blob)

        prompt = f"""
你同时担任【场内业务面试官】与【场外隐形求职教练】。当前是第 {turn_idx} 轮回答。
请基于候选人真实母本（严禁编造母本外经历）对候选人刚刚的回答进行单轮点评，并以面试官身份提出下一个深挖追问（若已满3轮则进入收尾实战题）。

{masked_blob}

严格返回 JSON：
{{
  "coach_feedback": {{
    "turn_score": 0到98整数评分,
    "strength": "本轮回答做得好的一点（25字内）",
    "improvement": "本轮回答欠缺或容易被质疑的一点（35字内）",
    "sample_upgrade": "基于候选人真实简历的一句更扎实平实的示范表达（70字内，禁黑话）"
  }},
  "next_question": "面试官接下来的追问话术（90字内）"
}}
"""
        try:
            llm_out = call_llm(
                messages=[
                    {"role": "system", "content": "你是务实严格的模拟面试系统，只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                expect_json=True,
                temperature=0.3,
                timeout=34,
                provider=self.provider,
            )
            parsed = llm_out["json"] or {}
            usage = llm_out["usage"]

            unmasked = self.masker.unmask_obj(parsed, mapping)
            cf = unmasked.get("coach_feedback") or {}
            cf["sample_upgrade"] = apply_truth_guard(cf.get("sample_upgrade", ""))
            return {
                "coach_feedback": cf,
                "next_question": unmasked.get("next_question", "感谢分享！你还有什么想了解我们团队具体工作的问题吗？"),
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                    "completion_tokens": int(usage.get("completion_tokens", 0)),
                },
            }
        except Exception:
            return {
                "coach_feedback": {
                    "turn_score": 82,
                    "strength": "切入直接，条理清晰。",
                    "improvement": "可再点出具体的落地验证结果或协作复盘细节。",
                    "sample_upgrade": "用具体跑通的功能闭环或双台账追踪机制举例会更有画面感。",
                },
                "next_question": "很好。如果这个项目重新做一遍，你觉得当时哪个边界设计还可以更进一步？",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }

    def generate_final_scorecard(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        cand_msgs = [m for m in history if m.get("role") == "candidate"]
        scores = [
            int((m.get("coach_feedback") or {}).get("turn_score", 82))
            for m in cand_msgs
        ]
        avg = round(sum(scores) / len(scores)) if scores else 80
        return {
            "overall_score": avg,
            "rounds_completed": len(cand_msgs),
            "dimensions": {
                "真实细节支撑度": min(96, avg + 2),
                "岗位痛点命中度": avg,
                "AI/业务边界意识": min(95, avg + 3),
                "反黑话真诚表达": min(98, avg + 4),
            },
            "verdict": (
                "✅ 面试就绪度高：表达真诚有实证，注意在开场前 30 秒先把已上线作品亮出来。"
                if avg >= 80
                else "⚠️ 建议再打磨 1~2 轮：回答时尽量少讲宽泛感受，多给具体动作与边界控制细节。"
            ),
        }
