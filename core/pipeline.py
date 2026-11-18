import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cleaner_tier2 import SemanticScoutCleaner
from .db import (
    append_pipeline_log,
    compute_grade,
    create_crawl_run,
    finish_crawl_run,
    get_job,
    list_jobs,
    record_token_usage,
    save_job,
    update_job_pipeline_fields,
    update_job_status,
)
from .delivery_engine import MultiPlatformDeliveryEngine
from .feishu_hub import FeishuHub
from .matcher import JobMatcher
from .resume_tailor import render_job_resume_artifacts
from .scrapers import MultiPlatformScraper
from .settings_manager import SettingsManager
from .veto_engine import VetoEngine


class PipelineOrchestrator:
    """
    HireLens 8 步状态机全链路编排引擎：
    [1.四平台巡航采集] → [2.双漏斗排雷清洗(Tier1规则+Tier2语义)] → [3.四赛道深度评分与破冰语/个人优势生成]
    → [4.无头A4定制PDF与BOSS图片简历生成] → [5.飞书多维表格同步与卡片推送] → [6.人机协同审批闸门] → [7.多平台防封投递]
    """

    def __init__(self, settings_mgr: Optional[SettingsManager] = None, db_path: Optional[Path] = None):
        self.settings_mgr = settings_mgr or SettingsManager()
        self.db_path = db_path
        self.abort_event = threading.Event()
        self._running_lock = threading.Lock()
        self.is_running = False
        self.current_run_id = 0
        self.current_stage = "idle"

    def log(self, message: str, stage: str = "pipeline", level: str = "INFO"):
        self.current_stage = stage
        append_pipeline_log(
            message=message,
            stage=stage,
            level=level,
            run_id=self.current_run_id,
            db_path=self.db_path,
        )

    def request_abort(self):
        self.abort_event.set()
        self.log("🛑 收到紧急中止指令，正在安全收尾当前原子步骤...", stage="abort", level="WARN")

    def run_full_pipeline_async(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if self.is_running:
            return {"success": False, "message": "当前已有巡航流水线正在运行中，请查看实时终端日志或点击中止"}
        t = threading.Thread(target=self.run_full_pipeline_sync, args=(overrides or {},), daemon=True)
        t.start()
        return {"success": True, "message": "全链路智能巡航流水线已在后台启动"}

    def run_full_pipeline_sync(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self._running_lock.acquire(blocking=False):
            return {"success": False, "message": "流水线繁忙中"}

        self.is_running = True
        self.abort_event.clear()
        cfg = self.settings_mgr.load()
        if overrides:
            cfg.update({k: v for k, v in overrides.items() if v is not None})

        platforms = cfg.get("cruise_platforms") or ["boss", "liepin", "51job", "zhilian"]
        if isinstance(platforms, str):
            platforms = [p.strip() for p in platforms.split(",") if p.strip()]
        raw_kws = str(cfg.get("cruise_keywords") or "AI产品经理")
        keywords = [k.strip() for k in raw_kws.replace("，", ",").split(",") if k.strip()]
        city = str(cfg.get("cruise_city") or "深圳")
        max_per_plat = int(cfg.get("cruise_max_per_platform") or 5)
        simulate = bool(cfg.get("cruise_simulate", True))
        use_llm_tier2 = bool(overrides.get("use_llm_tier2", False)) if overrides else False
        render_pdfs = bool(overrides.get("render_pdfs", True)) if overrides else True

        run_id = create_crawl_run(
            platforms=platforms,
            keywords=", ".join(keywords),
            city=city,
            mode="simulate" if simulate else "live",
            db_path=self.db_path,
        )
        self.current_run_id = run_id

        stats = {
            "run_id": run_id,
            "total_found": 0,
            "deduplicated_new": 0,
            "tier1_passed": 0,
            "tier2_passed": 0,
            "tailored_count": 0,
            "saved_job_ids": [],
        }

        try:
            self.log(
                f"🚀 启动全链路流水线 #{run_id} ｜ 平台：{','.join(platforms)} ｜ 城市：{city} ｜ 模式：{'安全沙盒预演' if simulate else '真实持久化浏览器'}",
                stage="capture",
            )

            # Stage 1: 采集
            scraper = MultiPlatformScraper(base_port=int(cfg.get("browser_debug_port") or 19333))
            crawl_res = scraper.cruise(
                platforms=platforms,
                keywords=keywords,
                city=city,
                max_per_platform=max_per_plat,
                simulate=simulate,
                abort_event=self.abort_event,
                log_fn=lambda m, lv: self.log(m, stage="capture", level=lv),
            )

            raw_jobs = crawl_res.get("jobs") or []
            stats["total_found"] = len(raw_jobs)

            if crawl_res.get("captcha_paused"):
                finish_crawl_run(run_id, total_found=len(raw_jobs), status="captcha_paused", error_msg=crawl_res.get("message", ""), db_path=self.db_path)
                return {"success": False, "captcha_paused": True, "stats": stats, "message": crawl_res.get("message", "")}

            existing_jobs = list_jobs(limit=1000, db_path=self.db_path)
            existing_urls = {(j.get("job_url") or "").strip() for j in existing_jobs if j.get("job_url")}
            existing_pairs = {(j.get("company_name"), j.get("job_title")) for j in existing_jobs}

            veto_engine = VetoEngine()
            llm_on = bool(cfg.get("llm_enabled", True)) and not bool(overrides.get("offline_fast"))
            prov = str(cfg.get("llm_provider") or "")
            scout = SemanticScoutCleaner(
                api_key=cfg.get("openai_api_key", ""),
                base_url=cfg.get("openai_base_url", "https://api.deepseek.com"),
                model=cfg.get("openai_model", "deepseek-chat"),
                provider=prov,
                llm_enabled=llm_on,
            )
            matcher = JobMatcher(
                api_key=cfg.get("openai_api_key", "") if llm_on else "",
                base_url=cfg.get("openai_base_url", "https://api.deepseek.com"),
                model=cfg.get("openai_model", "deepseek-chat"),
                provider=prov,
                llm_enabled=llm_on,
            )

            p_price = float(cfg.get("price_prompt_per_m_cny") or 2.0)
            c_price = float(cfg.get("price_completion_per_m_cny") or 8.0)

            for item in raw_jobs:
                if self.abort_event.is_set():
                    break

                u = (item.get("job_url") or "").strip()
                pair = (item.get("company_name"), item.get("job_title"))
                if (u and u in existing_urls) or (pair in existing_pairs and not simulate):
                    self.log(f"⏭️ 去重跳过已存在岗位：{item.get('company_name')} · {item.get('job_title')}", stage="dedup")
                    continue

                stats["deduplicated_new"] += 1
                title = item.get("job_title") or "待核实职位"
                company = item.get("company_name") or "招聘企业"
                jd = item.get("jd_text") or ""
                sal = item.get("salary") or ""
                hr_act = item.get("hr_active_status") or "今日活跃"
                plat = item.get("platform") or "boss"

                # Stage 2A: Tier-1 毫秒级规则排雷
                t1 = veto_engine.evaluate(title, company, jd, hr_act)
                if t1["risk_level"] == "veto":
                    self.log(f"⛔ [Tier-1 拦截] {company} · {title} —— {t1['advice']}", stage="clean_tier1", level="WARN")
                    jid = save_job(
                        job_title=title,
                        company_name=company,
                        salary=sal,
                        city=item.get("city") or city,
                        hr_title=item.get("hr_title") or "",
                        hr_active_status=hr_act,
                        job_url=u,
                        jd_text=jd,
                        selected_track="track_ai_builder",
                        match_score=38,
                        fit_reasons=["命中 Tier-1 硬规则排雷，已自动归档拦截"],
                        greeting_draft="",
                        profile_advantage="",
                        platform=plat,
                        risk_level="veto",
                        veto_report=t1,
                        tier2_score=20,
                        tier2_report={"verdict": "veto", "red_flags": t1["veto_hits"]},
                        pipeline_stage="skipped",
                        approval_status="rejected",
                        db_path=self.db_path,
                    )
                    stats["saved_job_ids"].append(jid)
                    continue

                stats["tier1_passed"] += 1

                # Stage 2B: Tier-2 语义排雷侦察
                t2 = scout.inspect(title, company, sal, jd, use_llm=use_llm_tier2)
                t2_usage = t2.get("usage") or {}
                if t2_usage.get("prompt_tokens") or t2_usage.get("completion_tokens"):
                    record_token_usage(
                        "tier2_clean", cfg.get("openai_model", "deepseek-chat"),
                        t2_usage["prompt_tokens"], t2_usage["completion_tokens"],
                        prompt_price_per_m=p_price, completion_price_per_m=c_price, db_path=self.db_path,
                    )

                if t2["verdict"] == "veto":
                    self.log(f"⛔ [Tier-2 语义拦截] {company} · {title} —— {'; '.join(t2['red_flags'])}", stage="clean_tier2", level="WARN")
                    t1["risk_level"] = "veto"
                    t1["advice"] = f"⛔ Tier-2 语义识破套路：{'；'.join(t2['red_flags'])}"
                    jid = save_job(
                        job_title=title,
                        company_name=company,
                        salary=sal,
                        city=item.get("city") or city,
                        hr_title=item.get("hr_title") or "",
                        hr_active_status=hr_act,
                        job_url=u,
                        jd_text=jd,
                        selected_track="track_ai_builder",
                        match_score=42,
                        fit_reasons=["命中 Tier-2 语义隐形套路排雷，已自动归档拦截"],
                        platform=plat,
                        risk_level="veto",
                        veto_report=t1,
                        tier2_score=t2["tier2_score"],
                        tier2_report=t2,
                        pipeline_stage="skipped",
                        approval_status="rejected",
                        db_path=self.db_path,
                    )
                    stats["saved_job_ids"].append(jid)
                    continue

                stats["tier2_passed"] += 1
                if t2["verdict"] == "warn" and t1["risk_level"] == "safe":
                    t1["risk_level"] = "caution"
                    t1["advice"] = f"⚠️ 语义提示：{'；'.join(t2['red_flags'])}"

                # Stage 3: 四赛道深度路由、评分与重点前置破冰语/BOSS个人优势生成
                eval_res = matcher.match_and_generate(
                    job_title=title,
                    jd_text=jd,
                    company_name=company,
                    hr_active_status=hr_act,
                    language="zh-CN",
                )
                effective_risk = "caution" if (t2["verdict"] == "warn" and eval_res["risk_level"] == "safe") else eval_res["risk_level"]
                grade = compute_grade(eval_res["match_score"], effective_risk)
                app_mode = cfg.get("approval_mode") or "manual_gate"
                approval_st = "auto_pass" if (app_mode == "auto_grade_s" and grade == "S") else "waiting_approval"

                jid = save_job(
                    job_title=title,
                    company_name=company,
                    salary=sal,
                    city=item.get("city") or city,
                    hr_title=item.get("hr_title") or "",
                    hr_active_status=hr_act,
                    job_url=u,
                    jd_text=jd,
                    selected_track=eval_res["selected_track"],
                    match_score=eval_res["match_score"],
                    fit_reasons=eval_res["fit_reasons"],
                    greeting_draft=eval_res["greeting"],
                    profile_advantage=eval_res.get("profile_advantage", ""),
                    platform=plat,
                    language="zh-CN",
                    risk_level=effective_risk,
                    veto_report=t1 if effective_risk == "caution" else eval_res["veto_report"],
                    dimension_scores=eval_res["dimension_scores"],
                    ats_matrix=eval_res["ats_matrix"],
                    cover_letter=eval_res.get("cover_letter", ""),
                    pii_audit=eval_res.get("pii_audit", {}),
                    tier2_score=t2["tier2_score"],
                    tier2_report=t2,
                    pipeline_stage="evaluated",
                    approval_status=approval_st,
                    db_path=self.db_path,
                )
                stats["saved_job_ids"].append(jid)

                e_usage = eval_res.get("usage") or {}
                if e_usage.get("prompt_tokens") or e_usage.get("completion_tokens"):
                    record_token_usage(
                        "analyze", cfg.get("openai_model", "deepseek-chat"),
                        e_usage["prompt_tokens"], e_usage["completion_tokens"],
                        job_id=jid, prompt_price_per_m=p_price, completion_price_per_m=c_price, db_path=self.db_path,
                    )

                self.log(
                    f"🎯 [定级 {grade} · {eval_res['match_score']}分] #{jid} {company} · {title} ｜ 首屏17字：「{eval_res['greeting'][:17]}...」",
                    stage="evaluate",
                )

                # Stage 4: 针对高匹配岗静默渲染无头 A4 PDF 与 BOSS 高清图片简历
                if render_pdfs and grade in ("S", "A", "B"):
                    saved_job = get_job(jid, db_path=self.db_path)
                    if saved_job:
                        art = render_job_resume_artifacts(saved_job)
                        update_job_pipeline_fields(
                            jid,
                            db_path=self.db_path,
                            tailored_resume_md=art["tailored_resume_md"],
                            tailored_pdf_path=art["pdf_path"],
                            tailored_img_path=art["img_path"],
                            pipeline_stage="tailored" if art["success"] else "evaluated",
                        )
                        if art["success"]:
                            stats["tailored_count"] += 1
                            self.log(f"🖨️ [定制简历就绪] #{jid} 已生成 A4 PDF 与高清图片简历", stage="tailor")

                # Stage 5: 飞书卡片实时通知（开启时对 S/A 高分岗推送）
                if cfg.get("feishu_enabled") and grade in ("S", "A"):
                    try:
                        hub = FeishuHub(cfg, db_path=self.db_path)
                        fresh_job = get_job(jid, db_path=self.db_path)
                        if fresh_job:
                            hub.push_card(hub.build_job_card_payload(fresh_job))
                    except Exception as fe:
                        self.log(f"飞书卡片推送提示: {fe}", stage="feishu", level="WARN")

            # 若配置了多维表格且开启飞书同步，执行批量 Upsert
            if cfg.get("feishu_enabled") and cfg.get("feishu_app_token") and cfg.get("feishu_jobs_table_id"):
                try:
                    hub = FeishuHub(cfg, db_path=self.db_path)
                    sync_res = hub.sync_local_to_bitable(limit=50)
                    self.log(f"☁️ 飞书多维表格自动同步完成：新增 {sync_res['created']}，更新 {sync_res['updated']}", stage="feishu")
                except Exception as fe:
                    self.log(f"飞书多维表格同步提示: {fe}", stage="feishu", level="WARN")

            finish_crawl_run(
                run_id,
                total_found=stats["total_found"],
                deduplicated_new=stats["deduplicated_new"],
                tier1_passed=stats["tier1_passed"],
                tier2_passed=stats["tier2_passed"],
                tailored_count=stats["tailored_count"],
                status="aborted" if self.abort_event.is_set() else "completed",
                db_path=self.db_path,
            )
            self.log(
                f"✅ 流水线 #{run_id} 圆满完成：发现 {stats['total_found']} → 新增 {stats['deduplicated_new']} → 双层排雷通过 {stats['tier2_passed']} → 生成定制简历 {stats['tailored_count']} 份！",
                stage="completed",
            )
            return {"success": True, "stats": stats}

        except Exception as e:
            finish_crawl_run(run_id, status="failed", error_msg=str(e), db_path=self.db_path)
            self.log(f"❌ 流水线异常终止: {e}", stage="error", level="ERROR")
            return {"success": False, "message": str(e), "stats": stats}
        finally:
            self.is_running = False
            self.current_stage = "idle"
            self._running_lock.release()

    def batch_deliver_approved(self, job_ids: Optional[List[int]] = None) -> Dict[str, Any]:
        """批量安全投递所有已获人工放行（approved / auto_pass）且处于待投递状态的岗位"""
        cfg = self.settings_mgr.load()
        dry_run = bool(cfg.get("dry_run", True))
        daily_limit = int(cfg.get("daily_delivery_limit") or 35)
        attach_mode = str(cfg.get("delivery_attach_mode") or "greeting_and_img")
        engine = MultiPlatformDeliveryEngine(dry_run=dry_run, base_port=int(cfg.get("browser_debug_port") or 19333))

        all_jobs = list_jobs(limit=500, db_path=self.db_path)
        today = datetime.now().strftime("%Y-%m-%d")
        delivered_today = sum(1 for j in all_jobs if (j.get("delivered_at") or "").startswith(today))

        if job_ids:
            targets = [j for j in all_jobs if j["id"] in set(job_ids)]
        else:
            targets = [
                j for j in all_jobs
                if j.get("delivery_status") == "pending_review"
                and j.get("approval_status") in ("approved", "auto_pass")
                and j.get("risk_level") != "veto"
            ]

        sent_cnt, fail_cnt = 0, 0
        results = []
        self.abort_event.clear()

        for job in targets:
            if self.abort_event.is_set():
                self.log("🛑 批量投递已被手动中止", stage="deliver", level="WARN")
                break
            if (delivered_today + sent_cnt) >= daily_limit and not dry_run:
                self.log(f"⚠️ 达到每日防封投递上限 ({daily_limit})，停止后续自动投递", stage="deliver", level="WARN")
                break

            res = engine.deliver_job(job, attach_mode=attach_mode)
            results.append({"job_id": job["id"], **res})
            if res.get("success"):
                sent_cnt += 1
                update_job_status(job["id"], "delivered", db_path=self.db_path)
                update_job_pipeline_fields(
                    job["id"],
                    db_path=self.db_path,
                    delivery_mode=attach_mode,
                    delivery_log=res.get("message", ""),
                )
                self.log(f"🚀 [投递成功] #{job['id']} {job['company_name']} —— {res.get('message')}", stage="deliver")
            else:
                fail_cnt += 1
                if res.get("captcha_paused"):
                    self.log(res.get("message", "触发验证码暂停"), stage="deliver", level="WARN")
                    break
                update_job_status(job["id"], "failed", db_path=self.db_path)
                update_job_pipeline_fields(job["id"], db_path=self.db_path, delivery_log=res.get("message", ""))
                self.log(f"⚠️ [投递受阻] #{job['id']} {job['company_name']} —— {res.get('message')}", stage="deliver", level="WARN")

        return {
            "success": True,
            "delivered_count": sent_cnt,
            "failed_count": fail_cnt,
            "dry_run": dry_run,
            "results": results,
        }


class BackgroundCronScheduler:
    """轻量级常驻定时调度器：按用户设定的每日时刻自动触发巡航流水线与飞书战报推送（默认关闭，用户开启即生效）"""

    def __init__(self, orchestrator: PipelineOrchestrator):
        self.orchestrator = orchestrator
        self._stop = threading.Event()
        self._last_fired: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(40.0):
            try:
                cfg = self.orchestrator.settings_mgr.load()
                if not cfg.get("scheduler_enabled"):
                    continue
                now = datetime.now()
                day_str = now.strftime("%Y-%m-%d")
                hm_str = now.strftime("%H:%M")

                cruise_hm = str(cfg.get("cruise_cron_time") or "09:30")
                if hm_str == cruise_hm and self._last_fired.get("cruise") != day_str:
                    self._last_fired["cruise"] = day_str
                    self.orchestrator.log(f"⏰ 定时调度器触发每日自动巡航 ({cruise_hm})", stage="scheduler")
                    self.orchestrator.run_full_pipeline_async()

                report_hm = str(cfg.get("report_cron_time") or "20:30")
                if hm_str == report_hm and self._last_fired.get("report") != day_str:
                    self._last_fired["report"] = day_str
                    if cfg.get("feishu_enabled"):
                        hub = FeishuHub(cfg)
                        hub.push_card(hub.build_daily_report_card())
                        self.orchestrator.log(f"⏰ 定时调度器已推送每日飞书求职战报 ({report_hm})", stage="scheduler")
            except Exception:
                pass
