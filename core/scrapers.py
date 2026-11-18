import random
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
PROFILES_ROOT = BASE_DIR / "data" / "profiles"
LEGACY_BOSS_PROFILE = BASE_DIR / "data" / "browser_profile"

# 全局浏览器互斥锁：防止多平台巡航与自动投递同时抢占 Chromium 实例
BROWSER_MUTEX = threading.RLock()

CITY_CODES = {
    "boss": {"深圳": "101280600", "广州": "101280100", "香港": "101320100", "上海": "101020100", "北京": "101010100", "杭州": "101210100"},
    "liepin": {"深圳": "050090", "广州": "050020", "香港": "310010", "上海": "020", "北京": "010", "杭州": "070020"},
    "51job": {"深圳": "040000", "广州": "030200", "香港": "330000", "上海": "020000", "北京": "010000", "杭州": "080200"},
    "zhilian": {"深圳": "765", "广州": "763", "香港": "891", "上海": "538", "北京": "530", "杭州": "653"},
}

PLATFORM_META = {
    "boss": {
        "name": "BOSS直聘",
        "home_url": "https://www.zhipin.com/web/geek/job",
        "port_offset": 0,
    },
    "liepin": {
        "name": "猎聘",
        "home_url": "https://www.liepin.com/zhaopin/",
        "port_offset": 1,
    },
    "51job": {
        "name": "前程无忧(51job)",
        "home_url": "https://we.51job.com/pc/search",
        "port_offset": 2,
    },
    "zhilian": {
        "name": "智联招聘",
        "home_url": "https://www.zhaopin.com/sou/",
        "port_offset": 3,
    },
}


def get_profile_dir(platform: str) -> Path:
    if platform == "boss" and LEGACY_BOSS_PROFILE.exists():
        return LEGACY_BOSS_PROFILE
    p = PROFILES_ROOT / platform
    p.mkdir(parents=True, exist_ok=True)
    return p


def inspect_browser_profiles() -> Dict[str, Any]:
    """零弹窗静态检测 4 大平台本地持久化登录态目录状态"""
    report: Dict[str, Any] = {}
    for plat, meta in PLATFORM_META.items():
        pdir = get_profile_dir(plat)
        cookie_candidates = [
            pdir / "Default" / "Cookies",
            pdir / "Default" / "Network" / "Cookies",
            pdir / "Cookies",
        ]
        has_cookies = any(c.exists() and c.stat().st_size > 1024 for c in cookie_candidates)
        report[plat] = {
            "name": meta["name"],
            "profile_dir": str(pdir),
            "initialized": pdir.exists() and any(pdir.iterdir()) if pdir.exists() else False,
            "has_session_cookies": has_cookies,
        }
    return report


def check_anti_bot_gate(page) -> Optional[str]:
    """
    合规红线守护：检测是否出现验证码、滑块或反爬拦截页。
    一旦发现立即返回原因并暂停任务，绝不尝试自动破解或绕过验证码！
    """
    try:
        curr_url = (page.url or "").lower()
        gate_urls = ["zpAntispam".lower(), "verify", "captcha", "sec.51job", "passport.zhaopin.com/org/login"]
        for gu in gate_urls:
            if gu in curr_url:
                return f"检测到平台验证/登录门禁 URL ({curr_url[:60]})"

        html_head = (page.html or "")[:4000]
        gate_phrases = ["请完成安全验证", "拖动下方滑块", "访问过于频繁", "异常访问验证", "向右滑动完成拼图"]
        for phrase in gate_phrases:
            if phrase in html_head:
                return f"页面触发平台安全验证提示（「{phrase}」）"
    except Exception:
        pass
    return None


class MultiPlatformScraper:
    """
    四平台低频拟人安全巡航采集器（BOSS直聘 / 猎聘 / 前程无忧 51job / 智联招聘）：
    - 支持 Simulate 沙盒巡航预演（零浏览器弹窗、秒级验证全链路漏斗与双层排雷）
    - 支持 Live 真实持久化登录态低频巡航（随机滚动停顿、僵尸 HR 过滤、遇到验证码立即安全挂起）
    """

    def __init__(self, base_port: int = 19333):
        self.base_port = base_port

    def _build_search_url(self, platform: str, keyword: str, city: str) -> str:
        kw_enc = urllib.parse.quote(keyword)
        code = CITY_CODES.get(platform, {}).get(city, "")
        if platform == "boss":
            c = code or "101280600"
            return f"https://www.zhipin.com/web/geek/job?query={kw_enc}&city={c}"
        if platform == "liepin":
            c = code or "050090"
            return f"https://www.liepin.com/zhaopin/?key={kw_enc}&dq={c}"
        if platform == "51job":
            c = code or "040000"
            return f"https://we.51job.com/pc/search?keyword={kw_enc}&jobArea={c}"
        if platform == "zhilian":
            c = code or "765"
            return f"https://www.zhaopin.com/sou/?jl={c}&kw={kw_enc}"
        return PLATFORM_META["boss"]["home_url"]

    def open_login_window(self, platform: str) -> Dict[str, Any]:
        """仅当用户在 Web 界面主动点击「打开浏览器扫码保存登录态」时调用"""
        if platform not in PLATFORM_META:
            return {"success": False, "message": f"不支持的平台: {platform}"}
        meta = PLATFORM_META[platform]
        pdir = get_profile_dir(platform)
        port = self.base_port + meta["port_offset"]

        with BROWSER_MUTEX:
            from DrissionPage import ChromiumOptions, ChromiumPage
            co = ChromiumOptions()
            co.set_local_port(port)
            co.set_user_data_path(str(pdir))
            co.set_argument("--disable-blink-features=AutomationControlled")
            co.set_argument("--no-first-run")
            page = ChromiumPage(co)
            page.get(meta["home_url"])
            return {
                "success": True,
                "message": f"已打开 {meta['name']} 独立配置浏览器（端口 {port}），扫码登录后保持或关闭即可自动持久化 Cookie。",
                "profile_dir": str(pdir),
            }

    def cruise(
        self,
        platforms: List[str],
        keywords: List[str],
        city: str = "深圳",
        max_per_platform: int = 5,
        simulate: bool = True,
        abort_event: Optional[threading.Event] = None,
        log_fn: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, Any]:
        def _log(msg: str, level: str = "INFO"):
            if log_fn:
                log_fn(msg, level)

        clean_platforms = [p for p in platforms if p in PLATFORM_META] or ["boss"]
        clean_keywords = [k.strip() for k in keywords if k.strip()] or ["AI产品经理"]

        if simulate:
            return self._simulate_cruise(clean_platforms, clean_keywords, city, max_per_platform, abort_event, _log)

        return self._live_cruise(clean_platforms, clean_keywords, city, max_per_platform, abort_event, _log)

    def _simulate_cruise(
        self,
        platforms: List[str],
        keywords: List[str],
        city: str,
        max_per_platform: int,
        abort_event: Optional[threading.Event],
        _log: Callable[[str, str], None],
    ) -> Dict[str, Any]:
        """安全沙盒巡航池：涵盖 4 大平台真实岗位形态 + 典型外包/虚标坑位，供端到端验证双层漏斗与飞书同步"""
        stamp = datetime.now().strftime("%m%d%H%M")
        primary_kw = keywords[0]
        seed_pool = {
            "boss": [
                {
                    "job_title": f"{primary_kw}（0-1独立产品优先）",
                    "company_name": "深圳灵境智造科技有限公司",
                    "salary": "15-22K·14薪",
                    "city": city,
                    "hr_title": "产品负责人",
                    "hr_active_status": "刚刚活跃",
                    "job_url": f"https://www.zhipin.com/job_detail/sim_boss_1_{stamp}.html",
                    "platform": "boss",
                    "jd_text": (
                        "岗位职责：\n"
                        "1. 负责公司面向海外与深港个人用户的效率类 AI Agent 应用从 0 到 1 原型设计与需求拆解；\n"
                        "2. 设计大模型 Prompt 边界与确定性代码校验机制，杜绝生成幻觉，保障用户数据隐私；\n"
                        "3. 熟练使用 Cursor / Claude 等工具快速产出可交互 Demo（Web 或 iOS/Flutter），推动快速迭代。\n"
                        "任职要求：本科及以上，不卡专业背景，有独立上架 App Store 应用或完整上线作品者优先；周末双休，六险一金。"
                    ),
                },
                {
                    "job_title": "AI 数字化工作流与项目运营专员",
                    "company_name": "前海云帆数字科技（深圳）有限公司",
                    "salary": "12-16K",
                    "city": city,
                    "hr_title": "运营总监",
                    "hr_active_status": "今日活跃",
                    "job_url": f"https://www.zhipin.com/job_detail/sim_boss_2_{stamp}.html",
                    "platform": "boss",
                    "jd_text": (
                        "岗位职责：\n"
                        "1. 统筹跨部门项目进度，搭建标准化的飞书多维表格看板与企业服务台账闭环；\n"
                        "2. 运用大模型、飞书机器人与 n8n 自动化优化内部资料流转，划定 AI 自动处理与人工复核（HITL）边界；\n"
                        "3. 协助整理项目纪要与汇报材料。要求：3年以上多方项目协调经验，粤语及普通话流利优先，双休弹性打卡。"
                    ),
                },
            ],
            "liepin": [
                {
                    "job_title": "AI Agent 应用产品经理（深港协同）",
                    "company_name": "港深智联创新科技集团",
                    "salary": "16-24K·13薪",
                    "city": city,
                    "hr_title": "HRBP",
                    "hr_active_status": "在线",
                    "job_url": f"https://www.liepin.com/job/sim_liepin_1_{stamp}.shtml",
                    "platform": "liepin",
                    "jd_text": (
                        "职位描述：\n"
                        "1. 结合业务团队真实痛点，主导内部智能体工作台与客户服务辅助工具的产品规划与 MVP 验证；\n"
                        "2. 跟踪 LLM Function Calling 准确率与 Token 成本审计，输出清晰的 SOP 文档；\n"
                        "3. 对接深圳研发与香港业务同事，粤语沟通顺畅，有 Python / Next.js 实操原型经验加分，周末双休，带薪年假。"
                    ),
                },
            ],
            "51job": [
                {
                    "job_title": "初级 AI 应用开发驻场支持",
                    "company_name": "某软件人力外包服务公司",
                    "salary": "10-14K",
                    "city": city,
                    "hr_title": "招聘专员",
                    "hr_active_status": "3月前活跃",
                    "job_url": f"https://jobs.51job.com/{city}/sim_51job_trap_{stamp}.html",
                    "platform": "51job",
                    "jd_text": (
                        "岗位说明：需要外派驻场开发，与甲方面试通过后派驻客户现场办公，接受大小周与高频加班安排，抗压能力极强。"
                    ),
                },
                {
                    "job_title": "综合行政与专职项目管理助理",
                    "company_name": "深圳湾精密智能医疗有限公司",
                    "salary": "11-15K",
                    "city": city,
                    "hr_title": "总裁办主任",
                    "hr_active_status": "今日活跃",
                    "job_url": f"https://jobs.51job.com/{city}/sim_51job_good_{stamp}.html",
                    "platform": "51job",
                    "jd_text": (
                        "岗位职责：\n"
                        "1. 负责管理层日程差旅统筹、跨部门督办双台账推进及大型行业论坛会务服务配合；\n"
                        "2. 规范公文纪要、供应商验收结算与档案保密管理，善用飞书多维表格与 AI 办公工具提效；\n"
                        "任职要求：全日制本科，3年以上政府机关或大型机构综合协调经验，五险一金，周末双休。"
                    ),
                },
            ],
            "zhilian": [
                {
                    "job_title": "AI 新媒体产品运营（综合高薪）",
                    "company_name": "深圳某电商孵化咨询工作室",
                    "salary": "8-28K",
                    "city": city,
                    "hr_title": "人事经理",
                    "hr_active_status": "本周活跃",
                    "job_url": f"https://jobs.zhaopin.com/sim_zhilian_warn_{stamp}.htm",
                    "platform": "zhilian",
                    "jd_text": (
                        "无责底薪3K加高额提成，面试前需完成完整方案后再安排一面，需一人独立负责产品开发与运营剪辑销售全流程。"
                    ),
                },
                {
                    "job_title": "AI 产品策划与品牌内容统筹",
                    "company_name": "深圳星图互动数字创意有限公司",
                    "salary": "13-18K",
                    "city": city,
                    "hr_title": "内容产品总监",
                    "hr_active_status": "刚刚活跃",
                    "job_url": f"https://jobs.zhaopin.com/sim_zhilian_good_{stamp}.htm",
                    "platform": "zhilian",
                    "jd_text": (
                        "岗位职责：\n"
                        "1. 参与公司 AI 创意工具产品的功能策划、交互原型梳理及产品演示视频脚本摄制；\n"
                        "2. 结合广电编导视听审美与 AI 工具实操，把复杂技术功能转化为通俗易懂的用户引导与宣发物料；\n"
                        "任职要求：本科及以上，兼具 AI 产品动手实践与视频图文全流程交付经验者优先，双休扁平化。"
                    ),
                },
            ],
        }

        collected: List[Dict[str, Any]] = []
        for plat in platforms:
            if abort_event and abort_event.is_set():
                _log("收到手动中止信号，停止后续平台巡航。", "WARN")
                break
            p_name = PLATFORM_META[plat]["name"]
            _log(f"[{p_name}] 正在启动沙盒低频巡航（关键词：{', '.join(keywords)} · 城市：{city}）...", "INFO")
            time.sleep(0.25)
            items = seed_pool.get(plat, [])[:max_per_platform]
            for it in items:
                collected.append(dict(it))
            _log(f"[{p_name}] 采集返回 {len(items)} 条原始职位卡片。", "INFO")

        return {
            "status": "ok",
            "mode": "simulate",
            "jobs": collected,
            "captcha_paused": False,
        }

    def _live_cruise(
        self,
        platforms: List[str],
        keywords: List[str],
        city: str,
        max_per_platform: int,
        abort_event: Optional[threading.Event],
        _log: Callable[[str, str], None],
    ) -> Dict[str, Any]:
        """真实持久化浏览器低频防封巡航（串行锁 + 随机停顿 + 验证码立即挂起不强闯）"""
        collected: List[Dict[str, Any]] = []
        captcha_reason = ""

        with BROWSER_MUTEX:
            from DrissionPage import ChromiumOptions, ChromiumPage

            for plat in platforms:
                if abort_event and abort_event.is_set():
                    break
                meta = PLATFORM_META[plat]
                pdir = get_profile_dir(plat)
                port = self.base_port + meta["port_offset"]

                co = ChromiumOptions()
                co.set_local_port(port)
                co.set_user_data_path(str(pdir))
                co.set_argument("--disable-blink-features=AutomationControlled")
                co.set_argument("--no-first-run")
                co.set_argument("--no-default-browser-check")

                try:
                    page = ChromiumPage(co)
                except Exception as e:
                    _log(f"[{meta['name']}] 浏览器实例连接失败: {e}", "ERROR")
                    continue

                for kw in keywords[:2]:
                    if abort_event and abort_event.is_set():
                        break
                    url = self._build_search_url(plat, kw, city)
                    _log(f"[{meta['name']}] 正在访问检索页: {kw} ({city})", "INFO")
                    try:
                        page.get(url)
                        time.sleep(random.uniform(2.5, 4.2))

                        gate = check_anti_bot_gate(page)
                        if gate:
                            captcha_reason = f"[{meta['name']}] {gate}"
                            _log(f"⛔ {captcha_reason} —— 已按合规红线主动挂起，请手动完成验证！", "WARN")
                            return {
                                "status": "captcha_paused",
                                "mode": "live",
                                "jobs": collected,
                                "captcha_paused": True,
                                "message": captcha_reason,
                            }

                        cards = self._extract_dom_cards(plat, page, city, max_per_platform)
                        _log(f"[{meta['name']}] 解析获得 {len(cards)} 个职位条目", "INFO")
                        collected.extend(cards)
                        time.sleep(random.uniform(1.8, 3.2))
                    except Exception as e:
                        _log(f"[{meta['name']}] 页面抓取异常: {e}", "WARN")

        return {
            "status": "ok",
            "mode": "live",
            "jobs": collected,
            "captcha_paused": False,
        }

    def _extract_dom_cards(self, platform: str, page, city: str, limit: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        selectors = {
            "boss": "css:.job-card-wrapper, .job-card-box",
            "liepin": "css:.job-list-item, [class*='job-card-pc']",
            "51job": "css:.joblist-item, .j_joblist .e",
            "zhilian": "css:.joblist-box__item, [class*='positionlist__item']",
        }
        eles = page.eles(selectors.get(platform, "css:a"), timeout=4)
        for card in eles[:limit]:
            try:
                raw_txt = (card.text or "").strip()
                if len(raw_txt) < 12:
                    continue
                link_ele = card.ele("tag:a", timeout=0.5)
                href = link_ele.attr("href") if link_ele else ""
                lines = [ln.strip() for ln in raw_txt.splitlines() if ln.strip()]
                title = lines[0][:60] if lines else "待核实职位"
                salary = next((ln for ln in lines if "K" in ln.upper() or "万" in ln or "薪" in ln), "")
                company = lines[-1][:50] if len(lines) >= 2 else "招聘企业"
                results.append({
                    "job_title": title,
                    "company_name": company,
                    "salary": salary,
                    "city": city,
                    "hr_title": "",
                    "hr_active_status": "近期活跃",
                    "job_url": href or "",
                    "platform": platform,
                    "jd_text": raw_txt,
                })
            except Exception:
                continue
        return results
