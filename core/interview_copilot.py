import os
import json
import urllib.request
from typing import Dict, Any
from .pii_masker import PIIMasker
from .matcher import apply_truth_guard
from .llm_client import call_llm, is_llm_available
from .profile_loader import load_candidate_profile

class InterviewCopilot:
    """
    岗位专属「简历靶向微调 + 考官视角 Grill-Me 押题急救包」引擎：
    针对心仪岗位生成：
      1. 简历排布微调建议（不捏造经历，只调顺序与对齐术语）
      2. 面试官最可能深挖/质疑的 3 道必考题 + 基于真实项目的平实回答要点
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str = "deepseek-chat", provider: str = "", llm_enabled: bool = True):
        self.api_key = os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("OPENAI_MODEL", "deepseek-chat")
        self.provider = provider
        self.llm_enabled = llm_enabled
        self.masker = PIIMasker()

    def generate_prep(self, job: Dict[str, Any], resume_md: str) -> Dict[str, Any]:
        job_title = job.get("job_title", "目标岗位")
        company = job.get("company_name", "招聘企业")
        jd_text = job.get("jd_text", "")
        track = job.get("selected_track", "track_ai_builder")

        masked_resume, r_map = self.masker.mask(resume_md)
        masked_jd, j_map = self.masker.mask(f"{job_title}\n{company}\n{jd_text}")
        merged_map = {**r_map, **j_map}

        if not is_llm_available(self.api_key, self.base_url, self.llm_enabled):
            fallback = self._offline_prep(job_title, track)
            fallback["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
            return fallback

        prompt = f"""
你是一名资深一线业务面试官兼求职教练。候选人 `[CANDIDATE_NAME]` 准备投递或面试以下岗位。
请结合候选人的真实母本简历与目标岗位 JD，输出一份**不吹牛、经得起追问**的定制微调与面试急救包。

【目标岗位】
职位: {job_title}（{company}）
JD详情:
{masked_jd}

【候选人当前选定母本（已脱敏）】
{masked_resume}

请严格按 JSON 格式返回：
{{
  "resume_tweaks": [
    "微调动作1：指出母本中哪一段真实项目/经历最该置顶放大，以及改哪几个关键词更贴合该JD",
    "微调动作2：指出哪部分不相关经历应该压缩，避免分散HR注意力",
    "微调动作3：投递该岗时建议同步附带展示的证明材料（如 App Store 链接 / 在线 Demo / 工作台截图）"
  ],
  "elevator_pitch": "30秒口语化自我介绍（约110字，平实自然，直接点出与本岗最相关的2个真实成果，不背诵生硬套话）",
  "grill_questions": [
    {{
      "question": "考官极大概率追问的尖锐问题1（如跨专业背景质疑/AI防幻觉细节/项目真实边界）",
      "intent": "面试官问这题的真实顾虑是什么",
      "answer_points": "基于候选人真实经历的平实回答提纲（说人话，坦诚说明做了什么、没做什么、怎么解决的）"
    }},
    {{
      "question": "考官针对本岗位日常业务落地的实战问题2",
      "intent": "考察意图",
      "answer_points": "回答提纲"
    }},
    {{
      "question": "考官关于协作/抗压/工作流习惯的追问3",
      "intent": "考察意图",
      "answer_points": "回答提纲"
    }}
  ],
  "questions_to_ask_hr": [
    "反问面试官的高质量务实问题1（了解当前团队最急需解决的具体事情）",
    "反问问题2（了解评估试用期产出的核心标准）"
  ]
}}
"""
        try:
            llm_out = call_llm(
                messages=[
                    {"role": "system", "content": "你是一名务实严谨的面试教练，严禁编造经历，只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                expect_json=True,
                temperature=0.3,
                timeout=42,
                provider=self.provider,
            )
            parsed = llm_out["json"] or {}
            usage = llm_out["usage"]

            unmasked = self.masker.unmask_obj(parsed, merged_map)
            unmasked["elevator_pitch"] = apply_truth_guard(unmasked.get("elevator_pitch", ""))
            unmasked["resume_tweaks"] = [apply_truth_guard(x) for x in (unmasked.get("resume_tweaks") or [])]
            for gq in (unmasked.get("grill_questions") or []):
                gq["answer_points"] = apply_truth_guard(gq.get("answer_points", ""))
            unmasked["usage"] = {
                "prompt_tokens": int(usage.get("prompt_tokens", 0)),
                "completion_tokens": int(usage.get("completion_tokens", 0)),
            }
            return unmasked
        except Exception as e:
            fallback = self._offline_prep(job_title, track)
            fallback["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
            fallback["error_note"] = f"已使用本地离线急救包（接口提示: {str(e)[:60]}）"
            return fallback

    def _offline_prep(self, job_title: str, track: str) -> Dict[str, Any]:
        cname = load_candidate_profile().get("candidate_name", "候选人")
        if track in ("track_ai_builder", "track_ai_ops"):
            return {
                "resume_tweaks": [
                    f"将已上架 App Store 应用与本地 AI 工作台置顶，突出与「{job_title}」直接对应的原型落地与 AI 边界控制细节。",
                    "将传统综合协调与编导经历压缩在简历底部 2 行以内，只保留“300+企业台账推进”与“端到端交付”。",
                    "沟通或邮件投递时，附上已上线应用商店链接与在线可交互原型链接，眼见为实。"
                ],
                "elevator_pitch": f"您好，我是{cname}。我虽然本科是广电编导，但过去一年用 Cursor 和 Claude 独立从 0 到 1 做出了几款产品，包括一款已经过审上架 App Store 的个人工具应用。加上之前 4 年大型项目协调经验，我很习惯把模糊需求快速搭成能跑的原型并控好防幻觉与隐私边界，所以很希望能加入团队负责 {job_title}。",
                "grill_questions": [
                    {
                        "question": "你不是计算机科班出身，代码都是用 AI 写的，遇到复杂 Bug 或线上事故怎么排查？",
                        "intent": "考察是否只是盲目粘贴代码，还是真正具备工程控制力与边界意识。",
                        "answer_points": "坦诚说明借助 AI 编码，但架构拆解、测试门禁和排障由自己把控：举例 该应用 后台用 GitHub Actions 跑 lint/test/build，客户端用 Sentry 定位过列表卡顿，且所有 AI 解读接口都把核心计算用传统代码算死，不让模型乱发挥。"
                    },
                    {
                        "question": "你在项目里是怎么防止大模型胡说八道（幻觉）和泄露用户隐私的？",
                        "intent": "考察 AI 产品/Agent 落地的实操安全意识。",
                        "answer_points": "结合两个真实例子：① 该应用 把统计计算放在确定性代码里，AI 只做通俗解读并配双维限流；② HireLens 发往云端前先把姓名、电话、单位物理替换成占位符，本地再还原；通勤模块 则是默认关闭读取，必须用户显式勾选授权。"
                    },
                    {
                        "question": "你之前的经历包含编导和政府项目协调，为什么现在确定要做 AI 方向？",
                        "intent": "考察求职动机稳定性与跨界迁移价值。",
                        "answer_points": "过去做协调和编导本质都是‘把模糊需求变成最终交付物’。接触 AI 工具后发现以前需要一个团队做的产品现在一个人就能跑通闭环，每天都在真实写项目迭代，因此希望全职投入。"
                    }
                ],
                "questions_to_ask_hr": [
                    f"请问这个 {job_title} 岗位入职后前 1~2 个月，团队最希望优先跑通或解决的具体业务问题是什么？",
                    "目前团队内部在 AI 工作流或 Agent 落地时，更看重快速出原型验证，还是对已有系统的稳定性打磨？"
                ]
            }
        return {
            "resume_tweaks": [
                f"针对「{job_title}」重点突出 39 家成员单位台账督办闭环、专职领导联络及大型招商活动会务配合经验。",
                "将数字化看板与 AI 辅助文书整理作为效率加分项点缀在技能栏，体现做事条理清晰、不墨守成规。",
                "准备好过往台账管理模板思路或公文/物料代表案例，面试时用具体数字（39家单位、300+企业）说话。"
            ],
            "elevator_pitch": f"您好，我是{cname}，全日制本科毕业，在深圳有 4 年大型机构综合行政与项目协调经验。做过专职联络员，也统筹过 39 家单位的台账督办和大型活动服务配合，平时还会用飞书看板和 AI 工具提高日常整理效率，做事稳妥守密，很希望能胜任贵司的 {job_title}。",
            "grill_questions": [
                {
                    "question": "如果同时遇到领导临时交办急事、跨部门催材料和日常报销截止，你怎么排优先级？",
                    "intent": "考察多线程抗压与轻重缓急判断。",
                    "answer_points": "引用在宣传部/企服中心对接 39 家单位时的双台账经验：先花 3 分钟确认硬截止时间与影响面，紧急涉领导日程的立即响应并同步预期时间，常规报销与材料按节点批量处理，事后留痕闭环。"
                },
                {
                    "question": "从体制内单位转到企业行政/运营，你觉得最大的差异是什么，能适应快节奏吗？",
                    "intent": "考察适应度与服务心态。",
                    "answer_points": "说明自己在招商中心期间本身就高频对接 300+ 市场化企业与外包供应商，也有过独立工作室自负盈亏经历，习惯结果导向和快节奏响应，同时保留了严谨守密的职业习惯。"
                },
                {
                    "question": "你在大型会议和接待中具体承担什么角色？",
                    "intent": "核实简历真实度。",
                    "answer_points": "实事求是说明：主要负责前期企业分级联络台账、对接外包会务落实标准、领导行程车辆差旅衔接以及现场流程配合保障，确保各环节不掉链子。"
                }
            ],
            "questions_to_ask_hr": [
                f"请问目前这个 {job_title} 岗位日常占比最高的三块具体事务是什么？",
                "贵司对该岗位入职首月快速上手最看重的配合习惯是什么？"
            ]
        }
