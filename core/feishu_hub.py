import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .analytics import PLATFORM_LABELS, TRACK_LABELS, build_full_analytics
from .db import (
    get_job,
    get_job_by_feishu_record,
    list_jobs,
    record_feishu_sync,
    record_token_usage,
    update_job_greeting,
    update_job_notes,
    update_job_pipeline_fields,
    update_job_status,
)
from .pii_masker import PIIMasker
from .llm_client import call_llm, is_llm_available

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

APPROVAL_TO_CN = {
    "waiting_approval": "待审批",
    "approved": "批准投递",
    "auto_pass": "批准投递",
    "rejected": "跳过归档",
}
CN_TO_APPROVAL = {
    "待审批": "waiting_approval",
    "批准投递": "approved",
    "跳过归档": "rejected",
}


def default_http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    req_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


class FeishuHub:
    """
    HireLens 飞书全生态协同中枢：
    1. 防在职泄密租户体检：主动查询飞书租户名称并强制提醒严禁接入在职雇主企业飞书；
    2. 零手工建表（Auto-Schema）：输入 AppID + Secret 即可一键自动创建多维表格与标准字段；
    3. 全双工同步 + ChatOps 机器人：支持富文本交互战报卡片、手机端多维表格审批回写本地 SQLite、以及自然语言指令交互。
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        http_fn: Callable[..., Dict[str, Any]] | None = None,
        db_path: Optional[Path] = None,
    ):
        self.settings = settings
        self._http = http_fn or default_http_json
        self.db_path = db_path
        self._token_cache: tuple[str, float] = ("", 0.0)
        self.masker = PIIMasker()

    # ------------------------------------------------------------------
    # 基础鉴权与在职防泄密体检
    # ------------------------------------------------------------------
    def get_tenant_access_token(self) -> str:
        app_id = (self.settings.get("feishu_app_id") or "").strip()
        app_secret = (self.settings.get("feishu_app_secret") or "").strip()
        if not app_id or not app_secret:
            raise ValueError("未配置 FEISHU_APP_ID 或 FEISHU_APP_SECRET")

        now = time.time()
        if self._token_cache[0] and self._token_cache[1] > now + 60:
            return self._token_cache[0]

        res = self._http(
            "POST",
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
        )
        if res.get("code", -1) != 0:
            raise RuntimeError(f"飞书鉴权失败: {res.get('msg', res)}")
        tok = res["tenant_access_token"]
        expire = int(res.get("expire", 7200))
        self._token_cache = (tok, now + expire)
        return tok

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.get_tenant_access_token()}"}

    def check_connectivity_and_tenant(self) -> Dict[str, Any]:
        """一键自检 Webhook 与飞书开放平台应用连通性 + 租户归属安全审查"""
        report: Dict[str, Any] = {
            "webhook_configured": bool(self.settings.get("feishu_webhook_url")),
            "app_configured": bool(self.settings.get("feishu_app_id") and self.settings.get("feishu_app_secret")),
            "personal_ack": bool(self.settings.get("feishu_personal_tenant_ack")),
            "tenant_name": "",
            "safety_warning": (
                "⚠️ 安全红线提醒：严禁使用【当前在职雇主公司】的企业飞书创建应用或多维表格（企业超管后台可审计云文档与API日志）！"
                "请务必使用个人手机号免费新建的独立飞书个人/团队空间。"
            ),
            "app_auth_ok": False,
        }
        if report["app_configured"]:
            try:
                headers = self._auth_headers()
                report["app_auth_ok"] = True
                try:
                    t_res = self._http("GET", f"{FEISHU_API_BASE}/tenant/v2/query", None, headers)
                    if t_res.get("code") == 0:
                        report["tenant_name"] = (t_res.get("data", {}).get("tenant", {}) or {}).get("name", "")
                except Exception:
                    report["tenant_name"] = "已认证（未开启 tenant:tenant:readonly 权限，不影响多维表格使用）"
            except Exception as e:
                report["auth_error"] = str(e)
        return report

    # ------------------------------------------------------------------
    # Pillar 1: 富文本交互卡片推送（Webhook 或机器人会话双通道）
    # ------------------------------------------------------------------
    def build_job_card_payload(self, job: Dict[str, Any]) -> Dict[str, Any]:
        grade = job.get("grade") or "B"
        tpl_color = "turquoise" if grade in ("S", "A") else ("orange" if grade == "B" else "red")
        greet = job.get("greeting_final") or job.get("greeting_draft") or ""
        exposed_16 = greet[:17] + ("..." if len(greet) > 17 else "")
        adv = job.get("profile_advantage") or "（待生成）"
        plat_name = PLATFORM_LABELS.get(job.get("platform") or "manual", job.get("platform") or "手动")
        track_name = TRACK_LABELS.get(job.get("selected_track") or "", job.get("selected_track") or "")

        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔭 打开本地聘镜工作台"},
                "type": "primary",
                "url": f"http://127.0.0.1:8765/?view=studio&job_id={job.get('id', '')}",
            }
        ]
        if job.get("job_url") and str(job["job_url"]).startswith("http"):
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔗 查看原招聘页"},
                "type": "default",
                "url": job["job_url"],
            })

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": tpl_color,
                    "title": {
                        "tag": "plain_text",
                        "content": f"[{grade}级 · {job.get('match_score', 0)}分] {job.get('job_title', '')} · {job.get('company_name', '')}",
                    },
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**渠道**：{plat_name} ｜ **城市**：{job.get('city', '深圳')} ｜ **薪资**：{job.get('salary') or '面议'}\n"
                                f"**匹配母本**：{track_name} ｜ **排雷状态**：`{job.get('risk_level', 'safe')}`\n"
                                f"**📱 HR未点开前17字预览**：`{exposed_16}`"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**💬 重点前置破冰语**：\n{greet}\n\n**🪪 BOSS牛人推荐页优势**：\n{adv}",
                        },
                    },
                    {"tag": "action", "actions": actions},
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": f"HireLens 聘镜 · 本地 #ID={job.get('id')} · 回复机器人「批准 #{job.get('id')}」或「跳过 #{job.get('id')}」可直接指令操作",
                            }
                        ],
                    },
                ],
            },
        }

    def build_daily_report_card(self) -> Dict[str, Any]:
        stats = build_full_analytics(days=7, db_path=self.db_path)
        k = stats["kpis"]
        tok = stats["tokens"]["summary"]
        funnel_lines = "\n".join(
            f"- {st['label']}：**{st['count']}** 岗（总转化 {st['rate_from_top']}%）"
            for st in stats["funnel"][:6]
        )
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "blue",
                    "title": {
                        "tag": "plain_text",
                        "content": f"📊 HireLens 聘镜 · 求职全链路战报 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                    },
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**核心指标总览**\n"
                                f"总入库：**{k['total_jobs']}** ｜ 排雷拦截：**{k['vetoed_jobs']}** ｜ S/A高分精选：**{k['sa_grade_jobs']}**\n"
                                f"已投递：**{k['delivered_jobs']}** ｜ 约面推进：**{k['interview_jobs']}** ｜ Offer：**{k['offer_jobs']}**\n"
                                f"定制PDF简历：**{k['tailored_pdfs']}** 份 ｜ 算力累计：**{tok['total_tokens']}** Tokens (¥{tok['total_cost_cny']})"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": f"**📈 全链路转化漏斗**\n{funnel_lines}"},
                    },
                ],
            },
        }

    def push_card(self, card_payload: Dict[str, Any]) -> Dict[str, Any]:
        """通过 Webhook 或自建应用群聊/私聊发送飞书富文本卡片"""
        webhook = (self.settings.get("feishu_webhook_url") or "").strip()
        chat_id = (self.settings.get("feishu_chat_id") or "").strip()

        if webhook:
            res = self._http("POST", webhook, card_payload)
            ok = res.get("code", 0) == 0 and res.get("StatusCode", 0) == 0
            record_feishu_sync("push", "webhook_card", 1 if ok else 0, "ok" if ok else "error", str(res)[:160], self.db_path)
            return {"success": ok, "channel": "webhook", "response": res}

        if chat_id and self.settings.get("feishu_app_id") and self.settings.get("feishu_app_secret"):
            headers = self._auth_headers()
            msg_body = {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": json.dumps(card_payload["card"], ensure_ascii=False),
            }
            id_type = "chat_id" if chat_id.startswith("oc_") else "open_id"
            res = self._http(
                "POST",
                f"{FEISHU_API_BASE}/im/v1/messages?receive_id_type={id_type}",
                msg_body,
                headers,
            )
            ok = res.get("code", -1) == 0
            record_feishu_sync("push", "im_card", 1 if ok else 0, "ok" if ok else "error", str(res)[:160], self.db_path)
            return {"success": ok, "channel": "app_im", "response": res}

        return {
            "success": False,
            "channel": "none",
            "message": "尚未配置 FEISHU_WEBHOOK_URL 或 FEISHU_CHAT_ID，已在本地生成卡片预览结构",
            "preview": card_payload,
        }

    # ------------------------------------------------------------------
    # Pillar 2: 多维表格 (Bitable) 一键自动建表与双向同步
    # ------------------------------------------------------------------
    JOB_TABLE_FIELDS = [
        {"field_name": "本地ID", "type": 2},
        {"field_name": "职位名称", "type": 1},
        {"field_name": "公司名称", "type": 1},
        {"field_name": "来源渠道", "type": 1},
        {"field_name": "薪资", "type": 1},
        {"field_name": "城市", "type": 1},
        {"field_name": "匹配赛道", "type": 1},
        {
            "field_name": "评级",
            "type": 3,
            "property": {"options": [{"name": "S"}, {"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "VETO"}]},
        },
        {"field_name": "匹配分", "type": 2},
        {
            "field_name": "风险等级",
            "type": 3,
            "property": {"options": [{"name": "safe"}, {"name": "caution"}, {"name": "veto"}]},
        },
        {
            "field_name": "手机审批指令",
            "type": 3,
            "property": {"options": [{"name": "待审批"}, {"name": "批准投递"}, {"name": "跳过归档"}]},
        },
        {
            "field_name": "求职进度",
            "type": 3,
            "property": {
                "options": [
                    {"name": "pending_review"},
                    {"name": "delivered"},
                    {"name": "interviewing"},
                    {"name": "offer"},
                    {"name": "rejected"},
                    {"name": "failed"},
                ]
            },
        },
        {"field_name": "重点前置破冰语", "type": 1},
        {"field_name": "BOSS个人优势", "type": 1},
        {"field_name": "岗位链接", "type": 1},
        {"field_name": "跟进备注", "type": 1},
    ]

    def auto_provision_bitable(self) -> Dict[str, Any]:
        """
        飞书多维表格一键自动建表与列对齐引擎：
        若未填 app_token 则自动创建新多维表格文档；若已填则自动扫描并补齐全部标准表与列。
        """
        headers = self._auth_headers()
        app_token = (self.settings.get("feishu_app_token") or "").strip()
        app_url = ""

        if not app_token:
            create_res = self._http(
                "POST",
                f"{FEISHU_API_BASE}/bitable/v1/apps",
                {"name": "HireLens 聘镜 · AI求职全链路中台"},
                headers,
            )
            if create_res.get("code", -1) != 0:
                raise RuntimeError(f"自动创建多维表格失败: {create_res.get('msg', create_res)}")
            app_info = create_res.get("data", {}).get("app", {})
            app_token = app_info.get("app_token", "")
            app_url = app_info.get("url", "")

        # 查询已有数据表
        tables_res = self._http(
            "GET",
            f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables?page_size=50",
            None,
            headers,
        )
        items = (tables_res.get("data") or {}).get("items") or []
        name_to_tid = {t.get("name"): t.get("table_id") for t in items}

        jobs_tid = self.settings.get("feishu_jobs_table_id") or name_to_tid.get("岗位全链路看板") or ""
        if not jobs_tid:
            t_create = self._http(
                "POST",
                f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables",
                {
                    "table": {
                        "name": "岗位全链路看板",
                        "default_view_name": "全部岗位视图",
                        "fields": self.JOB_TABLE_FIELDS,
                    }
                },
                headers,
            )
            if t_create.get("code", -1) != 0:
                raise RuntimeError(f"创建岗位看板子表失败: {t_create.get('msg', t_create)}")
            jobs_tid = (t_create.get("data") or {}).get("table_id", "")
        else:
            # 增量校准缺失字段
            f_res = self._http(
                "GET",
                f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{jobs_tid}/fields?page_size=100",
                None,
                headers,
            )
            existing_fnames = {f.get("field_name") for f in ((f_res.get("data") or {}).get("items") or [])}
            for fdef in self.JOB_TABLE_FIELDS:
                if fdef["field_name"] not in existing_fnames:
                    self._http(
                        "POST",
                        f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{jobs_tid}/fields",
                        fdef,
                        headers,
                    )

        resumes_tid = self.settings.get("feishu_resumes_table_id") or name_to_tid.get("四赛道母本资产库") or ""
        if not resumes_tid:
            r_create = self._http(
                "POST",
                f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables",
                {
                    "table": {
                        "name": "四赛道母本资产库",
                        "default_view_name": "母本列表",
                        "fields": [
                            {"field_name": "赛道代码", "type": 1},
                            {"field_name": "赛道名称", "type": 1},
                            {"field_name": "更新时间", "type": 1},
                            {"field_name": "母本正文摘要", "type": 1},
                        ],
                    }
                },
                headers,
            )
            if r_create.get("code", -1) == 0:
                resumes_tid = (r_create.get("data") or {}).get("table_id", "")

        record_feishu_sync("provision", "auto_init_bitable", 2, "ok", f"app_token={app_token}", self.db_path)
        return {
            "success": True,
            "feishu_app_token": app_token,
            "feishu_jobs_table_id": jobs_tid,
            "feishu_resumes_table_id": resumes_tid,
            "url": app_url or f"https://feishu.cn/base/{app_token}",
        }

    def _job_to_bitable_fields(self, job: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "本地ID": int(job.get("id") or 0),
            "职位名称": job.get("job_title") or "",
            "公司名称": job.get("company_name") or "",
            "来源渠道": PLATFORM_LABELS.get(job.get("platform") or "manual", job.get("platform") or "manual"),
            "薪资": job.get("salary") or "",
            "城市": job.get("city") or "深圳",
            "匹配赛道": TRACK_LABELS.get(job.get("selected_track") or "", job.get("selected_track") or ""),
            "评级": job.get("grade") or "B",
            "匹配分": int(job.get("match_score") or 0),
            "风险等级": job.get("risk_level") or "safe",
            "手机审批指令": APPROVAL_TO_CN.get(job.get("approval_status") or "waiting_approval", "待审批"),
            "求职进度": job.get("delivery_status") or "pending_review",
            "重点前置破冰语": job.get("greeting_final") or job.get("greeting_draft") or "",
            "BOSS个人优势": job.get("profile_advantage") or "",
            "岗位链接": job.get("job_url") or "",
            "跟进备注": job.get("notes") or "",
        }

    def sync_local_to_bitable(self, limit: int = 100) -> Dict[str, Any]:
        """本地 SQLite -> 飞书多维表格幂等推送（有 record_id 则更新，无则新建并回填 record_id，零重复行）"""
        app_token = (self.settings.get("feishu_app_token") or "").strip()
        table_id = (self.settings.get("feishu_jobs_table_id") or "").strip()
        if not app_token or not table_id:
            raise ValueError("尚未配置多维表格 app_token 或 jobs_table_id，请先点击「一键自动建表」")

        headers = self._auth_headers()
        jobs = list_jobs(limit=limit, db_path=self.db_path)
        created_cnt, updated_cnt = 0, 0

        for job in reversed(jobs):
            fields = self._job_to_bitable_fields(job)
            rec_id = (job.get("feishu_record_id") or "").strip()
            if rec_id:
                res = self._http(
                    "PUT",
                    f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{rec_id}",
                    {"fields": fields},
                    headers,
                )
                if res.get("code", -1) == 0:
                    updated_cnt += 1
                    continue

            res = self._http(
                "POST",
                f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                {"fields": fields},
                headers,
            )
            if res.get("code", -1) == 0:
                new_rec_id = ((res.get("data") or {}).get("record") or {}).get("record_id", "")
                if new_rec_id:
                    update_job_pipeline_fields(job["id"], db_path=self.db_path, feishu_record_id=new_rec_id)
                created_cnt += 1

        total_synced = created_cnt + updated_cnt
        record_feishu_sync(
            "push",
            "bitable_upsert",
            total_synced,
            "ok",
            f"新增 {created_cnt} 条, 更新 {updated_cnt} 条",
            self.db_path,
        )
        return {"success": True, "created": created_cnt, "updated": updated_cnt, "total": total_synced}

    def sync_bitable_to_local(self) -> Dict[str, Any]:
        """飞书多维表格 -> 本地 SQLite 反向拉取同步（把手机端修改的审批放行指令、进度、破冰语、备注同步回本地）"""
        app_token = (self.settings.get("feishu_app_token") or "").strip()
        table_id = (self.settings.get("feishu_jobs_table_id") or "").strip()
        if not app_token or not table_id:
            raise ValueError("尚未配置多维表格 app_token 或 jobs_table_id")

        headers = self._auth_headers()
        res = self._http(
            "GET",
            f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=200",
            None,
            headers,
        )
        if res.get("code", -1) != 0:
            raise RuntimeError(f"拉取飞书多维表格失败: {res.get('msg', res)}")

        items = (res.get("data") or {}).get("items") or []
        pulled_updates = 0
        approved_ids: List[int] = []

        for item in items:
            rec_id = item.get("record_id", "")
            f = item.get("fields") or {}
            local_id = f.get("本地ID")
            job = None
            if local_id:
                try:
                    job = get_job(int(float(local_id)), db_path=self.db_path)
                except Exception:
                    pass
            if not job and rec_id:
                job = get_job_by_feishu_record(rec_id, db_path=self.db_path)
            if not job:
                continue

            jid = job["id"]
            updates: Dict[str, Any] = {}
            if rec_id and job.get("feishu_record_id") != rec_id:
                updates["feishu_record_id"] = rec_id

            cn_cmd = str(f.get("手机审批指令") or "").strip()
            if cn_cmd in CN_TO_APPROVAL:
                mapped_app = CN_TO_APPROVAL[cn_cmd]
                if mapped_app != job.get("approval_status"):
                    updates["approval_status"] = mapped_app
                    if mapped_app == "approved":
                        approved_ids.append(jid)
                    elif mapped_app == "rejected":
                        updates["delivery_status"] = "rejected"

            remote_status = str(f.get("求职进度") or "").strip()
            allowed_st = {"pending_review", "delivered", "interviewing", "offer", "rejected", "failed"}
            if remote_status in allowed_st and remote_status != job.get("delivery_status"):
                updates.setdefault("delivery_status", remote_status)

            remote_greet = str(f.get("重点前置破冰语") or "").strip()
            if remote_greet and remote_greet != (job.get("greeting_final") or job.get("greeting_draft") or ""):
                updates["greeting_final"] = remote_greet

            remote_adv = str(f.get("BOSS个人优势") or "").strip()
            if remote_adv and remote_adv != (job.get("profile_advantage") or ""):
                updates["profile_advantage"] = remote_adv

            remote_notes = str(f.get("跟进备注") or "").strip()
            if remote_notes != (job.get("notes") or ""):
                updates["notes"] = remote_notes

            if updates:
                update_job_pipeline_fields(jid, db_path=self.db_path, **updates)
                pulled_updates += 1

        record_feishu_sync(
            "pull",
            "bitable_pull_sync",
            pulled_updates,
            "ok",
            f"回写更新 {pulled_updates} 条，手机新批准 {len(approved_ids)} 条",
            self.db_path,
        )
        return {
            "success": True,
            "scanned": len(items),
            "updated_local": pulled_updates,
            "newly_approved_ids": approved_ids,
        }

    # ------------------------------------------------------------------
    # Pillar 3: 飞书机器人自然语言指令引擎 (ChatOps)
    # ------------------------------------------------------------------
    def handle_bot_command(
        self,
        text: str,
        trigger_cruise_fn: Optional[Callable[[str, str], str]] = None,
        trigger_deliver_fn: Optional[Callable[[int], Dict[str, Any]]] = None,
    ) -> str:
        cmd = (text or "").strip()
        if not cmd:
            return "请输入指令（发送「帮助」查看全部飞书远控指令）。"

        if cmd.lower() in ("帮助", "help", "?", "？", "/help"):
            return (
                "🤖 **HireLens 聘镜 · 飞书机器人指令菜单**\n"
                "1. `状态` — 查看当前本地岗位漏斗、今日投递额度与算力账单\n"
                "2. `待批` — 列出评分最高的待审批岗位与前 16 字破冰语\n"
                "3. `批准 #12` — 批准 #12 号岗位并触发安全投递\n"
                "4. `跳过 #12` — 归档跳过 #12 号岗位\n"
                "5. `日报` — 生成全链路转化率与多平台 ROI 战报\n"
                "6. `巡航 深圳 AI产品经理` — 远程启动四平台巡航采集与双层排雷\n"
                "7. 直接输入任何求职复盘问题（如：`目前哪个渠道平均匹配分最高？`），由本地脱敏 Agent 即时解答"
            )

        if cmd.lower() in ("状态", "status", "/status"):
            st = build_full_analytics(days=7, db_path=self.db_path)
            k = st["kpis"]
            tok = st["tokens"]["summary"]
            return (
                f"📡 **HireLens 实时状态**\n"
                f"• 总岗位池：{k['total_jobs']} 条（有效通过排雷 {k['clean_jobs']} 条，S/A高分 {k['sa_grade_jobs']} 条）\n"
                f"• 投递与约面：已投递 {k['delivered_jobs']} ｜ 约面 {k['interview_jobs']} ｜ Offer {k['offer_jobs']}\n"
                f"• 已生成定制 PDF：{k['tailored_pdfs']} 份 ｜ 飞书已同步：{k['feishu_synced']} 条\n"
                f"• Token 累计：{tok['total_tokens']} ({tok['total_calls']} 次调用，约 ¥{tok['total_cost_cny']})"
            )

        if cmd.lower() in ("待批", "列表", "pending", "/pending"):
            jobs = [
                j for j in list_jobs(status="pending_review", limit=50, db_path=self.db_path)
                if j.get("risk_level") != "veto"
            ]
            jobs.sort(key=lambda x: int(x.get("match_score") or 0), reverse=True)
            if not jobs:
                return "✅ 当前没有待审批的岗位，可发送 `巡航 深圳 AI产品` 采集新岗位。"
            lines = ["📋 **待审批高匹配岗位 Top 列表**："]
            for j in jobs[:6]:
                g_snip = (j.get("greeting_final") or j.get("greeting_draft") or "")[:20]
                lines.append(
                    f"• **#{j['id']}** [{j.get('grade','B')}·{j['match_score']}分] {j['company_name']} - {j['job_title']} ({j.get('salary') or '面议'})\n"
                    f"  首屏破冰：`{g_snip}...`"
                )
            lines.append("\n💡 回复 `批准 #ID` 放行投递，或 `跳过 #ID` 归档。")
            return "\n".join(lines)

        if cmd.lower() in ("日报", "战报", "report", "/report"):
            st = build_full_analytics(days=14, db_path=self.db_path)
            plat_str = "；".join(
                f"{p['label']} {p['total']}条(均分{p['avg_score']})" for p in st["platform_roi"][:4]
            ) or "暂无渠道记录"
            funnel_str = " → ".join(f"{f['label'][3:]}({f['count']})" for f in st["funnel"][:6])
            return (
                f"📊 **HireLens 14日全景求职战报**\n"
                f"• 漏斗链路：{funnel_str}\n"
                f"• 渠道表现：{plat_str}\n"
                f"• 投递约面率：{st['kpis']['delivery_to_interview_rate']}%"
            )

        m_approve = re.match(r"^(?:批准|放行|投递|approve)\s*#?(\d+)$", cmd, flags=re.I)
        if m_approve:
            jid = int(m_approve.group(1))
            job = get_job(jid, db_path=self.db_path)
            if not job:
                return f"❌ 未找到编号 #{jid} 的岗位。"
            update_job_pipeline_fields(jid, db_path=self.db_path, approval_status="approved")
            if trigger_deliver_fn:
                d_res = trigger_deliver_fn(jid)
                return f"🚀 已批准 #{jid}（{job['company_name']} · {job['job_title']}）并执行投递：{d_res.get('message', '')}"
            return f"✅ 已将 #{jid}（{job['company_name']} · {job['job_title']}）标记为【已批准投递】！"

        m_skip = re.match(r"^(?:跳过|拒绝|归档|skip|reject)\s*#?(\d+)$", cmd, flags=re.I)
        if m_skip:
            jid = int(m_skip.group(1))
            job = get_job(jid, db_path=self.db_path)
            if not job:
                return f"❌ 未找到编号 #{jid} 的岗位。"
            update_job_status(jid, "rejected", db_path=self.db_path)
            return f"⚪ 已归档跳过 #{jid}（{job['company_name']} · {job['job_title']}）。"

        m_cruise = re.match(r"^巡航\s+(\S+)\s+(.+)$", cmd)
        if m_cruise:
            city, kw = m_cruise.group(1).strip(), m_cruise.group(2).strip()
            if trigger_cruise_fn:
                note = trigger_cruise_fn(city, kw)
                return f"🛸 {note}"
            return f"🛸 已记录巡航请求：城市={city}，关键词={kw}。"

        # 自由自然语言对话（接入脱敏统计上下文 + LLM）
        return self._answer_freeform_query(cmd)

    def _answer_freeform_query(self, question: str) -> str:
        st = build_full_analytics(days=14, db_path=self.db_path)
        api_key = (self.settings.get("openai_api_key") or os.getenv("OPENAI_API_KEY", "")).strip()
        base_url = (self.settings.get("openai_base_url") or os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")).strip()
        model = (self.settings.get("openai_model") or os.getenv("OPENAI_MODEL", "deepseek-chat")).strip()
        llm_enabled = bool(self.settings.get("llm_enabled", True))
        provider = str(self.settings.get("llm_provider") or "")

        context_summary = json.dumps(
            {
                "kpis": st["kpis"],
                "platform_roi": st["platform_roi"],
                "track_matrix": st["track_matrix"],
                "tokens": st["tokens"]["summary"],
            },
            ensure_ascii=False,
        )
        if not is_llm_available(api_key, base_url, llm_enabled):
            return (
                f"📊 本地统计速览：当前共 {st['kpis']['total_jobs']} 个岗位，"
                f"S/A 级高匹配 {st['kpis']['sa_grade_jobs']} 个，平均匹配分 {st['kpis']['avg_match_score']} 分。"
                f"（发送 `帮助` 查看快捷操作指令）"
            )

        masked_q, q_map, _ = self.masker.mask_with_audit(question)
        masked_ctx, c_map, _ = self.masker.mask_with_audit(context_summary)
        merged_map = {**q_map, **c_map}

        try:
            llm_out = call_llm(
                messages=[
                    {
                        "role": "system",
                        "content": "你是 HireLens 聘镜求职数据助手。回答简洁务实，直接引用给定统计数据，不讲空话，控制在 140 字以内。",
                    },
                    {
                        "role": "user",
                        "content": f"当前本地求职统计快照：{masked_ctx}\n\n用户提问：{masked_q}",
                    },
                ],
                api_key=api_key,
                base_url=base_url,
                model=model,
                expect_json=False,
                temperature=0.25,
                timeout=25,
                provider=provider,
            )
            ans = llm_out["content"].strip()
            usage = llm_out["usage"]
            if usage.get("prompt_tokens") or usage.get("completion_tokens"):
                record_token_usage(
                    "feishu_bot",
                    model,
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                    db_path=self.db_path,
                )
            return self.masker.unmask(ans, merged_map)
        except Exception as e:
            return f"本地统计速览：共 {st['kpis']['total_jobs']} 个岗位，S/A 级 {st['kpis']['sa_grade_jobs']} 个。（大模型忙碌: {str(e)[:50]}）"
