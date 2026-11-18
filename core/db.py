import sqlite3
import json
import csv
import io
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

DB_FILE = Path(__file__).resolve().parent.parent / "data" / "hire_lens.db"

_NEW_JOBS_COLUMNS = [
    ("platform", "TEXT DEFAULT 'manual'"),
    ("language", "TEXT DEFAULT 'zh-CN'"),
    ("risk_level", "TEXT DEFAULT 'safe'"),
    ("veto_report", "TEXT DEFAULT '{}'"),
    ("dimension_scores", "TEXT DEFAULT '{}'"),
    ("ats_matrix", "TEXT DEFAULT '{}'"),
    ("profile_advantage", "TEXT DEFAULT ''"),
    ("cover_letter", "TEXT DEFAULT ''"),
    ("pii_audit", "TEXT DEFAULT '{}'"),
    ("interview_prep", "TEXT DEFAULT '{}'"),
    ("notes", "TEXT DEFAULT ''"),
    ("updated_at", "TEXT DEFAULT ''"),
    # v3 全链路升级字段
    ("tier2_score", "INTEGER DEFAULT 85"),
    ("tier2_report", "TEXT DEFAULT '{}'"),
    ("grade", "TEXT DEFAULT 'B'"),
    ("pipeline_stage", "TEXT DEFAULT 'evaluated'"),
    ("approval_status", "TEXT DEFAULT 'waiting_approval'"),
    ("tailored_resume_md", "TEXT DEFAULT ''"),
    ("tailored_pdf_path", "TEXT DEFAULT ''"),
    ("tailored_img_path", "TEXT DEFAULT ''"),
    ("feishu_record_id", "TEXT DEFAULT ''"),
    ("company_intel", "TEXT DEFAULT '{}'"),
    ("delivery_mode", "TEXT DEFAULT 'greeting_only'"),
    ("delivery_log", "TEXT DEFAULT ''"),
]


def compute_grade(match_score: int, risk_level: str = "safe") -> str:
    if risk_level == "veto":
        return "VETO"
    if match_score >= 88:
        return "S"
    if match_score >= 80:
        return "A"
    if match_score >= 70:
        return "B"
    return "C"


@contextmanager
def db_session(db_path: Optional[Path] = None):
    target = db_path or DB_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None):
    """幂等初始化并平滑升级本地 SQLite 数据表（100% 保留历史存量数据）"""
    with db_session(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_title TEXT NOT NULL,
                company_name TEXT NOT NULL,
                salary TEXT,
                city TEXT DEFAULT '深圳',
                hr_title TEXT,
                hr_active_status TEXT,
                job_url TEXT,
                jd_text TEXT,
                selected_track TEXT,
                match_score INTEGER DEFAULT 0,
                fit_reasons TEXT,
                greeting_draft TEXT,
                greeting_final TEXT,
                delivery_status TEXT DEFAULT 'pending_review',
                delivered_at TEXT,
                created_at TEXT NOT NULL
            );
        """)

        cur = conn.cursor()
        cur.execute("PRAGMA table_info(jobs)")
        existing_cols = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in _NEW_JOBS_COLUMNS:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col_name} {col_def};")

        # 为存量历史记录自动补齐空缺的 grade 字段
        conn.execute("""
            UPDATE jobs
            SET grade = CASE
                WHEN risk_level = 'veto' THEN 'VETO'
                WHEN match_score >= 88 THEN 'S'
                WHEN match_score >= 80 THEN 'A'
                WHEN match_score >= 70 THEN 'B'
                ELSE 'C'
            END
            WHERE grade IS NULL OR grade = '' OR (grade = 'B' AND match_score >= 80);
        """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_url
            ON jobs(job_url) WHERE job_url IS NOT NULL AND job_url != '';
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(delivery_status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_feishu_rec ON jobs(feishu_record_id);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                action TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                estimated_cny REAL DEFAULT 0.0,
                created_at TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platforms TEXT NOT NULL,
                keywords TEXT NOT NULL,
                city TEXT DEFAULT '深圳',
                mode TEXT DEFAULT 'simulate',
                total_found INTEGER DEFAULT 0,
                deduplicated_new INTEGER DEFAULT 0,
                tier1_passed INTEGER DEFAULT 0,
                tier2_passed INTEGER DEFAULT 0,
                tailored_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                error_msg TEXT DEFAULT '',
                started_at TEXT NOT NULL,
                finished_at TEXT DEFAULT ''
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER DEFAULT 0,
                level TEXT DEFAULT 'INFO',
                stage TEXT DEFAULT 'system',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS mock_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                company_name TEXT DEFAULT '',
                job_title TEXT DEFAULT '',
                selected_track TEXT DEFAULT 'track_ai_builder',
                status TEXT DEFAULT 'active',
                messages_json TEXT DEFAULT '[]',
                scorecard_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS feishu_sync_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                action TEXT NOT NULL,
                records_synced INTEGER DEFAULT 0,
                status TEXT DEFAULT 'ok',
                detail TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
        """)


def _deserialize_job(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for list_field in ("fit_reasons",):
        try:
            d[list_field] = json.loads(d.get(list_field) or "[]")
        except Exception:
            d[list_field] = []
    for dict_field in (
        "veto_report",
        "dimension_scores",
        "ats_matrix",
        "pii_audit",
        "interview_prep",
        "tier2_report",
        "company_intel",
    ):
        try:
            val = json.loads(d.get(dict_field) or "{}")
            d[dict_field] = val if isinstance(val, dict) else {}
        except Exception:
            d[dict_field] = {}
    if not d.get("grade"):
        d["grade"] = compute_grade(int(d.get("match_score") or 0), d.get("risk_level") or "safe")
    return d


def save_job(
    job_title: str,
    company_name: str,
    salary: str = "",
    city: str = "深圳",
    hr_title: str = "",
    hr_active_status: str = "今日活跃",
    job_url: str = "",
    jd_text: str = "",
    selected_track: str = "track_ai_builder",
    match_score: int = 0,
    fit_reasons: List[str] | None = None,
    greeting_draft: str = "",
    profile_advantage: str = "",
    platform: str = "manual",
    language: str = "zh-CN",
    risk_level: str = "safe",
    veto_report: Dict[str, Any] | None = None,
    dimension_scores: Dict[str, Any] | None = None,
    ats_matrix: Dict[str, Any] | None = None,
    cover_letter: str = "",
    pii_audit: Dict[str, Any] | None = None,
    tier2_score: int = 85,
    tier2_report: Dict[str, Any] | None = None,
    pipeline_stage: str = "evaluated",
    approval_status: str = "waiting_approval",
    db_path: Optional[Path] = None,
) -> int:
    """写入或更新岗位全维度数据（含打招呼语、BOSS 个人优势、自荐信、双层清洗评级）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reasons_json = json.dumps(fit_reasons or [], ensure_ascii=False)
    veto_json = json.dumps(veto_report or {}, ensure_ascii=False)
    dim_json = json.dumps(dimension_scores or {}, ensure_ascii=False)
    ats_json = json.dumps(ats_matrix or {}, ensure_ascii=False)
    pii_json = json.dumps(pii_audit or {}, ensure_ascii=False)
    t2_json = json.dumps(tier2_report or {}, ensure_ascii=False)
    clean_url = (job_url or "").strip() or None
    grade = compute_grade(match_score, risk_level)

    with db_session(db_path) as conn:
        cur = conn.cursor()
        if clean_url:
            cur.execute(
                "SELECT id FROM jobs WHERE job_url = ? OR (company_name = ? AND job_title = ?)",
                (clean_url, company_name, job_title),
            )
        else:
            cur.execute(
                "SELECT id FROM jobs WHERE company_name = ? AND job_title = ? AND company_name != '未填公司'",
                (company_name, job_title),
            )
        row = cur.fetchone()
        if row:
            job_id = row["id"]
            cur.execute("""
                UPDATE jobs SET
                    salary = ?, city = ?, hr_title = ?, hr_active_status = ?,
                    jd_text = ?, selected_track = ?, match_score = ?,
                    fit_reasons = ?, greeting_draft = ?, greeting_final = ?,
                    profile_advantage = ?, platform = ?, language = ?, risk_level = ?,
                    veto_report = ?, dimension_scores = ?, ats_matrix = ?, cover_letter = ?,
                    pii_audit = ?, tier2_score = ?, tier2_report = ?, grade = ?,
                    pipeline_stage = ?, updated_at = ?
                WHERE id = ?
            """, (
                salary, city, hr_title, hr_active_status,
                jd_text, selected_track, match_score,
                reasons_json, greeting_draft, greeting_draft,
                profile_advantage, platform, language, risk_level,
                veto_json, dim_json, ats_json, cover_letter,
                pii_json, tier2_score, t2_json, grade,
                pipeline_stage, now, job_id,
            ))
        else:
            init_delivery_status = "rejected" if risk_level == "veto" else "pending_review"
            try:
                cur.execute("""
                    INSERT INTO jobs (
                        job_title, company_name, salary, city, hr_title, hr_active_status,
                        job_url, jd_text, selected_track, match_score, fit_reasons,
                        greeting_draft, greeting_final, profile_advantage, delivery_status, created_at,
                        platform, language, risk_level, veto_report, dimension_scores,
                        ats_matrix, cover_letter, pii_audit, tier2_score, tier2_report,
                        grade, pipeline_stage, approval_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_title, company_name, salary, city, hr_title, hr_active_status,
                    clean_url, jd_text, selected_track, match_score, reasons_json,
                    greeting_draft, greeting_draft, profile_advantage, init_delivery_status, now,
                    platform, language, risk_level, veto_json, dim_json,
                    ats_json, cover_letter, pii_json, tier2_score, t2_json,
                    grade, pipeline_stage, approval_status, now,
                ))
                job_id = cur.lastrowid
            except sqlite3.IntegrityError:
                cur.execute("SELECT id FROM jobs WHERE company_name = ? AND job_title = ?", (company_name, job_title))
                fallback_row = cur.fetchone()
                job_id = fallback_row["id"] if fallback_row else 1
        return int(job_id)


def update_job_pipeline_fields(job_id: int, db_path: Optional[Path] = None, **kwargs):
    """通用安全字段更新（供流水线、定制简历渲染、飞书双向同步、背调模块调用）"""
    allowed = {
        "tier2_score", "tier2_report", "grade", "pipeline_stage", "approval_status",
        "tailored_resume_md", "tailored_pdf_path", "tailored_img_path",
        "feishu_record_id", "company_intel", "delivery_mode", "delivery_log",
        "delivery_status", "delivered_at", "greeting_final", "profile_advantage",
        "notes", "risk_level", "match_score",
    }
    sets = []
    vals: List[Any] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for k, v in kwargs.items():
        if k not in allowed:
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        vals.append(v)

    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(job_id)

    with db_session(db_path) as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", tuple(vals))


def list_jobs(
    status: Optional[str] = None,
    track: Optional[str] = None,
    search: Optional[str] = None,
    platform: Optional[str] = None,
    grade: Optional[str] = None,
    approval_status: Optional[str] = None,
    limit: int = 200,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        clauses = []
        params: List[Any] = []
        if status:
            clauses.append("delivery_status = ?")
            params.append(status)
        if track:
            clauses.append("selected_track = ?")
            params.append(track)
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if grade:
            clauses.append("grade = ?")
            params.append(grade)
        if approval_status:
            clauses.append("approval_status = ?")
            params.append(approval_status)
        if search:
            clauses.append("(job_title LIKE ? OR company_name LIKE ? OR jd_text LIKE ? OR notes LIKE ?)")
            kw = f"%{search}%"
            params.extend([kw, kw, kw, kw])

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(f"SELECT * FROM jobs {where_sql} ORDER BY id DESC LIMIT ?", (*params, limit))
        return [_deserialize_job(r) for r in cur.fetchall()]


def get_job(job_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return _deserialize_job(row) if row else None


def get_job_by_feishu_record(record_id: str, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    if not record_id:
        return None
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs WHERE feishu_record_id = ?", (record_id,))
        row = cur.fetchone()
        return _deserialize_job(row) if row else None


def update_job_greeting(
    job_id: int,
    greeting: str,
    cover_letter: Optional[str] = None,
    profile_advantage: Optional[str] = None,
    db_path: Optional[Path] = None,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        fields = ["greeting_final = ?", "updated_at = ?"]
        vals: List[Any] = [greeting, now]
        if cover_letter is not None:
            fields.append("cover_letter = ?")
            vals.append(cover_letter)
        if profile_advantage is not None:
            fields.append("profile_advantage = ?")
            vals.append(profile_advantage)
        vals.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", tuple(vals))


def update_job_status(job_id: int, status: str, db_path: Optional[Path] = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        if status == "delivered":
            conn.execute(
                "UPDATE jobs SET delivery_status = ?, approval_status = 'approved', pipeline_stage = 'delivered', delivered_at = ?, updated_at = ? WHERE id = ?",
                (status, now, now, job_id),
            )
        elif status == "rejected":
            conn.execute(
                "UPDATE jobs SET delivery_status = ?, approval_status = 'rejected', pipeline_stage = 'skipped', updated_at = ? WHERE id = ?",
                (status, now, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET delivery_status = ?, updated_at = ? WHERE id = ?",
                (status, now, job_id),
            )


def update_job_notes(job_id: int, notes: str, db_path: Optional[Path] = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        conn.execute("UPDATE jobs SET notes = ?, updated_at = ? WHERE id = ?", (notes, now, job_id))


def save_interview_prep(job_id: int, prep_data: Dict[str, Any], db_path: Optional[Path] = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prep_json = json.dumps(prep_data, ensure_ascii=False)
    with db_session(db_path) as conn:
        conn.execute("UPDATE jobs SET interview_prep = ?, updated_at = ? WHERE id = ?", (prep_json, now, job_id))


def delete_job(job_id: int, db_path: Optional[Path] = None):
    with db_session(db_path) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def record_token_usage(
    action: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    job_id: Optional[int] = None,
    prompt_price_per_m: float = 2.0,
    completion_price_per_m: float = 8.0,
    db_path: Optional[Path] = None,
):
    total = int(prompt_tokens) + int(completion_tokens)
    est_cny = round(
        (prompt_tokens * float(prompt_price_per_m) + completion_tokens * float(completion_price_per_m)) / 1_000_000,
        5,
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        conn.execute("""
            INSERT INTO token_usage (job_id, action, model, prompt_tokens, completion_tokens, total_tokens, estimated_cny, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, action, model, prompt_tokens, completion_tokens, total, est_cny, now))


def get_token_summary(db_path: Optional[Path] = None) -> Dict[str, Any]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS p_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS c_tokens,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(estimated_cny), 0.0) AS cost_cny
            FROM token_usage
        """)
        row = cur.fetchone()
        return {
            "total_calls": int(row["calls"]),
            "prompt_tokens": int(row["p_tokens"]),
            "completion_tokens": int(row["c_tokens"]),
            "total_tokens": int(row["tokens"]),
            "total_cost_cny": round(float(row["cost_cny"]), 4),
        }


def list_token_logs(
    limit: int = 80,
    offset: int = 0,
    action: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        if action:
            cur.execute(
                """
                SELECT t.*, j.job_title, j.company_name
                FROM token_usage t
                LEFT JOIN jobs j ON t.job_id = j.id
                WHERE t.action = ?
                ORDER BY t.id DESC LIMIT ? OFFSET ?
                """,
                (action, limit, offset),
            )
        else:
            cur.execute(
                """
                SELECT t.*, j.job_title, j.company_name
                FROM token_usage t
                LEFT JOIN jobs j ON t.job_id = j.id
                ORDER BY t.id DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        return [dict(r) for r in cur.fetchall()]


def create_crawl_run(
    platforms: List[str],
    keywords: str,
    city: str,
    mode: str = "simulate",
    db_path: Optional[Path] = None,
) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO crawl_runs (platforms, keywords, city, mode, status, started_at)
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (",".join(platforms), keywords, city, mode, now),
        )
        return int(cur.lastrowid)


def finish_crawl_run(
    run_id: int,
    total_found: int = 0,
    deduplicated_new: int = 0,
    tier1_passed: int = 0,
    tier2_passed: int = 0,
    tailored_count: int = 0,
    status: str = "completed",
    error_msg: str = "",
    db_path: Optional[Path] = None,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE crawl_runs
            SET total_found = ?, deduplicated_new = ?, tier1_passed = ?, tier2_passed = ?,
                tailored_count = ?, status = ?, error_msg = ?, finished_at = ?
            WHERE id = ?
            """,
            (
                total_found, deduplicated_new, tier1_passed, tier2_passed,
                tailored_count, status, error_msg, now, run_id,
            ),
        )


def list_crawl_runs(limit: int = 25, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def append_pipeline_log(
    message: str,
    stage: str = "pipeline",
    level: str = "INFO",
    run_id: int = 0,
    db_path: Optional[Path] = None,
) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO pipeline_logs (run_id, level, stage, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, level, stage, message, now),
        )
        return int(cur.lastrowid)


def list_pipeline_logs(since_id: int = 0, limit: int = 120, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        if since_id > 0:
            cur.execute(
                "SELECT * FROM pipeline_logs WHERE id > ? ORDER BY id ASC LIMIT ?",
                (since_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM pipeline_logs ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows


def create_mock_session(
    job_id: Optional[int],
    company_name: str,
    job_title: str,
    selected_track: str,
    initial_messages: List[Dict[str, Any]],
    db_path: Optional[Path] = None,
) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO mock_sessions (job_id, company_name, job_title, selected_track, status, messages_json, scorecard_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'active', ?, '{}', ?, ?)
            """,
            (job_id, company_name, job_title, selected_track, json.dumps(initial_messages, ensure_ascii=False), now, now),
        )
        return int(cur.lastrowid)


def update_mock_session(
    session_id: int,
    messages: List[Dict[str, Any]],
    status: str = "active",
    scorecard: Optional[Dict[str, Any]] = None,
    db_path: Optional[Path] = None,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        if scorecard is not None:
            conn.execute(
                "UPDATE mock_sessions SET messages_json = ?, status = ?, scorecard_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(messages, ensure_ascii=False), status, json.dumps(scorecard, ensure_ascii=False), now, session_id),
            )
        else:
            conn.execute(
                "UPDATE mock_sessions SET messages_json = ?, status = ?, updated_at = ? WHERE id = ?",
                (json.dumps(messages, ensure_ascii=False), status, now, session_id),
            )


def get_mock_session(session_id: int, db_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM mock_sessions WHERE id = ?", (session_id,))
        row = cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["messages"] = json.loads(d.get("messages_json") or "[]")
        d["scorecard"] = json.loads(d.get("scorecard_json") or "{}")
        return d


def list_mock_sessions(limit: int = 20, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM mock_sessions ORDER BY id DESC LIMIT ?", (limit,))
        res = []
        for r in cur.fetchall():
            d = dict(r)
            d["messages"] = json.loads(d.get("messages_json") or "[]")
            d["scorecard"] = json.loads(d.get("scorecard_json") or "{}")
            res.append(d)
        return res


def record_feishu_sync(
    direction: str,
    action: str,
    records_synced: int,
    status: str = "ok",
    detail: str = "",
    db_path: Optional[Path] = None,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO feishu_sync_logs (direction, action, records_synced, status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (direction, action, records_synced, status, detail, now),
        )


def list_feishu_sync_logs(limit: int = 25, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    with db_session(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM feishu_sync_logs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def export_jobs_csv(db_path: Optional[Path] = None) -> str:
    jobs = list_jobs(limit=1000, db_path=db_path)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ID", "职位名称", "公司名称", "渠道", "薪资", "城市",
        "选定赛道", "评级", "匹配分", "风险等级", "审批状态", "当前状态",
        "打招呼破冰语", "BOSS个人优势", "飞书记录ID", "跟进备注", "录入时间"
    ])
    for j in jobs:
        writer.writerow([
            j.get("id"),
            j.get("job_title"),
            j.get("company_name"),
            j.get("platform"),
            j.get("salary"),
            j.get("city"),
            j.get("selected_track"),
            j.get("grade"),
            j.get("match_score"),
            j.get("risk_level"),
            j.get("approval_status"),
            j.get("delivery_status"),
            j.get("greeting_final") or j.get("greeting_draft"),
            j.get("profile_advantage") or "",
            j.get("feishu_record_id") or "",
            j.get("notes") or "",
            j.get("created_at"),
        ])
    return buf.getvalue()
