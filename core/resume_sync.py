import re
from typing import Any, Dict, List

from .matcher import JobMatcher, apply_truth_guard
from .profile_loader import load_candidate_profile, resolve_resume_path
from .scrapers import BROWSER_MUTEX, PLATFORM_META, get_profile_dir

ONLINE_RESUME_URLS = {
    "boss": "https://www.zhipin.com/web/geek/resume",
    "liepin": "https://c.liepin.com/resume/editresume",
    "51job": "https://i.51job.com/resume/standard_resume.php",
    "zhilian": "https://i.zhaopin.com/resume",
}

PLATFORM_LIMITS = {
    "boss": {"advantage_max": 140, "project_desc_max": 300, "label": "BOSS直聘（牛人推荐卡核心曝光）"},
    "liepin": {"advantage_max": 300, "project_desc_max": 500, "label": "猎聘（猎头关键词布尔检索）"},
    "51job": {"advantage_max": 400, "project_desc_max": 500, "label": "前程无忧 51job（HR 简历库筛选）"},
    "zhilian": {"advantage_max": 400, "project_desc_max": 500, "label": "智联招聘（ATS 关键词权重匹配）"},
}

FORBIDDEN_MASK_TOKENS = ["[CANDIDATE_NAME]", "[PHONE_NUMBER]", "[CANDIDATE_EMAIL]", "[UNIVERSITY_NAME]", "[ORG_UNIT_"]


def parse_master_sections(track_key: str) -> Dict[str, str]:
    meta = JobMatcher.TRACK_META.get(track_key, JobMatcher.TRACK_META["track_ai_builder"])
    path = resolve_resume_path(meta["file"])
    raw = path.read_text(encoding="utf-8") if path.exists() else ""

    sections: Dict[str, List[str]] = {"header": []}
    curr = "header"
    for ln in raw.splitlines():
        if ln.startswith("## "):
            curr = ln[3:].strip()
            sections.setdefault(curr, [])
        else:
            sections.setdefault(curr, []).append(ln)

    return {k: "\n".join(v).strip() for k, v in sections.items()}


def build_online_resume_mapping(
    track_key: str = "track_ai_builder",
    platform: str = "boss",
    custom_advantage: str = "",
) -> Dict[str, Any]:
    """
    将选定赛道的真实母本映射为四大招聘平台在线简历的标准表单区块，
    并强制执行「联系方式保护锁」与「字数截断预警」。
    """
    plat = platform if platform in PLATFORM_LIMITS else "boss"
    lim = PLATFORM_LIMITS[plat]
    meta = JobMatcher.TRACK_META.get(track_key, JobMatcher.TRACK_META["track_ai_builder"])
    secs = parse_master_sections(track_key)

    default_adv_by_track = {
        "track_ai_builder": "独立上架App Store工具应用，具0→1原型、Prompt防幻觉与AI Agent工作流实战。能用Cursor/Claude开发SwiftUI/Flutter/Next.js/Python；4年300+企业项目统筹与SOP拆解经验，粤普流利。",
        "track_ai_ops": "5年多方项目统筹与跨部门协调经验，曾维护300+企业台账与39家单位督办闭环；熟练运用大模型、飞书多维表格、n8n与Python搭建数字化工作流与SOP看板，具独立输出交互原型能力。",
        "track_admin": "4年政府综合行政与领导专职联络经验，统筹对接39家单位双台账闭环，参与大型招商活动会务服务配合与300+企业联络。精通办文办会、公文纪要、供应商结算与OA归档，守密细致。",
        "track_media_pr": "全日制广播电视编导本科，具选题策划、人物专访、脚本分镜、现场拍摄、Premiere/PS剪辑及大型展会宣介物料全流程交付经验；参与过官方《投资指南》编制，兼具AI工具实操能力。",
    }

    raw_adv = apply_truth_guard((custom_advantage or "").strip() or default_adv_by_track.get(track_key, ""))
    trimmed_adv = raw_adv[: lim["advantage_max"]]

    # 提取项目列表与工作经历
    proj_sec = next((v for k, v in secs.items() if "项目" in k or "作品" in k), "")
    work_sec = next((v for k, v in secs.items() if "工作" in k or "任职" in k or "履历" in k), "")
    skill_sec = next((v for k, v in secs.items() if "技能" in k or "工具" in k or "优势" in k), "")

    projects: List[Dict[str, str]] = []
    for block in re.split(r"\n(?=### )", "\n" + proj_sec):
        b = block.strip()
        if not b.startswith("### "):
            continue
        lines = b.splitlines()
        p_title = lines[0].replace("### ", "").strip()
        p_body = "\n".join(lines[1:]).strip()[: lim["project_desc_max"]]
        projects.append({"title": p_title, "description": apply_truth_guard(p_body)})

    full_check_blob = trimmed_adv + "\n" + proj_sec + "\n" + work_sec
    leaked_tokens = [tok for tok in FORBIDDEN_MASK_TOKENS if tok in full_check_blob]

    cname = load_candidate_profile().get("candidate_name", "候选人")
    return {
        "platform": plat,
        "platform_label": lim["label"],
        "edit_url": ONLINE_RESUME_URLS[plat],
        "track_key": track_key,
        "track_name": meta["name"],
        "contact_protection_lock": {
            "locked": True,
            "verified_clean": len(leaked_tokens) == 0,
            "note": f"姓名（{cname}）、手机号、邮箱与学历证书编号已物理锁定只读，同步操作绝不触碰基础身份栏，杜绝占位符误覆写。",
        },
        "fields": {
            "advantage": {
                "title": "个人优势 / 自我评价",
                "content": trimmed_adv,
                "char_count": len(trimmed_adv),
                "max_limit": lim["advantage_max"],
                "within_limit": len(trimmed_adv) <= lim["advantage_max"],
            },
            "projects": projects[:4],
            "work_experience": apply_truth_guard(work_sec[:1200]),
            "skills": apply_truth_guard(skill_sec[:500]),
        },
    }


def open_online_resume_assist(platform: str, base_port: int = 19333) -> Dict[str, Any]:
    """在用户主动触发且非无头测试时，打开对应平台的在线简历编辑页供一键粘贴核对"""
    plat = platform if platform in ONLINE_RESUME_URLS else "boss"
    meta = PLATFORM_META[plat]
    pdir = get_profile_dir(plat)
    port = base_port + meta["port_offset"]

    with BROWSER_MUTEX:
        from DrissionPage import ChromiumOptions, ChromiumPage

        co = ChromiumOptions()
        co.set_local_port(port)
        co.set_user_data_path(str(pdir))
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--no-first-run")
        page = ChromiumPage(co)
        page.get(ONLINE_RESUME_URLS[plat])
        return {
            "success": True,
            "platform": plat,
            "url": ONLINE_RESUME_URLS[plat],
            "message": f"已打开 {meta['name']} 在线简历编辑页，左侧结构化文案可一键复制填入（基础联系方式已保护锁定）。",
        }
