import asyncio
import html
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from core.analytics import build_full_analytics
from core.cleaner_tier2 import SemanticScoutCleaner
from core.company_intel import CompanyIntelEngine
from core.db import (
    append_pipeline_log,
    create_mock_session,
    delete_job,
    export_jobs_csv,
    get_job,
    get_mock_session,
     get_token_summary,
    init_db,
    list_crawl_runs,
    list_feishu_sync_logs,
    list_jobs,
    list_mock_sessions,
    list_pipeline_logs,
    list_token_logs,
    record_token_usage,
    save_interview_prep,
    save_job,
    update_job_greeting,
    update_job_notes,
    update_job_pipeline_fields,
    update_job_status,
    update_mock_session,
)
from core.delivery_engine import MultiPlatformDeliveryEngine
from core.feishu_hub import FeishuHub
from core.interview_copilot import InterviewCopilot
from core.jd_parser import parse_raw_job_posting
from core.llm_client import LLM_PROVIDERS, fetch_available_models, is_llm_available, test_llm_connectivity
from core.matcher import JobMatcher
from core.mock_interviewer import MockInterviewer
from core.pipeline import BackgroundCronScheduler, PipelineOrchestrator
from core.profile_loader import load_candidate_profile, resolve_resume_path
from core.resume_sync import build_online_resume_mapping, open_online_resume_assist
from core.resume_tailor import CHROME_BIN, build_tailored_markdown, markdown_to_clean_html, render_job_resume_artifacts
from core.scrapers import MultiPlatformScraper, inspect_browser_profiles
from core.settings_manager import SettingsManager
from core.veto_engine import VetoEngine
from core.vision_parser import parse_job_image

init_db()
settings_mgr = SettingsManager()
orchestrator = PipelineOrchestrator(settings_mgr=settings_mgr)
cron_scheduler = BackgroundCronScheduler(orchestrator=orchestrator)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cron_scheduler.start()
    yield


app = FastAPI(
    title="HireLens (聘镜) v3.0 - 招聘端视角全链路智能求职系统",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(http://127\.0\.0\.1(:\d+)?|http://localhost(:\d+)?|chrome-extension://[a-z]{32})$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def local_token_security_guard(request: Request, call_next):
    """可选本地 API 令牌门禁：保护本地写操作与配置路由"""
    cfg = settings_mgr.load()
    required_tok = (cfg.get("local_api_token") or "").strip()
    if required_tok and request.url.path.startswith("/api/") and request.method in ("POST", "DELETE"):
        # 允许飞书回调自身的 challenge 握手
        if request.url.path != "/api/feishu/webhook-callback":
            provided = request.headers.get("X-HireLens-Token", "") or request.query_params.get("token", "")
            if provided != required_tok:
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "detail": "本地安全门禁拦截：请在右上角或系统设置中输入正确的 X-HireLens-Token"},
                )
    return await call_next(request)


def _cfg() -> Dict[str, Any]:
    return settings_mgr.load()


def get_matcher(cfg: Optional[Dict[str, Any]] = None) -> JobMatcher:
    c = cfg or _cfg()
    return JobMatcher(
        api_key=c.get("openai_api_key") or config.OPENAI_API_KEY,
        base_url=c.get("openai_base_url") or config.OPENAI_BASE_URL,
        model=c.get("openai_model") or config.OPENAI_MODEL,
        provider=str(c.get("llm_provider") or ""),
        llm_enabled=bool(c.get("llm_enabled", True)),
    )


def get_copilot(cfg: Optional[Dict[str, Any]] = None) -> InterviewCopilot:
    c = cfg or _cfg()
    return InterviewCopilot(
        api_key=c.get("openai_api_key") or config.OPENAI_API_KEY,
        base_url=c.get("openai_base_url") or config.OPENAI_BASE_URL,
        model=c.get("openai_model") or config.OPENAI_MODEL,
        provider=str(c.get("llm_provider") or ""),
        llm_enabled=bool(c.get("llm_enabled", True)),
    )


# ==================== Pydantic Request Models ====================
class RawPasteInput(BaseModel):
    raw_text: str


class ImageParseInput(BaseModel):
    image_data: str
    vision_provider: str = ""


class JobInput(BaseModel):
    job_title: str = ""
    company_name: str = ""
    salary: str = ""
    city: str = "深圳"
    hr_title: str = ""
    hr_active_status: str = "今日活跃"
    job_url: str = ""
    jd_text: str = ""
    raw_text: str = ""
    platform: str = "manual"
    language: str = "zh-CN"
    preferred_track: str = ""
    auto_render_pdf: bool = False


class RegenerateInput(BaseModel):
    language: str = "zh-CN"
    preferred_track: str = ""


class GreetingUpdate(BaseModel):
    greeting: str
    profile_advantage: Optional[str] = None
    cover_letter: Optional[str] = None


class StatusUpdate(BaseModel):
    status: str


class ApprovalUpdate(BaseModel):
    approval_status: str


class NotesUpdate(BaseModel):
    notes: str


class RulesPayload(BaseModel):
    hard_veto_keywords: List[str] = []
    caution_keywords: List[str] = []
    green_keywords: List[str] = []
    blacklist_companies: List[str] = []


class PipelineRunPayload(BaseModel):
    cruise_platforms: Optional[List[str]] = None
    cruise_keywords: Optional[str] = None
    cruise_city: Optional[str] = None
    cruise_max_per_platform: Optional[int] = None
    cruise_simulate: Optional[bool] = None
    use_llm_tier2: bool = False
    render_pdfs: bool = True
    offline_fast: bool = False
    sync: bool = False


class BatchDeliverPayload(BaseModel):
    job_ids: Optional[List[int]] = None


class FeishuBotCommandPayload(BaseModel):
    command: str


class MockStartPayload(BaseModel):
    job_id: int


class MockAnswerPayload(BaseModel):
    answer: str


# ==================== Core Pages & Basic Stats ====================
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    template_path = PROJECT_ROOT / "web" / "templates" / "index.html"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")
    return "<h1>HireLens 模板文件缺失，请检查 web/templates/index.html</h1>"


@app.get("/api/stats")
async def get_stats():
    cfg = _cfg()
    jobs = list_jobs(limit=1500)
    today = datetime.now().strftime("%Y-%m-%d")
    delivered_today = sum(1 for j in jobs if (j.get("delivered_at") or "").startswith(today))

    counts = {
        "pending_review": 0,
        "delivered": 0,
        "interviewing": 0,
        "offer": 0,
        "rejected": 0,
        "failed": 0,
    }
    waiting_approval = 0
    approved_ready = 0
    for j in jobs:
        st = j.get("delivery_status") or "pending_review"
        counts[st] = counts.get(st, 0) + 1
        if st == "pending_review" and j.get("risk_level") != "veto":
            if j.get("approval_status") in ("approved", "auto_pass"):
                approved_ready += 1
            else:
                waiting_approval += 1

    eff_key = cfg.get("openai_api_key") or config.OPENAI_API_KEY
    eff_url = cfg.get("openai_base_url") or config.OPENAI_BASE_URL
    llm_enabled = bool(cfg.get("llm_enabled", True))
    has_llm = is_llm_available(eff_key, eff_url, llm_enabled)
    return {
        "total_jobs": len(jobs),
        "delivered_today": delivered_today,
        "daily_limit": int(cfg.get("daily_delivery_limit") or config.DAILY_DELIVERY_LIMIT),
        "pending_review": counts["pending_review"],
        "waiting_approval": waiting_approval,
        "approved_ready": approved_ready,
        "status_counts": counts,
        "dry_run": bool(cfg.get("dry_run", config.DRY_RUN)),
        "has_llm": has_llm,
        "llm_enabled": llm_enabled,
        "llm_provider": cfg.get("llm_provider") or "deepseek",
        "model": (cfg.get("openai_model") or config.OPENAI_MODEL) if has_llm else "local-heuristic",
        "feishu_enabled": bool(cfg.get("feishu_enabled")),
        "pipeline_running": orchestrator.is_running,
        "pipeline_stage": orchestrator.current_stage,
        "tokens": get_token_summary(),
        "candidate": {
            k: load_candidate_profile().get(k, "")
            for k in ("candidate_name", "sim_meta", "sim_exp_1", "sim_exp_2")
        },
    }


# ==================== Fast Entry: Messy Text + Screenshot VL/OCR ====================
@app.post("/api/parse-raw")
async def parse_raw_endpoint(payload: RawPasteInput):
    parsed = parse_raw_job_posting(payload.raw_text)
    veto = VetoEngine().evaluate(
        parsed["job_title"], parsed["company_name"], parsed["jd_text"], parsed["hr_active_status"]
    )
    return {"success": True, "parsed": parsed, "veto": veto}


@app.post("/api/parse-image")
async def parse_image_endpoint(payload: ImageParseInput):
    cfg = _cfg()
    try:
        res = parse_job_image(
            image_data=payload.image_data,
            vision_provider=payload.vision_provider or cfg.get("vision_provider") or "apple_vision_first",
            vision_api_key=cfg.get("vision_api_key") or "",
            vision_base_url=cfg.get("vision_base_url") or "",
            vision_model=cfg.get("vision_model") or "qwen-vl-plus",
        )
        usage = res.get("usage") or {}
        if usage.get("prompt_tokens") or usage.get("completion_tokens"):
            record_token_usage(
                action="vision_ocr",
                model=cfg.get("vision_model") or "cloud_vl",
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
        return res
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "detail": f"截图识别异常: {str(e)}"})


# ==================== Jobs CRUD, Dual-Clean & Evaluation ====================
@app.get("/api/jobs")
async def get_jobs_list(
    status: str = Query(default=""),
    track: str = Query(default=""),
    search: str = Query(default=""),
    platform: str = Query(default=""),
    grade: str = Query(default=""),
    approval_status: str = Query(default=""),
):
    return list_jobs(
        status=status or None,
        track=track or None,
        search=search or None,
        platform=platform or None,
        grade=grade or None,
        approval_status=approval_status or None,
        limit=250,
    )


@app.post("/api/analyze")
@app.post("/api/clip")
async def analyze_job(payload: JobInput):
    cfg = _cfg()
    if payload.raw_text and (not payload.job_title or not payload.jd_text):
        parsed = parse_raw_job_posting(payload.raw_text)
        job_title = payload.job_title or parsed["job_title"] or "待核实岗位"
        company_name = payload.company_name or parsed["company_name"]
        salary = payload.salary or parsed["salary"]
        city = payload.city if payload.city != "深圳" else parsed["city"]
        hr_active = payload.hr_active_status if payload.hr_active_status != "今日活跃" else parsed["hr_active_status"]
        job_url = payload.job_url or parsed["job_url"]
        platform = payload.platform if payload.platform != "manual" else parsed["platform"]
        jd_text = payload.jd_text or parsed["jd_text"]
    else:
        job_title = payload.job_title.strip()
        company_name = payload.company_name.strip()
        salary = payload.salary.strip()
        city = payload.city.strip() or "深圳"
        hr_active = payload.hr_active_status.strip() or "今日活跃"
        job_url = payload.job_url.strip()
        platform = payload.platform.strip() or "manual"
        jd_text = payload.jd_text.strip()

    if not job_title or not jd_text:
        raise HTTPException(status_code=400, detail="岗位名称与职位描述不能为空")

    try:
        # 双层排雷：Tier-2 本地快速语义扫描补充隐形外包/虚标提示
        scout = SemanticScoutCleaner(api_key="")
        t2_res = scout.inspect(job_title, company_name, salary, jd_text, use_llm=False)

        matcher = get_matcher(cfg)
        eval_result = matcher.match_and_generate(
            job_title=job_title,
            jd_text=jd_text,
            company_name=company_name,
            hr_active_status=hr_active,
            language=payload.language or "zh-CN",
            preferred_track=payload.preferred_track or "",
        )

        effective_risk = eval_result["risk_level"]
        veto_rep = dict(eval_result["veto_report"])
        if t2_res["verdict"] == "veto":
            effective_risk = "veto"
            veto_rep["risk_level"] = "veto"
            veto_rep["advice"] = f"⛔ Tier-2 语义排雷拦截：{'；'.join(t2_res['red_flags'])}"
        elif t2_res["verdict"] == "warn" and effective_risk == "safe":
            effective_risk = "caution"
            veto_rep["risk_level"] = "caution"
            veto_rep["advice"] = f"⚠️ Tier-2 语义提醒：{'；'.join(t2_res['red_flags'])}"

        job_id = save_job(
            job_title=job_title,
            company_name=company_name or "未填公司",
            salary=salary,
            city=city,
            hr_title=payload.hr_title,
            hr_active_status=hr_active,
            job_url=job_url,
            jd_text=jd_text,
            selected_track=eval_result["selected_track"],
            match_score=eval_result["match_score"],
            fit_reasons=eval_result["fit_reasons"],
            greeting_draft=eval_result["greeting"],
            profile_advantage=eval_result.get("profile_advantage", ""),
            platform=platform,
            language=payload.language or "zh-CN",
            risk_level=effective_risk,
            veto_report=veto_rep,
            dimension_scores=eval_result["dimension_scores"],
            ats_matrix=eval_result["ats_matrix"],
            cover_letter=eval_result.get("cover_letter", ""),
            pii_audit=eval_result.get("pii_audit", {}),
            tier2_score=t2_res["tier2_score"],
            tier2_report=t2_res,
        )

        usage = eval_result.get("usage") or {}
        if usage.get("prompt_tokens") or usage.get("completion_tokens"):
            record_token_usage(
                action="analyze",
                model=cfg.get("openai_model") or config.OPENAI_MODEL,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                job_id=job_id,
                prompt_price_per_m=float(cfg.get("price_prompt_per_m_cny") or 2.0),
                completion_price_per_m=float(cfg.get("price_completion_per_m_cny") or 8.0),
            )

        if payload.auto_render_pdf:
            saved_tmp = get_job(job_id)
            if saved_tmp:
                art = render_job_resume_artifacts(saved_tmp)
                update_job_pipeline_fields(
                    job_id,
                    tailored_resume_md=art["tailored_resume_md"],
                    tailored_pdf_path=art["pdf_path"],
                    tailored_img_path=art["img_path"],
                    pipeline_stage="tailored",
                )

        saved = get_job(job_id)
        append_pipeline_log(f"完成岗位诊断入库 #{job_id}：{company_name or '未填公司'} · {job_title} ({eval_result['match_score']}分)", stage="studio")
        return {"success": True, "job": saved, "eval": eval_result}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"success": False, "detail": f"分析或存储异常: {str(e)}"})


@app.post("/api/jobs/{job_id}/regenerate")
async def regenerate_job_eval(job_id: int, payload: RegenerateInput):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")

    cfg = _cfg()
    matcher = get_matcher(cfg)
    eval_result = matcher.match_and_generate(
        job_title=job["job_title"],
        jd_text=job["jd_text"],
        company_name=job.get("company_name", ""),
        hr_active_status=job.get("hr_active_status", "今日活跃"),
        language=payload.language,
        preferred_track=payload.preferred_track,
    )

    save_job(
        job_title=job["job_title"],
        company_name=job.get("company_name", "未填公司"),
        salary=job.get("salary", ""),
        city=job.get("city", "深圳"),
        hr_title=job.get("hr_title", ""),
        hr_active_status=job.get("hr_active_status", "今日活跃"),
        job_url=job.get("job_url") or "",
        jd_text=job["jd_text"],
        selected_track=eval_result["selected_track"],
        match_score=eval_result["match_score"],
        fit_reasons=eval_result["fit_reasons"],
        greeting_draft=eval_result["greeting"],
        profile_advantage=eval_result.get("profile_advantage", ""),
        platform=job.get("platform", "manual"),
        language=payload.language,
        risk_level=eval_result["risk_level"],
        veto_report=eval_result["veto_report"],
        dimension_scores=eval_result["dimension_scores"],
        ats_matrix=eval_result["ats_matrix"],
        cover_letter=eval_result.get("cover_letter", ""),
        pii_audit=eval_result.get("pii_audit", {}),
    )

    usage = eval_result.get("usage") or {}
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        record_token_usage(
            action="regenerate",
            model=cfg.get("openai_model") or config.OPENAI_MODEL,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            job_id=job_id,
        )

    return {"success": True, "job": get_job(job_id), "eval": eval_result}


@app.post("/api/jobs/{job_id}/interview-prep")
async def generate_interview_prep_endpoint(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")

    cfg = _cfg()
    matcher = get_matcher(cfg)
    track_key = job.get("selected_track") or "track_ai_builder"
    resume_md = matcher.resumes.get(track_key) or matcher.resumes.get("track_ai_builder", "")

    copilot = get_copilot(cfg)
    prep = copilot.generate_prep(job, resume_md)

    usage = prep.pop("usage", {})
    save_interview_prep(job_id, prep)
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        record_token_usage(
            action="interview_prep",
            model=cfg.get("openai_model") or config.OPENAI_MODEL,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            job_id=job_id,
        )

    return {"success": True, "interview_prep": prep, "job": get_job(job_id)}


@app.post("/api/jobs/{job_id}/company-intel")
async def generate_company_intel_endpoint(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")

    cfg = _cfg()
    engine = CompanyIntelEngine(
        api_key=cfg.get("openai_api_key") or config.OPENAI_API_KEY,
        base_url=cfg.get("openai_base_url") or config.OPENAI_BASE_URL,
        model=cfg.get("openai_model") or config.OPENAI_MODEL,
        serper_api_key=cfg.get("serper_api_key") or "",
        provider=str(cfg.get("llm_provider") or ""),
        llm_enabled=bool(cfg.get("llm_enabled", True)),
    )
    intel = engine.analyze(
        company_name=job.get("company_name", ""),
        job_title=job.get("job_title", ""),
        jd_text=job.get("jd_text", ""),
        salary=job.get("salary", ""),
    )
    usage = intel.pop("usage", {})
    update_job_pipeline_fields(job_id, company_intel=intel)
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        record_token_usage(
            action="company_intel",
            model=cfg.get("openai_model") or config.OPENAI_MODEL,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            job_id=job_id,
        )
    return {"success": True, "company_intel": intel, "job": get_job(job_id)}


@app.post("/api/jobs/{job_id}/greeting")
async def update_greeting(job_id: int, payload: GreetingUpdate):
    update_job_greeting(
        job_id,
        greeting=payload.greeting,
        cover_letter=payload.cover_letter,
        profile_advantage=payload.profile_advantage,
    )
    return {"success": True, "message": "文案已保存"}


@app.post("/api/jobs/{job_id}/status")
async def change_job_status(job_id: int, payload: StatusUpdate):
    allowed = {"pending_review", "delivered", "interviewing", "offer", "rejected", "failed"}
    if payload.status not in allowed:
        raise HTTPException(status_code=400, detail="状态值不合法")
    update_job_status(job_id, payload.status)
    return {"success": True, "status": payload.status}


@app.post("/api/jobs/{job_id}/approval")
async def change_job_approval(job_id: int, payload: ApprovalUpdate):
    allowed = {"waiting_approval", "approved", "auto_pass", "rejected"}
    if payload.approval_status not in allowed:
        raise HTTPException(status_code=400, detail="审批状态不合法")
    updates: Dict[str, Any] = {"approval_status": payload.approval_status}
    if payload.approval_status == "rejected":
        updates["delivery_status"] = "rejected"
    update_job_pipeline_fields(job_id, **updates)
    return {"success": True, "approval_status": payload.approval_status, "job": get_job(job_id)}


@app.post("/api/jobs/{job_id}/notes")
async def save_notes_endpoint(job_id: int, payload: NotesUpdate):
    update_job_notes(job_id, payload.notes)
    return {"success": True, "message": "备注已保存"}


@app.delete("/api/jobs/{job_id}")
async def remove_job_endpoint(job_id: int):
    delete_job(job_id)
    return {"success": True}


# ==================== Multi-Platform Delivery & Tailored Resume Artifacts ====================
@app.post("/api/jobs/{job_id}/deliver")
async def deliver_job_endpoint(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")

    cfg = _cfg()
    dry_run = bool(cfg.get("dry_run", config.DRY_RUN))
    daily_limit = int(cfg.get("daily_delivery_limit") or config.DAILY_DELIVERY_LIMIT)
    attach_mode = str(cfg.get("delivery_attach_mode") or "greeting_and_img")

    today = datetime.now().strftime("%Y-%m-%d")
    all_jobs = list_jobs(limit=600)
    delivered_today = sum(1 for j in all_jobs if (j.get("delivered_at") or "").startswith(today))
    if delivered_today >= daily_limit and not dry_run:
        return {
            "success": False,
            "message": f"已触发每日安全防封上限（{delivered_today}/{daily_limit}），建议明日再投或手动沟通。",
        }

    # 如需附带图片或PDF简历且尚未生成，自动静默补齐
    if attach_mode in ("greeting_and_img", "greeting_and_pdf") and not job.get("tailored_pdf_path"):
        art = render_job_resume_artifacts(job)
        update_job_pipeline_fields(
            job_id,
            tailored_resume_md=art["tailored_resume_md"],
            tailored_pdf_path=art["pdf_path"],
            tailored_img_path=art["img_path"],
        )
        job = get_job(job_id) or job

    engine = MultiPlatformDeliveryEngine(
        dry_run=dry_run,
        base_port=int(cfg.get("browser_debug_port") or config.BROWSER_DEBUG_PORT),
    )
    result = engine.deliver_job(job=job, attach_mode=attach_mode)

    if result.get("success"):
        update_job_status(job_id, "delivered")
        update_job_pipeline_fields(job_id, delivery_mode=attach_mode, delivery_log=result.get("message", ""))
        append_pipeline_log(f"投递完成 #{job_id} ({job['company_name']})：{result.get('message', '')}", stage="deliver")
    else:
        if not result.get("captcha_paused"):
            update_job_status(job_id, "failed")
        update_job_pipeline_fields(job_id, delivery_log=result.get("message", ""))
        append_pipeline_log(f"投递受阻 #{job_id}：{result.get('message', '')}", stage="deliver", level="WARN")

    return result


@app.post("/api/jobs/{job_id}/reject")
async def reject_job_endpoint(job_id: int):
    update_job_status(job_id, "rejected")
    return {"success": True, "message": "已归档跳过"}


@app.post("/api/jobs/{job_id}/render-artifacts")
async def render_job_artifacts_endpoint(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")
    art = render_job_resume_artifacts(job)
    update_job_pipeline_fields(
        job_id,
        tailored_resume_md=art["tailored_resume_md"],
        tailored_pdf_path=art["pdf_path"],
        tailored_img_path=art["img_path"],
        pipeline_stage="tailored" if art["success"] else job.get("pipeline_stage", "evaluated"),
    )
    append_pipeline_log(f"已为 #{job_id} ({job['company_name']}) 生成定制 A4 PDF 与高清图片简历", stage="tailor")
    return {
        "success": art["success"],
        "message": art["message"],
        "pdf_url": f"/api/jobs/{job_id}/artifact/pdf" if art["pdf_path"] else "",
        "png_url": f"/api/jobs/{job_id}/artifact/png" if art["img_path"] else "",
        "job": get_job(job_id),
    }


@app.get("/api/jobs/{job_id}/artifact/{kind}")
async def download_job_artifact(job_id: int, kind: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")

    if kind == "pdf":
        target = job.get("tailored_pdf_path") or ""
        if not target or not os.path.exists(target):
            art = render_job_resume_artifacts(job)
            target = art["pdf_path"]
            update_job_pipeline_fields(job_id, tailored_pdf_path=target, tailored_img_path=art["img_path"], tailored_resume_md=art["tailored_resume_md"])
        if target and os.path.exists(target):
            return FileResponse(target, media_type="application/pdf", filename=Path(target).name)
    elif kind == "png":
        target = job.get("tailored_img_path") or ""
        if not target or not os.path.exists(target):
            art = render_job_resume_artifacts(job)
            target = art["img_path"]
            update_job_pipeline_fields(job_id, tailored_pdf_path=art["pdf_path"], tailored_img_path=target, tailored_resume_md=art["tailored_resume_md"])
        if target and os.path.exists(target):
            return FileResponse(target, media_type="image/png", filename=Path(target).name)

    raise HTTPException(status_code=404, detail="简历产物文件尚未生成")


@app.get("/api/resumes/{track_key}")
async def get_resume_content(track_key: str):
    file_map = {
        "track_ai_builder": "master_ai_builder.md",
        "track_ai_ops": "master_ai_ops.md",
        "track_admin": "master_admin.md",
        "track_media_pr": "master_media_pr.md",
    }
    filename = file_map.get(track_key)
    if not filename:
        raise HTTPException(status_code=404, detail="赛道不存在")

    path = resolve_resume_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="母本文件不存在")

    return {
        "track_key": track_key,
        "track_name": JobMatcher.TRACK_META[track_key]["name"],
        "content": path.read_text(encoding="utf-8"),
    }


@app.get("/jobs/{job_id}/tailored-resume", response_class=HTMLResponse)
async def render_tailored_resume(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")

    md_text = job.get("tailored_resume_md") or build_tailored_markdown(job)
    ats = job.get("ats_matrix") or {}
    prep = job.get("interview_prep") or {}
    body_html = markdown_to_clean_html(md_text, highlight_keywords=ats.get("matched_keywords") or [])

    matched_badges = "".join(
        f'<span class="kw ok">{html.escape(k)}</span>' for k in (ats.get("matched_keywords") or [])
    ) or '<span class="kw">无特定硬词</span>'
    missing_badges = "".join(
        f'<span class="kw warn">{html.escape(k)}</span>' for k in (ats.get("missing_keywords") or [])
    ) or '<span class="kw ok">无明显缺口</span>'
    tweaks_html = "".join(
        f"<li>{html.escape(t)}</li>" for t in (prep.get("resume_tweaks") or job.get("fit_reasons") or [])
    )

    tpl_path = PROJECT_ROOT / "web" / "templates" / "resume_tailored.html"
    tpl = tpl_path.read_text(encoding="utf-8")
    return (
        tpl.replace("{{JOB_TITLE}}", html.escape(job.get("job_title", "")))
           .replace("{{COMPANY_NAME}}", html.escape(job.get("company_name", "")))
           .replace("{{MATCH_SCORE}}", str(job.get("match_score", 0)))
           .replace("{{MATCHED_BADGES}}", matched_badges)
           .replace("{{MISSING_BADGES}}", missing_badges)
           .replace("{{TWEAKS_HTML}}", tweaks_html)
           .replace("{{RESUME_BODY_HTML}}", body_html)
    )


# ==================== Analytics & Token Audit Dashboard ====================
@app.get("/api/analytics")
async def get_analytics_dashboard(days: int = Query(default=14, ge=7, le=60)):
    return build_full_analytics(days=days)


@app.get("/api/tokens/logs")
async def get_token_audit_logs(limit: int = Query(default=80, le=300), action: str = Query(default="")):
    return {
        "summary": get_token_summary(),
        "logs": list_token_logs(limit=limit, action=action or None),
    }


# ==================== Pipeline Command Center & SSE Stream ====================
@app.post("/api/pipeline/run")
async def run_pipeline_endpoint(payload: PipelineRunPayload):
    overrides = {
        "cruise_platforms": payload.cruise_platforms,
        "cruise_keywords": payload.cruise_keywords,
        "cruise_city": payload.cruise_city,
        "cruise_max_per_platform": payload.cruise_max_per_platform,
        "cruise_simulate": payload.cruise_simulate,
        "use_llm_tier2": payload.use_llm_tier2,
        "render_pdfs": payload.render_pdfs,
        "offline_fast": payload.offline_fast,
    }
    if payload.sync:
        return orchestrator.run_full_pipeline_sync(overrides)
    return orchestrator.run_full_pipeline_async(overrides)


@app.post("/api/pipeline/abort")
async def abort_pipeline_endpoint():
    orchestrator.request_abort()
    return {"success": True, "message": "已发出紧急中止信号"}


@app.get("/api/pipeline/status")
async def get_pipeline_status(since_log_id: int = Query(default=0)):
    return {
        "is_running": orchestrator.is_running,
        "current_stage": orchestrator.current_stage,
        "current_run_id": orchestrator.current_run_id,
        "runs": list_crawl_runs(limit=12),
        "logs": list_pipeline_logs(since_id=since_log_id, limit=100),
    }


@app.post("/api/pipeline/batch-deliver")
async def batch_deliver_endpoint(payload: BatchDeliverPayload):
    return orchestrator.batch_deliver_approved(job_ids=payload.job_ids)


@app.get("/api/stream/logs")
async def stream_pipeline_logs(since_id: int = Query(default=0)):
    """SSE 实时日志流：驱动前端底部可折叠实时终端监控抽屉"""
    async def event_gen():
        cursor_id = since_id
        for _ in range(90):
            new_logs = list_pipeline_logs(since_id=cursor_id, limit=50)
            if new_logs:
                cursor_id = new_logs[-1]["id"]
                payload = json.dumps({
                    "is_running": orchestrator.is_running,
                    "stage": orchestrator.current_stage,
                    "logs": new_logs,
                }, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            else:
                hb = json.dumps({
                    "is_running": orchestrator.is_running,
                    "stage": orchestrator.current_stage,
                    "logs": [],
                })
                yield f"data: {hb}\n\n"
            await asyncio.sleep(1.2)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ==================== Feishu Full Ecosystem Endpoints ====================
@app.get("/api/feishu/check")
async def feishu_connectivity_check():
    hub = FeishuHub(_cfg())
    report = hub.check_connectivity_and_tenant()
    report["sync_logs"] = list_feishu_sync_logs(limit=15)
    return report


@app.post("/api/feishu/init-bitable")
async def feishu_auto_init_bitable():
    cfg = _cfg()
    hub = FeishuHub(cfg)
    try:
        res = hub.auto_provision_bitable()
        settings_mgr.save({
            "feishu_enabled": True,
            "feishu_app_token": res["feishu_app_token"],
            "feishu_jobs_table_id": res["feishu_jobs_table_id"],
            "feishu_resumes_table_id": res["feishu_resumes_table_id"],
        })
        append_pipeline_log(f"飞书多维表格一键自动建表成功: app_token={res['feishu_app_token']}", stage="feishu")
        return res
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "detail": str(e)})


@app.post("/api/feishu/sync-push")
async def feishu_sync_push_endpoint():
    hub = FeishuHub(_cfg())
    try:
        res = hub.sync_local_to_bitable(limit=150)
        append_pipeline_log(f"飞书多维表格推送完成：新增 {res['created']}，更新 {res['updated']}", stage="feishu")
        return res
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "detail": str(e)})


@app.post("/api/feishu/sync-pull")
async def feishu_sync_pull_endpoint():
    hub = FeishuHub(_cfg())
    try:
        res = hub.sync_bitable_to_local()
        append_pipeline_log(f"从飞书多维表格反向同步回本地：更新 {res['updated_local']} 条，新批准 {len(res['newly_approved_ids'])} 条", stage="feishu")
        return res
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "detail": str(e)})


@app.post("/api/feishu/push-job/{job_id}")
async def feishu_push_single_job(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="岗位不存在")
    hub = FeishuHub(_cfg())
    card = hub.build_job_card_payload(job)
    return hub.push_card(card)


@app.post("/api/feishu/push-report")
async def feishu_push_daily_report():
    hub = FeishuHub(_cfg())
    card = hub.build_daily_report_card()
    return hub.push_card(card)


@app.post("/api/feishu/bot-command")
async def feishu_bot_command_endpoint(payload: FeishuBotCommandPayload):
    cfg = _cfg()
    hub = FeishuHub(cfg)

    def _trigger_cruise(city: str, kw: str) -> str:
        r = orchestrator.run_full_pipeline_async({"cruise_city": city, "cruise_keywords": kw})
        return r.get("message", "已启动")

    def _trigger_deliver(jid: int) -> Dict[str, Any]:
        j = get_job(jid)
        if not j:
            return {"success": False, "message": "岗位不存在"}
        eng = MultiPlatformDeliveryEngine(dry_run=bool(cfg.get("dry_run", True)))
        res = eng.deliver_job(j, attach_mode=str(cfg.get("delivery_attach_mode") or "greeting_and_img"))
        if res.get("success"):
            update_job_status(jid, "delivered")
        return res

    reply = hub.handle_bot_command(
        payload.command,
        trigger_cruise_fn=_trigger_cruise,
        trigger_deliver_fn=_trigger_deliver,
    )
    return {"success": True, "reply": reply}


@app.post("/api/feishu/webhook-callback")
async def feishu_event_callback(request: Request):
    """兼容飞书开放平台事件订阅回调（含 URL Verification 握手与 im.message.receive_v1 自动回复）"""
    body = await request.json()
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    event = body.get("event") or {}
    msg = event.get("message") or {}
    content_raw = msg.get("content") or "{}"
    try:
        txt = json.loads(content_raw).get("text", "").strip()
    except Exception:
        txt = ""
    txt = re.sub(r"@_user_\d+\s*", "", txt).strip()

    if txt:
        cfg = _cfg()
        hub = FeishuHub(cfg)
        reply = hub.handle_bot_command(txt)
        chat_id = msg.get("chat_id")
        if chat_id and cfg.get("feishu_app_id") and cfg.get("feishu_app_secret"):
            try:
                headers = hub._auth_headers()
                hub._http(
                    "POST",
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": reply}, ensure_ascii=False)},
                    headers,
                )
            except Exception:
                pass
        return {"code": 0, "reply": reply}
    return {"code": 0}


# ==================== Online Resume Sync & Browser Login Profiles ====================
@app.get("/api/online-resume/preview")
async def preview_online_resume_sync(
    track: str = Query(default="track_ai_builder"),
    platform: str = Query(default="boss"),
    job_id: int = Query(default=0),
):
    custom_adv = ""
    if job_id > 0:
        j = get_job(job_id)
        if j:
            custom_adv = j.get("profile_advantage") or ""
            track = j.get("selected_track") or track
    return build_online_resume_mapping(track_key=track, platform=platform, custom_advantage=custom_adv)


@app.post("/api/online-resume/open/{platform}")
async def open_online_resume_window(platform: str):
    cfg = _cfg()
    try:
        return open_online_resume_assist(platform, base_port=int(cfg.get("browser_debug_port") or 19333))
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "detail": str(e)})


@app.get("/api/browser/profiles")
async def get_browser_profiles_status():
    return inspect_browser_profiles()


@app.post("/api/browser/login/{platform}")
async def launch_platform_login(platform: str):
    cfg = _cfg()
    scraper = MultiPlatformScraper(base_port=int(cfg.get("browser_debug_port") or 19333))
    try:
        return scraper.open_login_window(platform)
    except Exception as e:
        return JSONResponse(status_code=400, content={"success": False, "detail": str(e)})


# ==================== Multi-Turn Mock Interview Camp ====================
@app.post("/api/mock-interview/start")
async def start_mock_interview_endpoint(payload: MockStartPayload):
    job = get_job(payload.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="请先选择一个目标岗位")

    cfg = _cfg()
    interviewer = MockInterviewer(
        api_key=cfg.get("openai_api_key") or config.OPENAI_API_KEY,
        base_url=cfg.get("openai_base_url") or config.OPENAI_BASE_URL,
        model=cfg.get("openai_model") or config.OPENAI_MODEL,
        provider=str(cfg.get("llm_provider") or ""),
        llm_enabled=bool(cfg.get("llm_enabled", True)),
    )
    res = interviewer.start_interview(job)
    usage = res.get("usage") or {}
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        record_token_usage(
            "mock_interview",
            cfg.get("openai_model") or config.OPENAI_MODEL,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            job_id=job["id"],
        )

    init_msgs = [{"role": "interviewer", "content": res["interviewer_message"], "ts": datetime.now().strftime("%H:%M:%S")}]
    sid = create_mock_session(
        job_id=job["id"],
        company_name=job.get("company_name", ""),
        job_title=job.get("job_title", ""),
        selected_track=job.get("selected_track", "track_ai_builder"),
        initial_messages=init_msgs,
    )
    return {"success": True, "session": get_mock_session(sid)}


@app.post("/api/mock-interview/{session_id}/answer")
async def answer_mock_interview_endpoint(session_id: int, payload: MockAnswerPayload):
    sess = get_mock_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="模拟面试会话不存在")
    job = get_job(sess["job_id"]) if sess.get("job_id") else {
        "job_title": sess.get("job_title", ""),
        "company_name": sess.get("company_name", ""),
        "selected_track": sess.get("selected_track", "track_ai_builder"),
    }

    cfg = _cfg()
    interviewer = MockInterviewer(
        api_key=cfg.get("openai_api_key") or config.OPENAI_API_KEY,
        base_url=cfg.get("openai_base_url") or config.OPENAI_BASE_URL,
        model=cfg.get("openai_model") or config.OPENAI_MODEL,
        provider=str(cfg.get("llm_provider") or ""),
        llm_enabled=bool(cfg.get("llm_enabled", True)),
    )
    turn_res = interviewer.evaluate_and_followup(job or {}, sess["messages"], payload.answer)
    usage = turn_res.get("usage") or {}
    if usage.get("prompt_tokens") or usage.get("completion_tokens"):
        record_token_usage(
            "mock_interview",
            cfg.get("openai_model") or config.OPENAI_MODEL,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            job_id=sess.get("job_id"),
        )

    msgs = list(sess["messages"])
    msgs.append({
        "role": "candidate",
        "content": payload.answer,
        "coach_feedback": turn_res["coach_feedback"],
        "ts": datetime.now().strftime("%H:%M:%S"),
    })
    msgs.append({
        "role": "interviewer",
        "content": turn_res["next_question"],
        "ts": datetime.now().strftime("%H:%M:%S"),
    })
    update_mock_session(session_id, messages=msgs, status="active")
    return {"success": True, "session": get_mock_session(session_id), "turn": turn_res}


@app.post("/api/mock-interview/{session_id}/finish")
async def finish_mock_interview_endpoint(session_id: int):
    sess = get_mock_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="模拟面试会话不存在")
    scorecard = MockInterviewer(api_key="").generate_final_scorecard(sess["messages"])
    update_mock_session(session_id, messages=sess["messages"], status="completed", scorecard=scorecard)
    return {"success": True, "session": get_mock_session(session_id), "scorecard": scorecard}


@app.get("/api/mock-interview/sessions")
async def list_mock_sessions_endpoint():
    return list_mock_sessions(limit=20)


# ==================== Universal BYOK LLM Gateway Endpoints ====================
class LLMTestPayload(BaseModel):
    provider: str = ""
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"


def _resolve_input_key(submitted_key: str, stored_key: str) -> str:
    s = (submitted_key or "").strip()
    if s == "__CLEAR__":
        return ""
    if not s or "****" in s:
        return (stored_key or "").strip()
    return s


@app.get("/api/llm/providers")
async def list_llm_providers():
    return {"providers": LLM_PROVIDERS}


@app.post("/api/llm/test")
async def test_custom_llm_endpoint(payload: LLMTestPayload):
    cfg = _cfg()
    real_key = _resolve_input_key(payload.api_key, cfg.get("openai_api_key") or config.OPENAI_API_KEY)
    return test_llm_connectivity(
        api_key=real_key,
        base_url=payload.base_url or cfg.get("openai_base_url") or "https://api.deepseek.com",
        model=payload.model or cfg.get("openai_model") or "deepseek-chat",
        provider=payload.provider or cfg.get("llm_provider") or "",
    )


@app.post("/api/llm/models")
async def fetch_llm_models_endpoint(payload: LLMTestPayload):
    cfg = _cfg()
    real_key = _resolve_input_key(payload.api_key, cfg.get("openai_api_key") or config.OPENAI_API_KEY)
    return fetch_available_models(
        api_key=real_key,
        base_url=payload.base_url or cfg.get("openai_base_url") or "https://api.deepseek.com",
        provider=payload.provider or cfg.get("llm_provider") or "",
    )


# ==================== Rules, Settings & CSV Export ====================
@app.get("/api/rules")
async def get_veto_rules():
    return VetoEngine().rules


@app.post("/api/rules")
async def update_veto_rules(payload: RulesPayload):
    saved = VetoEngine().save_rules(payload.model_dump())
    return {"success": True, "rules": saved}


@app.get("/api/settings")
async def get_runtime_settings():
    pub = settings_mgr.get_public()
    return {
        "settings": pub,
        "diagnostics": {
            "chrome_headless_available": os.path.exists(CHROME_BIN),
            "apple_vision_ocr_available": os.path.exists("/usr/bin/swift"),
            "bind_host": config.BIND_HOST,
            "browser_profiles": inspect_browser_profiles(),
        },
    }


@app.post("/api/settings")
async def save_runtime_settings(request: Request):
    body = await request.json()
    saved = settings_mgr.save(body)
    return {"success": True, "settings": saved}


@app.get("/api/export/csv")
async def export_csv_endpoint():
    csv_str = "\ufeff" + export_jobs_csv()
    filename = f"hire_lens_export_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        content=csv_str.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


if __name__ == "__main__":
    import uvicorn
    print(f"🔭 HireLens (聘镜) v3.0 全链路求职系统启动于 http://{config.BIND_HOST}:{config.WEB_PORT}")
    uvicorn.run("web.app:app", host=config.BIND_HOST, port=config.WEB_PORT, reload=False)
