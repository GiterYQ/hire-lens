import re
from typing import Tuple, Dict, Any
from .profile_loader import load_candidate_profile

class PIIMasker:
    """
    增强型物理双向脱敏引擎：
    在发往任何云端大模型 API 前，将姓名、手机号（含内地/香港）、邮箱、微信号、
    毕业院校、敏感原单位及个人域名物理替换为安全占位符，并生成脱敏审计摘要；
    模型返回结果后在本地内存中递归还原，确保隐私资产零外泄。
    """

    def __init__(
        self,
        candidate_names: list[str] | None = None,
        phone: str | None = None,
        emails: list[str] | None = None,
        university: str | None = None,
        sensitive_entities: list[str] | None = None,
        personal_domains: list[str] | None = None,
    ):
        prof = load_candidate_profile()
        self.candidate_names = candidate_names or prof.get("candidate_aliases") or [prof.get("candidate_name", "林知行")]
        self.phone = phone if phone is not None else prof.get("phone", "13800138000")
        self.emails = emails or prof.get("emails") or ["candidate@example.com"]
        self.university = university if university is not None else prof.get("university", "南方理工大学")
        self.sensitive_entities = sensitive_entities or prof.get("sensitive_entities") or []
        self.personal_domains = personal_domains or prof.get("personal_domains") or []

    def mask_with_audit(self, text: str) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        """返回 (脱敏文本, 还原字典, 脱敏审计报告)"""
        if not text:
            return "", {}, {"masked_count": 0, "categories": []}

        mapping: Dict[str, str] = {}
        categories: list[str] = []
        masked = text
        count = 0

        # 1. 手机号码脱敏（指定号码 + 内地11位 + 香港8位带区号）
        if self.phone and self.phone in masked:
            count += masked.count(self.phone)
            masked = masked.replace(self.phone, "[PHONE_NUMBER]")
            mapping["[PHONE_NUMBER]"] = self.phone
            categories.append("手机号码")

        def _phone_repl(m: re.Match) -> str:
            nonlocal count
            val = m.group(0).strip()
            count += 1
            mapping.setdefault("[PHONE_NUMBER]", val)
            if "手机号码" not in categories:
                categories.append("手机号码")
            return "[PHONE_NUMBER]"

        masked = re.sub(r'(?:\+?86[-\s]?)?1[3-9]\d[\s\-]?\d{4}[\s\-]?\d{4}', _phone_repl, masked)
        masked = re.sub(r'\+852[-\s]?[5689]\d{3}[-\s]?\d{4}', _phone_repl, masked)

        # 2. 邮箱脱敏
        for idx, em in enumerate(self.emails):
            if em and em in masked:
                token = "[CANDIDATE_EMAIL]" if idx == 0 else f"[CANDIDATE_EMAIL_{idx+1}]"
                count += masked.count(em)
                masked = masked.replace(em, token)
                mapping[token] = em
                if "电子邮箱" not in categories:
                    categories.append("电子邮箱")

        def _email_repl(m: re.Match) -> str:
            nonlocal count
            val = m.group(0)
            token = "[EXTERNAL_EMAIL]"
            count += 1
            mapping.setdefault(token, val)
            if "电子邮箱" not in categories:
                categories.append("电子邮箱")
            return token

        masked = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', _email_repl, masked)

        # 3. 微信号脱敏
        def _wx_repl(m: re.Match) -> str:
            nonlocal count
            prefix, wx_id = m.group(1), m.group(2)
            count += 1
            mapping["[WECHAT_ID]"] = wx_id
            if "微信号" not in categories:
                categories.append("微信号")
            return f"{prefix}[WECHAT_ID]"

        masked = re.sub(r'((?:微信|WeChat|weixin)[\s:：]+)([a-zA-Z][a-zA-Z0-9_-]{5,19})', _wx_repl, masked, flags=re.I)

        # 4. 个人域名脱敏
        for idx, dom in enumerate(self.personal_domains):
            if dom and dom in masked:
                token = f"[PORTFOLIO_SITE_{idx+1}]"
                count += masked.count(dom)
                masked = masked.replace(dom, token)
                mapping[token] = dom
                if "个人站点" not in categories:
                    categories.append("个人站点")

        # 5. 姓名脱敏
        for idx, nm in enumerate(self.candidate_names):
            if nm and nm in masked:
                token = "[CANDIDATE_NAME]" if idx == 0 else f"[CANDIDATE_ALIAS_{idx}]"
                count += masked.count(nm)
                masked = masked.replace(nm, token)
                mapping[token] = nm
                if "候选人姓名" not in categories:
                    categories.append("候选人姓名")

        # 6. 毕业院校脱敏
        if self.university and self.university in masked:
            count += masked.count(self.university)
            masked = masked.replace(self.university, "[UNIVERSITY_NAME]")
            mapping["[UNIVERSITY_NAME]"] = self.university
            categories.append("毕业院校")

        # 7. 政企/前雇主机构脱敏（按长度降序，防止子串提前截断）
        sorted_entities = sorted(self.sensitive_entities, key=len, reverse=True)
        for idx, entity in enumerate(sorted_entities):
            if entity and entity in masked:
                token = f"[ORG_UNIT_{idx+1}]"
                count += masked.count(entity)
                masked = masked.replace(entity, token)
                mapping[token] = entity
                if "过往机构" not in categories:
                    categories.append("过往机构")

        return masked, mapping, {
            "masked_count": count,
            "categories": categories,
        }

    def mask(self, text: str) -> Tuple[str, Dict[str, str]]:
        """向后兼容 v1 接口"""
        masked, mapping, _ = self.mask_with_audit(text)
        return masked, mapping

    def unmask(self, text: str, mapping: Dict[str, str]) -> str:
        """从占位符还原为本地真实文本"""
        if not text or not mapping:
            return text or ""
        restored = text
        # 按 token 长度降序替换，避免前缀误碰撞
        for token in sorted(mapping.keys(), key=len, reverse=True):
            restored = restored.replace(token, mapping[token])
        return restored

    def unmask_obj(self, obj: Any, mapping: Dict[str, str]) -> Any:
        """递归还原字典或列表中的所有字符串"""
        if not mapping:
            return obj
        if isinstance(obj, str):
            return self.unmask(obj, mapping)
        if isinstance(obj, list):
            return [self.unmask_obj(item, mapping) for item in obj]
        if isinstance(obj, dict):
            return {k: self.unmask_obj(v, mapping) for k, v in obj.items()}
        return obj
