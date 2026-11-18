import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.pii_masker import PIIMasker
from core.jd_parser import parse_raw_job_posting
from core.veto_engine import VetoEngine
from core.matcher import JobMatcher, apply_truth_guard, ensure_front_loaded_greeting
from core.interview_copilot import InterviewCopilot
from core import db
from fastapi.testclient import TestClient
from web.app import app


class TestHireLensSuite(unittest.TestCase):

    def test_pii_masker_bidirectional_and_audit(self):
        masker = PIIMasker(
            candidate_names=["林知行", "Alex Lin"],
            phone="13800138000",
            emails=["candidate@example.com"],
            university="南方理工大学",
            sensitive_entities=["深圳某数字产业服务中心"],
            personal_domains=["demo-portfolio.example.com"],
        )
        raw = (
            "候选人林知行，电话13800138000，香港电话+852 6123 4567，邮箱candidate@example.com，"
            "微信: lin_sz_demo，毕业于南方理工大学，曾供职于深圳某数字产业服务中心，官网 demo-portfolio.example.com。"
        )
        masked, mapping, audit = masker.mask_with_audit(raw)
        self.assertNotIn("林知行", masked)
        self.assertNotIn("13800138000", masked)
        self.assertNotIn("candidate@example.com", masked)
        self.assertNotIn("南方理工大学", masked)
        self.assertGreaterEqual(audit["masked_count"], 6)

        restored = masker.unmask_obj({"text": masked, "items": [masked]}, mapping)
        self.assertIn("林知行", restored["text"])
        self.assertIn("13800138000", restored["text"])

    def test_front_loaded_greeting_guard(self):
        # 当模型误将“您好，看到贵司招聘”放在开头时，自动补齐前置硬实力钩子
        bad = "您好，看到贵司正在招聘AI产品经理，我很感兴趣！"
        fixed = ensure_front_loaded_greeting(bad, "track_ai_builder", "zh-CN")
        self.assertTrue(fixed.startswith("【独立上架iOS·AI全栈】"))

    def test_raw_jd_parser_messy_paste(self):
        messy = """
        以下是图片中提取的招聘JD文字内容：
        **AI Agent 产品经理**  **15-22K·14薪**
        📍 深圳  刚刚活跃
        [立即沟通]
        岗位职责
        1. 负责 AI Agent 工作流产品规划与 Prompt 调优；
        深圳市星辰智造科技有限公司
        上BOSS直聘，直接谈
        """
        parsed = parse_raw_job_posting(messy)
        self.assertEqual(parsed["job_title"], "AI Agent 产品经理")
        self.assertEqual(parsed["company_name"], "深圳市星辰智造科技有限公司")
        self.assertIn("15-22K", parsed["salary"])
        self.assertEqual(parsed["hr_active_status"], "刚刚活跃")

    def test_veto_engine_red_and_green_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = VetoEngine(rules_path=Path(tmp) / "rules.json")
            bad = engine.evaluate("初级开发", "某外包公司", "需要外派驻场开发，接受单休", "半年前活跃")
            self.assertEqual(bad["risk_level"], "veto")
            good = engine.evaluate("AI 产品实习生", "港资科技团队", "周末双休，五险一金，探索 AI Agent", "刚刚活跃")
            self.assertEqual(good["risk_level"], "safe")

    def test_4_track_routing_and_boss_profile_advantage(self):
        matcher = JobMatcher(api_key="")
        res_ai = matcher.match_and_generate(
            job_title="AI Agent 应用开发工程师",
            jd_text="熟悉 Cursor、Claude、Prompt 调优、Next.js 与 App Store 独立产品开发经验优先。",
            company_name="HK AI Lab",
            language="zh-CN"
        )
        self.assertEqual(res_ai["selected_track"], "track_ai_builder")
        self.assertTrue(res_ai["greeting"].startswith("【"))
        self.assertIn("profile_advantage", res_ai)
        self.assertGreater(len(res_ai["profile_advantage"]), 50)
        self.assertLessEqual(len(res_ai["profile_advantage"]), 145)
        self.assertNotIn("性格开朗", res_ai["profile_advantage"])
        self.assertNotIn("高规格接待", apply_truth_guard("负责高规格接待并赋能团队降本增效"))

    def test_sqlite_migration_and_advantage_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            test_db = Path(tmp) / "test.db"
            db.init_db(test_db)
            jid = db.save_job(
                job_title="AI PM",
                company_name="Co A",
                jd_text="Build AI Agent",
                selected_track="track_ai_builder",
                match_score=89,
                greeting_draft="【独立上架iOS·AI全栈】您好！",
                profile_advantage="独立开发并上架 App Store 应用 DemoApp",
                db_path=test_db
            )
            db.update_job_greeting(
                jid,
                greeting="【独立上架iOS·AI全栈】您好！已微调",
                profile_advantage="独立上架 App Store 应用 + Prompt 防幻觉设计",
                db_path=test_db
            )
            loaded = db.get_job(jid, db_path=test_db)
            self.assertEqual(loaded["profile_advantage"], "独立上架 App Store 应用 + Prompt 防幻觉设计")
            csv_txt = db.export_jobs_csv(db_path=test_db)
            self.assertIn("BOSS个人优势", csv_txt)

    def test_hirelens_ui_and_recruiter_simulator_endpoints(self):
        client = TestClient(app)
        r_index = client.get("/")
        self.assertEqual(r_index.status_code, 200)
        self.assertIn("HireLens 聘镜", r_index.text)
        self.assertIn("招聘端实时模拟页", r_index.text)
        self.assertIn("推荐牛人", r_index.text)


if __name__ == "__main__":
    unittest.main()
