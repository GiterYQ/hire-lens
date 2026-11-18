import json
from pathlib import Path
from typing import Dict, Any, List

RULES_FILE = Path(__file__).resolve().parent.parent / "data" / "veto_rules.json"

DEFAULT_RULES = {
    "hard_veto_keywords": [
        "驻场开发", "外派驻场", "人力外包", "劳务派遣", "无底薪", "纯提成",
        "自费培训", "培训贷", "交押金", "电话销售", "电销催收", "陌拜销售"
    ],
    "caution_keywords": [
        "单休", "大小周", "抗压能力极强", "接受高频加班", "软通动力", "中软国际",
        "博彦科技", "法本信息", "纬创软件", "半年前活跃", "年前活跃", "5月前活跃", "4月前活跃", "3月前活跃"
    ],
    "green_keywords": [
        "双休", "周末双休", "五险一金", "六险一金", "弹性打卡", "弹性工作",
        "远程办公", "Remote", "AI Agent", "大模型", "LLM", "Claude", "Cursor",
        "港资", "外企", "扁平化", "不打卡", "带薪年假", "年终奖"
    ],
    "blacklist_companies": []
}

class VetoEngine:
    """
    Tier-1 毫秒级本地规则排雷引擎：
    结合硬伤词阻断与 HR 活跃度筛查，
    在消耗任何 LLM Token 之前，识别外包驻场、单休电销套壳与僵尸 HR，同时提取优质信号。
    """

    def __init__(self, rules_path: Path | None = None):
        self.rules_path = rules_path or RULES_FILE
        self.rules = self._load_rules()

    def _load_rules(self) -> Dict[str, List[str]]:
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.rules_path.exists():
            with open(self.rules_path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_RULES, f, ensure_ascii=False, indent=2)
            return dict(DEFAULT_RULES)
        try:
            with open(self.rules_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(DEFAULT_RULES)
            merged.update({k: v for k, v in data.items() if isinstance(v, list)})
            return merged
        except Exception:
            return dict(DEFAULT_RULES)

    def save_rules(self, updated_rules: Dict[str, List[str]]) -> Dict[str, List[str]]:
        merged = dict(self.rules)
        for key in ("hard_veto_keywords", "caution_keywords", "green_keywords", "blacklist_companies"):
            if key in updated_rules and isinstance(updated_rules[key], list):
                merged[key] = [str(x).strip() for x in updated_rules[key] if str(x).strip()]
        with open(self.rules_path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        self.rules = merged
        return merged

    def evaluate(self, job_title: str, company_name: str, jd_text: str, hr_active_status: str = "") -> Dict[str, Any]:
        full_blob = f"{job_title}\n{company_name}\n{hr_active_status}\n{jd_text}".lower()

        veto_hits: List[str] = []
        caution_hits: List[str] = []
        green_hits: List[str] = []

        # 1. 黑名单公司检查
        for comp in self.rules.get("blacklist_companies", []):
            if comp and comp.lower() in (company_name or "").lower():
                veto_hits.append(f"黑名单企业：{comp}")

        # 2. 一票否决硬伤词
        for kw in self.rules.get("hard_veto_keywords", []):
            if kw and kw.lower() in full_blob:
                veto_hits.append(kw)

        # 3. 风险预警词（含僵尸 HR）
        for kw in self.rules.get("caution_keywords", []):
            if kw and kw.lower() in full_blob:
                caution_hits.append(kw)

        # 4. 正向加分词
        for kw in self.rules.get("green_keywords", []):
            if kw and kw.lower() in full_blob:
                if kw not in green_hits:
                    green_hits.append(kw)

        if veto_hits:
            risk_level = "veto"
            safety_score = max(15, 45 - len(veto_hits) * 10)
            advice = f"⛔ 命中一票否决雷区（{'、'.join(veto_hits[:3])}），建议直接跳过，避免浪费精力。"
        elif caution_hits:
            risk_level = "caution"
            safety_score = max(55, 78 - len(caution_hits) * 6)
            advice = f"⚠️ 存在潜在雷点（{'、'.join(caution_hits[:3])}），沟通时务必核实清楚考勤/合同主体。"
        else:
            risk_level = "safe"
            safety_score = min(98, 86 + len(green_hits) * 2)
            advice = "✅ 未发现外包、单休或沉睡 HR 等明显雷点，可放心推进沟通。"

        return {
            "risk_level": risk_level,       # safe | caution | veto
            "safety_score": safety_score,   # 0 - 100
            "veto_hits": veto_hits,
            "caution_hits": caution_hits,
            "green_hits": green_hits[:8],
            "advice": advice,
        }
