import json
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_PROFILE_FILE = BASE_DIR / "data" / "candidate_profile.json"
LOCAL_RESUMES_DIR = BASE_DIR / "data" / "local_resumes"
TEMPLATE_RESUMES_DIR = BASE_DIR / "resumes"

DEFAULT_PUBLIC_PROFILE: Dict[str, Any] = {
    "candidate_name": "林知行",
    "candidate_aliases": ["林知行", "Alex Lin"],
    "phone": "13800138000",
    "emails": ["candidate@example.com"],
    "university": "南方理工大学",
    "sensitive_entities": [
        "深圳某数字产业服务中心",
        "深圳某创新文化传播机构",
    ],
    "personal_domains": [
        "demo-portfolio.example.com",
    ],
    "sim_meta": "5年经验 · 本科 · 期望：深圳",
    "sim_exp_1": "• 2024–至今   独立产品与 AI 全栈实践 · 已上架 App Store 工具应用",
    "sim_exp_2": "• 2022–2024   大型科技与企服机构 · 跨部门项目统筹与数字化工作流",
}


def load_candidate_profile() -> Dict[str, Any]:
    """优先读取本地 gitignore 保护的 data/candidate_profile.json；不存在时回退通用示例画像"""
    merged = dict(DEFAULT_PUBLIC_PROFILE)
    if LOCAL_PROFILE_FILE.exists():
        try:
            disk = json.loads(LOCAL_PROFILE_FILE.read_text(encoding="utf-8"))
            if isinstance(disk, dict):
                merged.update(disk)
        except Exception:
            pass
    return merged


def resolve_resume_path(filename: str) -> Path:
    """优先读取本机私有母本 data/local_resumes/*.md；若不存在则读取仓库通用示例母本 resumes/*.md"""
    local_p = LOCAL_RESUMES_DIR / filename
    if local_p.exists():
        return local_p
    return TEMPLATE_RESUMES_DIR / filename
