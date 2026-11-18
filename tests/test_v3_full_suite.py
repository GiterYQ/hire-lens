import base64
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import db
from core.analytics import build_full_analytics
from core.cleaner_tier2 import SemanticScoutCleaner
from core.company_intel import CompanyIntelEngine
from core.feishu_hub import FeishuHub
from core.llm_client import clean_reasoning_and_extract_json, is_llm_available, normalize_chat_endpoint
from core.mock_interviewer import MockInterviewer
from core.pipeline import PipelineOrchestrator
from core.resume_sync import build_online_resume_mapping
from core.resume_tailor import CHROME_BIN, render_job_resume_artifacts
from core.settings_manager import SettingsManager, mask_secret
from core.vision_parser import parse_job_image
from fastapi.testclient import TestClient
from web.app import app


class TestHireLensV3FullSuite(unittest.TestCase):

    def test_01_settings_secret_masking_and_hot_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "settings.json"
            mgr = SettingsManager(settings_path=p)
            mgr.save({"openai_api_key": "sk-testsecretkey123456789", "daily_delivery_limit": 28})
            pub = mgr.get_public()
            self.assertIn("****", pub["openai_api_key"])
            self.assertNotIn("testsecretkey", pub["openai_api_key"])
            self.assertEqual(pub["daily_delivery_limit"], 28)

            # 回传脱敏带 **** 的字符串不应覆盖真实密钥
            mgr.save({"openai_api_key": pub["openai_api_key"], "daily_delivery_limit": 30})
            raw = mgr.load()
            self.assertEqual(raw["openai_api_key"], "sk-testsecretkey123456789")
            self.assertEqual(raw["daily_delivery_limit"], 30)
            self.assertTrue(mask_secret("short").endswith("****"))

    def test_02_tier2_semantic_scout_traps(self):
        scout = SemanticScoutCleaner(api_key="")
        res_veto = scout.inspect(
            "AI 应用开发工程师",
            "某科技服务公司",
            "12-18K",
            "通过甲方面试后派驻客户现场办公，配合驻点团队迭代。",
            use_llm=False,
        )
        self.assertEqual(res_veto["verdict"], "veto")
        self.assertTrue(any("驻场" in f or "外派" in f for f in res_veto["red_flags"]))

        res_warn = scout.inspect(
            "AI 产品运营",
            "某孵化器",
            "8-28K",
            "面试前请完成完整方案后再安排一面。",
            use_llm=False,
        )
        self.assertIn(res_warn["verdict"], ("warn", "veto"))
        self.assertGreaterEqual(len(res_warn["red_flags"]), 1)

    def test_03_apple_vision_local_screenshot_ocr(self):
        if not Path(CHROME_BIN).exists():
            self.skipTest("Chrome binary not available")
        with tempfile.TemporaryDirectory() as tmp:
            html_p = Path(tmp) / "card.html"
            png_p = Path(tmp) / "card.png"
            html_p.write_text(
                """<!DOCTYPE html><html><body style="background:#fff;padding:40px;font-family:'PingFang SC',sans-serif;font-size:26px;color:#000;">
                <div><strong>AI Agent 产品经理</strong> &nbsp; 18-25K·14薪</div>
                <div>深圳 &nbsp; 刚刚活跃</div>
                <div>岗位职责：负责大模型工作流产品设计与 Prompt 防幻觉优化，周末双休。</div>
                <div>深圳市云帆智能科技有限公司</div>
                </body></html>""",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    CHROME_BIN,
                    "--headless=new",
                    "--disable-gpu",
                    "--window-size=900,420",
                    f"--screenshot={png_p}",
                    html_p.as_uri(),
                ],
                capture_output=True,
                timeout=20,
                check=True,
            )
            b64 = "data:image/png;base64," + base64.b64encode(png_p.read_bytes()).decode("ascii")
            res = parse_job_image(b64, vision_provider="apple_vision_first")
            self.assertTrue(res["success"])
            self.assertEqual(res["engine"], "apple_vision_local")
            self.assertIn("18-25K", res["parsed"]["salary"])
            self.assertIn("深圳市云帆智能科技有限公司", res["parsed"]["company_name"])

    def test_04_headless_tailored_pdf_and_png_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdb = Path(tmp) / "test.db"
            db.init_db(tdb)
            jid = db.save_job(
                job_title="AI Agent 产品经理",
                company_name="前海智造科技",
                salary="16-22K",
                jd_text="负责 AI Agent 原型验证、Prompt 调优与跨团队推进。",
                selected_track="track_ai_builder",
                match_score=90,
                fit_reasons=["独立上架 App Store 应用 DemoApp", "实操过防幻觉计算隔离"],
                greeting_draft="【独立上架iOS·AI全栈】您好！",
                profile_advantage="独立上架App Store工具应用，具0→1原型与AI Agent实战。",
                ats_matrix={"matched_keywords": ["AI", "Agent", "Prompt"]},
                db_path=tdb,
            )
            job = db.get_job(jid, db_path=tdb)
            art = render_job_resume_artifacts(job)
            self.assertTrue(art["success"])
            self.assertTrue(Path(art["pdf_path"]).exists())
            self.assertTrue(Path(art["img_path"]).exists())
            self.assertTrue(Path(art["pdf_path"]).read_bytes().startswith(b"%PDF"))
            self.assertTrue(Path(art["img_path"]).read_bytes().startswith(b"\x89PNG"))
            # 清理测试产物
            Path(art["pdf_path"]).unlink(missing_ok=True)
            Path(art["img_path"]).unlink(missing_ok=True)

    def test_05_feishu_auto_bitable_bidirectional_sync_and_bot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdb = Path(tmp) / "feishu_test.db"
            db.init_db(tdb)
            jid = db.save_job(
                job_title="AI 产品经理",
                company_name="星海智能",
                salary="15-22K",
                jd_text="AI Agent 落地，周末双休",
                selected_track="track_ai_builder",
                match_score=89,
                greeting_draft="【独立上架iOS·AI全栈】您好！",
                profile_advantage="独立上架App Store应用",
                db_path=tdb,
            )

            mock_records = {}

            def fake_http(method, url, payload=None, headers=None, timeout=20):
                if "tenant_access_token" in url:
                    return {"code": 0, "tenant_access_token": "t-mock123", "expire": 7200}
                if url.endswith("/bitable/v1/apps") and method == "POST":
                    return {"code": 0, "data": {"app": {"app_token": "bascnMockApp", "url": "https://feishu.cn/base/bascnMockApp"}}}
                if "/tables?" in url and method == "GET":
                    return {"code": 0, "data": {"items": []}}
                if url.endswith("/tables") and method == "POST":
                    tname = payload["table"]["name"]
                    tid = "tblJobsMock" if "岗位" in tname else "tblResumesMock"
                    return {"code": 0, "data": {"table_id": tid}}
                if "/records" in url and method == "POST":
                    rec_id = f"rec_{len(mock_records)+1}"
                    mock_records[rec_id] = dict(payload["fields"])
                    return {"code": 0, "data": {"record": {"record_id": rec_id}}}
                if "/records/" in url and method == "PUT":
                    rec_id = url.rsplit("/", 1)[-1]
                    mock_records[rec_id] = dict(payload["fields"])
                    return {"code": 0, "data": {"record": {"record_id": rec_id}}}
                if "/records?" in url and method == "GET":
                    # 模拟用户在手机飞书上把审批改成了「批准投递」并写了备注
                    items = []
                    for rid, f in mock_records.items():
                        f_edited = dict(f)
                        f_edited["手机审批指令"] = "批准投递"
                        f_edited["跟进备注"] = "手机飞书审批通过，下午约聊"
                        items.append({"record_id": rid, "fields": f_edited})
                    return {"code": 0, "data": {"items": items}}
                if "hook_mock" in url:
                    return {"code": 0, "StatusCode": 0}
                return {"code": 0}

            cfg = {
                "feishu_app_id": "cli_mock",
                "feishu_app_secret": "sec_mock",
                "feishu_webhook_url": "https://open.feishu.cn/hook_mock",
            }
            hub = FeishuHub(cfg, http_fn=fake_http, db_path=tdb)

            # 1. 一键自动建表
            prov = hub.auto_provision_bitable()
            self.assertEqual(prov["feishu_app_token"], "bascnMockApp")
            self.assertEqual(prov["feishu_jobs_table_id"], "tblJobsMock")

            cfg.update(prov)
            # 2. 本地推送到飞书多维表格
            push_res = hub.sync_local_to_bitable()
            self.assertEqual(push_res["created"], 1)
            j_after_push = db.get_job(jid, db_path=tdb)
            self.assertEqual(j_after_push["feishu_record_id"], "rec_1")

            # 3. 从飞书手机端拉取修改回本地 SQLite
            pull_res = hub.sync_bitable_to_local()
            self.assertEqual(pull_res["updated_local"], 1)
            self.assertIn(jid, pull_res["newly_approved_ids"])
            j_after_pull = db.get_job(jid, db_path=tdb)
            self.assertEqual(j_after_pull["approval_status"], "approved")
            self.assertIn("手机飞书审批通过", j_after_pull["notes"])

            # 4. 机器人指令与富文本卡片推送
            bot_pending = hub.handle_bot_command("待批")
            self.assertIn("星海智能", bot_pending)
            bot_skip = hub.handle_bot_command(f"跳过 #{jid}")
            self.assertIn("已归档跳过", bot_skip)
            self.assertEqual(db.get_job(jid, db_path=tdb)["delivery_status"], "rejected")

            card_res = hub.push_card(hub.build_daily_report_card())
            self.assertTrue(card_res["success"])

    def test_06_full_pipeline_4_platforms_and_batch_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdb = Path(tmp) / "pipe.db"
            s_path = Path(tmp) / "settings.json"
            db.init_db(tdb)
            sm = SettingsManager(settings_path=s_path)
            sm.save({
                "dry_run": True,
                "cruise_platforms": ["boss", "liepin", "51job", "zhilian"],
                "cruise_keywords": "AI产品经理",
                "cruise_city": "深圳",
                "cruise_simulate": True,
            })

            orch = PipelineOrchestrator(settings_mgr=sm, db_path=tdb)
            res = orch.run_full_pipeline_sync({"offline_fast": True, "render_pdfs": False})
            self.assertTrue(res["success"])
            st = res["stats"]
            self.assertGreaterEqual(st["total_found"], 6)
            # 验证 51job 的外包驻场与智联的低底薪陷阱被双层漏斗成功拦截
            self.assertLess(st["tier2_passed"], st["total_found"])

            # 批准一条有效岗位并执行批量预演投递
            clean_jobs = [j for j in db.list_jobs(db_path=tdb) if j["risk_level"] != "veto"]
            self.assertGreaterEqual(len(clean_jobs), 3)
            target_id = clean_jobs[0]["id"]
            db.update_job_pipeline_fields(target_id, db_path=tdb, approval_status="approved")

            deliv_res = orch.batch_deliver_approved()
            self.assertEqual(deliv_res["delivered_count"], 1)
            self.assertEqual(db.get_job(target_id, db_path=tdb)["delivery_status"], "delivered")

            # 验证全景统计大盘聚合指标
            ana = build_full_analytics(days=14, db_path=tdb)
            self.assertEqual(ana["kpis"]["total_jobs"], st["total_found"])
            self.assertGreaterEqual(ana["kpis"]["vetoed_jobs"], 1)
            self.assertEqual(ana["kpis"]["delivered_jobs"], 1)
            self.assertEqual(len(ana["funnel"]), 7)
            self.assertEqual(len(ana["daily_trends"]), 14)

    def test_07_online_resume_mapping_and_mock_interview_and_intel(self):
        for plat in ("boss", "liepin", "51job", "zhilian"):
            m = build_online_resume_mapping("track_ai_builder", platform=plat)
            self.assertTrue(m["contact_protection_lock"]["verified_clean"])
            self.assertTrue(m["fields"]["advantage"]["within_limit"])

        intel = CompanyIntelEngine(api_key="").analyze("深圳灵境科技", "AI 产品经理", "周末双休，负责 AI Agent 原型", "16-22K")
        self.assertEqual(len(intel["must_verify_questions"]), 3)
        self.assertIn("基于岗位 JD", intel["provenance"])

        interviewer = MockInterviewer(api_key="")
        start = interviewer.start_interview({"job_title": "AI 产品经理", "company_name": "灵境", "selected_track": "track_ai_builder"})
        self.assertTrue("考官" in start["interviewer_message"] or "面试官" in start["interviewer_message"])
        turn = interviewer.evaluate_and_followup(
            {"job_title": "AI 产品经理", "selected_track": "track_ai_builder"},
            [{"role": "interviewer", "content": start["interviewer_message"]}],
            "我独立开发并上架了 DemoApp，把核心统计放在确定性代码里避免大模型算错数字。",
        )
        self.assertIn("turn_score", turn["coach_feedback"])
        card = interviewer.generate_final_scorecard([
            {"role": "candidate", "content": "test", "coach_feedback": turn["coach_feedback"]}
        ])
        self.assertGreaterEqual(card["overall_score"], 70)

    def test_08_web_v3_routes_and_local_token_security(self):
        client = TestClient(app)
        r_home = client.get("/")
        self.assertEqual(r_home.status_code, 200)
        for tab_txt in ("诊断与话术台", "招聘端实时模拟页", "全链路巡航指挥台", "数据统计大盘", "飞书协同 & 配置", "模拟面试营"):
            self.assertIn(tab_txt, r_home.text)

        r_ana = client.get("/api/analytics?days=14")
        self.assertEqual(r_ana.status_code, 200)
        self.assertIn("funnel", r_ana.json())

        r_bot = client.post("/api/feishu/bot-command", json={"command": "状态"})
        self.assertEqual(r_bot.status_code, 200)
        self.assertIn("HireLens 实时状态", r_bot.json()["reply"])

    def test_09_universal_byok_llm_gateway(self):
        # 1. URL 自动规范化防 404
        self.assertEqual(normalize_chat_endpoint("https://api.openai.com"), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(normalize_chat_endpoint("https://api.openai.com/v1/chat/completions"), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(normalize_chat_endpoint("https://api.deepseek.com"), "https://api.deepseek.com/chat/completions")
        self.assertEqual(normalize_chat_endpoint("https://api.anthropic.com", "anthropic"), "https://api.anthropic.com/v1/messages")
        self.assertEqual(normalize_chat_endpoint("http://127.0.0.1:11434"), "http://127.0.0.1:11434/v1/chat/completions")

        # 2. 推理模型 <think> 标签剥离与 JSON 提取
        raw_r1 = '<think>\n用户需要评分...\n</think>\n```json\n{"match_score": 91, "selected_track": "track_ai_builder"}\n```'
        parsed = clean_reasoning_and_extract_json(raw_r1)
        self.assertEqual(parsed["match_score"], 91)

        # 3. 本地免 Key Ollama 可用性判定与开关控制
        self.assertTrue(is_llm_available("", "http://127.0.0.1:11434/v1", llm_enabled=True))
        self.assertFalse(is_llm_available("", "https://api.deepseek.com", llm_enabled=True))
        self.assertFalse(is_llm_available("sk-123", "https://api.deepseek.com", llm_enabled=False))

        # 4. 厂商预设字典接口
        client = TestClient(app)
        r_prov = client.get("/api/llm/providers")
        self.assertEqual(r_prov.status_code, 200)
        provs = r_prov.json()["providers"]
        for k in ("deepseek", "openai", "anthropic", "dashscope", "ollama", "custom"):
            self.assertIn(k, provs)


if __name__ == "__main__":
    unittest.main()
