from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from .db import db_session, list_jobs, get_token_summary, list_token_logs, list_crawl_runs

PLATFORM_LABELS = {
    "boss": "BOSS直聘",
    "liepin": "猎聘",
    "51job": "前程无忧(51job)",
    "zhilian": "智联招聘",
    "jobsdb": "香港 JobsDB",
    "linkedin": "LinkedIn",
    "vision_ocr": "截图识岗(OCR/VL)",
    "clipper": "浏览器剪藏插件",
    "manual": "工作台手动录入",
}

TRACK_LABELS = {
    "track_ai_builder": "AI产品经理 / Agent开发",
    "track_ai_ops": "项目运营 / 数字化工作流",
    "track_admin": "行政综合 / 专职联络",
    "track_media_pr": "品牌物料 / 视频编导",
}

ACTION_LABELS = {
    "analyze": "岗位深度路由与话术生成",
    "regenerate": "多语种/切换赛道重算",
    "tier2_clean": "Tier-2 语义排雷初筛",
    "resume_tailor": "靶向简历摘要定制",
    "interview_prep": "考官押题急救包",
    "company_intel": "企业背调情报研判",
    "mock_interview": "多轮模拟面试对练",
    "feishu_bot": "飞书机器人自然语言指令",
    "vision_ocr": "云端视觉模型截图识岗",
}


def build_full_analytics(days: int = 14, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """构建全景求职漏斗、渠道 ROI、赛道分布、14日趋势与大模型 Token 审计大盘"""
    jobs = list_jobs(limit=2000, db_path=db_path)
    total = len(jobs)

    passed_clean = [j for j in jobs if (j.get("risk_level") or "safe") != "veto"]
    high_grade = [j for j in passed_clean if (j.get("grade") or "B") in ("S", "A")]
    tailored_ready = [
        j for j in passed_clean
        if (j.get("greeting_final") or j.get("greeting_draft") or j.get("tailored_pdf_path"))
    ]
    delivered_plus = [
        j for j in jobs
        if (j.get("delivery_status") or "") in ("delivered", "interviewing", "offer")
    ]
    interview_plus = [
        j for j in jobs
        if (j.get("delivery_status") or "") in ("interviewing", "offer")
    ]
    offers = [j for j in jobs if (j.get("delivery_status") or "") == "offer"]

    raw_stages = [
        ("captured", "1. 多源采集入库", len(jobs)),
        ("cleaned", "2. 双层排雷通过", len(passed_clean)),
        ("tailored", "3. 定制话术/简历就绪", len(tailored_ready)),
        ("high_grade", "4. S/A 级高匹配精选", len(high_grade)),
        ("delivered", "5. 已投递触达 HR", len(delivered_plus)),
        ("interviewing", "6. 沟通反馈 / 约面", len(interview_plus)),
        ("offer", "7. 斩获录用 Offer", len(offers)),
    ]

    funnel: List[Dict[str, Any]] = []
    prev_cnt = len(jobs)
    for key, label, cnt in raw_stages:
        rate_top = round((cnt / total * 100), 1) if total > 0 else 0.0
        rate_prev = round((cnt / prev_cnt * 100), 1) if prev_cnt > 0 else (100.0 if cnt > 0 else 0.0)
        funnel.append({
            "key": key,
            "label": label,
            "count": cnt,
            "rate_from_top": rate_top,
            "rate_from_prev": min(100.0, rate_prev),
        })
        prev_cnt = cnt if cnt > 0 else prev_cnt

    # 2. 渠道 ROI 矩阵
    plat_buckets: Dict[str, List[Dict[str, Any]]] = {}
    for j in jobs:
        p = (j.get("platform") or "manual").lower()
        plat_buckets.setdefault(p, []).append(j)

    platform_roi = []
    for p_key, p_jobs in sorted(plat_buckets.items(), key=lambda x: len(x[1]), reverse=True):
        p_total = len(p_jobs)
        p_veto = sum(1 for x in p_jobs if x.get("risk_level") == "veto")
        p_scores = [int(x.get("match_score") or 0) for x in p_jobs]
        p_sa = sum(1 for x in p_jobs if x.get("grade") in ("S", "A"))
        p_deliv = sum(1 for x in p_jobs if x.get("delivery_status") in ("delivered", "interviewing", "offer"))
        p_inter = sum(1 for x in p_jobs if x.get("delivery_status") in ("interviewing", "offer"))
        p_offer = sum(1 for x in p_jobs if x.get("delivery_status") == "offer")
        platform_roi.append({
            "platform": p_key,
            "label": PLATFORM_LABELS.get(p_key, p_key),
            "total": p_total,
            "veto_count": p_veto,
            "clean_rate": round(((p_total - p_veto) / p_total * 100), 1) if p_total else 0.0,
            "avg_score": round(sum(p_scores) / p_total, 1) if p_total else 0.0,
            "sa_count": p_sa,
            "delivered": p_deliv,
            "interview": p_inter,
            "offer": p_offer,
            "reply_rate": round((p_inter / p_deliv * 100), 1) if p_deliv else 0.0,
        })

    # 3. 四大赛道表现矩阵
    track_matrix = []
    for t_key, t_label in TRACK_LABELS.items():
        t_jobs = [j for j in jobs if j.get("selected_track") == t_key]
        t_total = len(t_jobs)
        t_scores = [int(x.get("match_score") or 0) for x in t_jobs]
        t_sa = sum(1 for x in t_jobs if x.get("grade") in ("S", "A"))
        t_deliv = sum(1 for x in t_jobs if x.get("delivery_status") in ("delivered", "interviewing", "offer"))
        t_inter = sum(1 for x in t_jobs if x.get("delivery_status") in ("interviewing", "offer"))
        top_job = max(t_jobs, key=lambda x: int(x.get("match_score") or 0)) if t_jobs else None
        track_matrix.append({
            "track": t_key,
            "label": t_label,
            "total": t_total,
            "avg_score": round(sum(t_scores) / t_total, 1) if t_total else 0.0,
            "sa_count": t_sa,
            "delivered": t_deliv,
            "interview": t_inter,
            "top_match": f"{top_job['company_name']} ({top_job['match_score']}分)" if top_job else "暂无",
        })

    # 4. 评级与风险分布
    grade_dist = {"S": 0, "A": 0, "B": 0, "C": 0, "VETO": 0}
    risk_dist = {"safe": 0, "caution": 0, "veto": 0}
    for j in jobs:
        g = j.get("grade") or "B"
        grade_dist[g] = grade_dist.get(g, 0) + 1
        rk = j.get("risk_level") or "safe"
        risk_dist[rk] = risk_dist.get(rk, 0) + 1

    # 5. 近 N 日时间序列趋势
    today_dt = datetime.now().date()
    date_keys = [(today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    trend_map = {
        dk: {
            "date": dk,
            "short_date": dk[5:],
            "captured": 0,
            "cleaned": 0,
            "delivered": 0,
            "tokens": 0,
            "cost_cny": 0.0,
        }
        for dk in date_keys
    }

    for j in jobs:
        c_day = (j.get("created_at") or "")[:10]
        if c_day in trend_map:
            trend_map[c_day]["captured"] += 1
            if (j.get("risk_level") or "safe") != "veto":
                trend_map[c_day]["cleaned"] += 1
        d_day = (j.get("delivered_at") or "")[:10]
        if d_day in trend_map:
            trend_map[d_day]["delivered"] += 1

    # 6. Token 消耗分组统计
    by_action: List[Dict[str, Any]] = []
    by_model: List[Dict[str, Any]] = []
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT substr(created_at, 1, 10) AS d,
                   COALESCE(SUM(total_tokens), 0) AS tok,
                   COALESCE(SUM(estimated_cny), 0.0) AS cny
            FROM token_usage
            GROUP BY substr(created_at, 1, 10)
        """)
        for row in cur.fetchall():
            dk = row["d"]
            if dk in trend_map:
                trend_map[dk]["tokens"] = int(row["tok"])
                trend_map[dk]["cost_cny"] = round(float(row["cny"]), 4)

        cur.execute("""
            SELECT action,
                   COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(estimated_cny), 0.0) AS cost_cny
            FROM token_usage
            GROUP BY action
            ORDER BY total_tokens DESC
        """)
        for row in cur.fetchall():
            act = row["action"]
            by_action.append({
                "action": act,
                "label": ACTION_LABELS.get(act, act),
                "calls": int(row["calls"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "cost_cny": round(float(row["cost_cny"]), 4),
            })

        cur.execute("""
            SELECT model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(estimated_cny), 0.0) AS cost_cny
            FROM token_usage
            GROUP BY model
            ORDER BY total_tokens DESC
        """)
        for row in cur.fetchall():
            by_model.append({
                "model": row["model"],
                "calls": int(row["calls"]),
                "total_tokens": int(row["total_tokens"]),
                "cost_cny": round(float(row["cost_cny"]), 4),
            })

    avg_score_all = (
        round(sum(int(j.get("match_score") or 0) for j in jobs) / total, 1)
        if total else 0.0
    )
    pdf_count = sum(1 for j in jobs if j.get("tailored_pdf_path"))
    feishu_synced_count = sum(1 for j in jobs if j.get("feishu_record_id"))

    return {
        "kpis": {
            "total_jobs": total,
            "clean_jobs": len(passed_clean),
            "vetoed_jobs": total - len(passed_clean),
            "sa_grade_jobs": len(high_grade),
            "delivered_jobs": len(delivered_plus),
            "interview_jobs": len(interview_plus),
            "offer_jobs": len(offers),
            "avg_match_score": avg_score_all,
            "tailored_pdfs": pdf_count,
            "feishu_synced": feishu_synced_count,
            "delivery_to_interview_rate": round((len(interview_plus) / len(delivered_plus) * 100), 1) if delivered_plus else 0.0,
        },
        "funnel": funnel,
        "platform_roi": platform_roi,
        "track_matrix": track_matrix,
        "grade_distribution": grade_dist,
        "risk_distribution": risk_dist,
        "daily_trends": list(trend_map.values()),
        "tokens": {
            "summary": get_token_summary(db_path=db_path),
            "by_action": by_action,
            "by_model": by_model,
            "recent_logs": list_token_logs(limit=60, db_path=db_path),
        },
        "recent_crawl_runs": list_crawl_runs(limit=10, db_path=db_path),
    }
