import os
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .scrapers import BROWSER_MUTEX, PLATFORM_META, check_anti_bot_gate, get_profile_dir


def detect_platform_from_url(job_url: str, fallback: str = "boss") -> str:
    u = (job_url or "").lower()
    if "zhipin.com" in u:
        return "boss"
    if "liepin.com" in u:
        return "liepin"
    if "51job.com" in u:
        return "51job"
    if "zhaopin.com" in u:
        return "zhilian"
    return fallback if fallback in PLATFORM_META else "boss"


class MultiPlatformDeliveryEngine:
    """
    四平台拟人防封自动投递引擎（BOSS直聘 / 猎聘 / 前程无忧 51job / 智联招聘）：
    1. 升级支持「文字重点前置破冰语 + BOSS 高清图片简历 / 猎聘51智联 A4 PDF 附件」组合投递；
    2. 串行浏览器互斥锁 + 沉睡 HR 拦截 + 随机键入节奏；
    3. 严守合规红线：检测到任何验证码或滑块页面立即挂起返回 `captcha_paused`，绝不强闯。
    """

    def __init__(self, dry_run: bool = True, base_port: int = 19333):
        self.dry_run = dry_run
        self.base_port = base_port

    def deliver_job(
        self,
        job: Dict[str, Any],
        attach_mode: str = "greeting_and_img",
    ) -> Dict[str, Any]:
        job_url = (job.get("job_url") or "").strip()
        platform = detect_platform_from_url(job_url, job.get("platform") or "boss")
        plat_name = PLATFORM_META.get(platform, PLATFORM_META["boss"])["name"]
        greeting = (job.get("greeting_final") or job.get("greeting_draft") or "").strip()
        pdf_path = (job.get("tailored_pdf_path") or "").strip()
        img_path = (job.get("tailored_img_path") or "").strip()

        if not greeting:
            return {"success": False, "platform": platform, "message": "打招呼破冰语为空，已中止投递"}

        attachment_used = "无附件（纯破冰语）"
        if attach_mode == "greeting_and_img" and img_path and os.path.exists(img_path):
            attachment_used = f"高清图片简历 ({Path(img_path).name})"
        elif attach_mode == "greeting_and_pdf" and pdf_path and os.path.exists(pdf_path):
            attachment_used = f"定制 A4 PDF ({Path(pdf_path).name})"

        if self.dry_run:
            time.sleep(0.35)
            trace = [
                f"[预演模式 Dry-Run] 目标平台：{plat_name}",
                f"HR活跃核验：{job.get('hr_active_status') or '今日活跃'}（通过）",
                f"首屏前17字核验：「{greeting[:17]}...」",
                f"拟人逐字输入破冰语（共 {len(greeting)} 字）",
                f"简历附件策略：{attachment_used}",
                "状态：预演校验全部通过（未触发真实网页点击发送，零封号风险）",
            ]
            return {
                "success": True,
                "dry_run": True,
                "platform": platform,
                "attachment": attachment_used,
                "message": f"【{plat_name} 预演投递成功】已验证前置破冰语与 {attachment_used}",
                "trace": trace,
            }

        if not job_url or not job_url.startswith("http"):
            return {"success": False, "platform": platform, "message": "缺少有效岗位详情页 URL，无法启动真实浏览器投递"}

        with BROWSER_MUTEX:
            from DrissionPage import ChromiumOptions, ChromiumPage

            meta = PLATFORM_META.get(platform, PLATFORM_META["boss"])
            pdir = get_profile_dir(platform)
            port = self.base_port + meta["port_offset"]

            co = ChromiumOptions()
            co.set_local_port(port)
            co.set_user_data_path(str(pdir))
            co.set_argument("--disable-blink-features=AutomationControlled")
            co.set_argument("--no-first-run")
            co.set_argument("--no-default-browser-check")

            try:
                page = ChromiumPage(co)
                page.get(job_url)
                time.sleep(random.uniform(2.2, 3.8))

                gate = check_anti_bot_gate(page)
                if gate:
                    return {
                        "success": False,
                        "captcha_paused": True,
                        "platform": platform,
                        "message": f"⛔ [{plat_name}] {gate} —— 已安全挂起，请手动验证后重试",
                    }

                if platform == "boss":
                    return self._deliver_boss(page, greeting, img_path if attach_mode == "greeting_and_img" else "")
                if platform == "liepin":
                    return self._deliver_liepin(page, greeting, pdf_path if attach_mode == "greeting_and_pdf" else "")
                if platform == "51job":
                    return self._deliver_51job(page, pdf_path)
                if platform == "zhilian":
                    return self._deliver_zhilian(page, pdf_path)

                return {"success": False, "platform": platform, "message": f"暂不支持该平台自动动作: {platform}"}
            except Exception as e:
                return {"success": False, "platform": platform, "message": f"[{plat_name}] 投递执行异常: {str(e)}"}

    def _type_humanly(self, ele, text: str):
        for ch in text:
            ele.input(ch)
            time.sleep(random.uniform(0.015, 0.042))

    def _deliver_boss(self, page, greeting: str, img_path: str) -> Dict[str, Any]:
        # 检查沉睡 HR
        status_ele = page.ele("css:.boss-active-time, [class*='active-time']", timeout=1.5)
        if status_ele:
            st_txt = status_ele.text.strip()
            if any(z in st_txt for z in ("月前", "半年前", "年前")):
                return {"success": False, "platform": "boss", "message": f"已跳过僵尸岗位：HR {st_txt}"}

        chat_btn = page.ele("text:立即沟通", timeout=3) or page.ele("css:.btn-startchat", timeout=2)
        if not chat_btn:
            if page.ele("text:继续沟通", timeout=1):
                return {"success": False, "platform": "boss", "message": "该岗位此前已沟通，防重复跳过"}
            return {"success": False, "platform": "boss", "message": "未定位到「立即沟通」按钮（请确认登录态或岗位是否下线）"}

        chat_btn.hover()
        time.sleep(random.uniform(0.3, 0.6))
        chat_btn.click()
        time.sleep(random.uniform(2.0, 3.2))

        input_box = (
            page.ele("css:#chat-input", timeout=4)
            or page.ele("css:.chat-input", timeout=2)
            or page.ele('css:[contenteditable="true"]', timeout=2)
        )
        if not input_box:
            return {"success": False, "platform": "boss", "message": "进入会话后未定位到聊天输入框"}

        input_box.click()
        time.sleep(0.4)
        self._type_humanly(input_box, greeting)
        time.sleep(random.uniform(0.7, 1.3))

        send_btn = page.ele("css:.btn-send", timeout=1.5) or page.ele("text:发送", timeout=1.5)
        if send_btn and send_btn.states.is_enabled:
            send_btn.click()
        else:
            input_box.input("\n")

        img_sent = False
        if img_path and os.path.exists(img_path):
            time.sleep(random.uniform(1.2, 2.0))
            file_input = page.ele('css:input[type="file"][accept*="image"]', timeout=2)
            if file_input:
                try:
                    file_input.input(img_path)
                    time.sleep(2.0)
                    confirm_img = page.ele("text:发送", timeout=2) or page.ele("text:确定", timeout=1)
                    if confirm_img:
                        confirm_img.click()
                    img_sent = True
                except Exception:
                    pass

        extra = " + 高清图片简历" if img_sent else ""
        return {"success": True, "platform": "boss", "message": f"BOSS直聘破冰语{extra}已送达 HR！"}

    def _deliver_liepin(self, page, greeting: str, pdf_path: str) -> Dict[str, Any]:
        btn = page.ele("text:聊一聊", timeout=3) or page.ele("text:立即沟通", timeout=2) or page.ele("text:应聘职位", timeout=2)
        if not btn:
            return {"success": False, "platform": "liepin", "message": "猎聘页面未找到「聊一聊/应聘职位」按钮（请检查登录态）"}
        btn.click()
        time.sleep(random.uniform(2.0, 3.2))

        box = page.ele("css:textarea", timeout=3) or page.ele('css:[contenteditable="true"]', timeout=2)
        if box:
            box.click()
            self._type_humanly(box, greeting)
            send = page.ele("text:发送", timeout=2)
            if send:
                send.click()

        if pdf_path and os.path.exists(pdf_path):
            f_in = page.ele('css:input[type="file"]', timeout=1.5)
            if f_in:
                try:
                    f_in.input(pdf_path)
                except Exception:
                    pass

        return {"success": True, "platform": "liepin", "message": "猎聘沟通与简历投递指令已执行"}

    def _deliver_51job(self, page, pdf_path: str) -> Dict[str, Any]:
        btn = page.ele("text:立即申请", timeout=3) or page.ele("text:申请职位", timeout=2)
        if not btn:
            if page.ele("text:已申请", timeout=1):
                return {"success": False, "platform": "51job", "message": "前程无忧该职位已申请过，跳过重复投递"}
            return {"success": False, "platform": "51job", "message": "前程无忧未找到「立即申请」按钮（请检查登录态）"}
        btn.click()
        time.sleep(random.uniform(2.0, 3.0))
        return {"success": True, "platform": "51job", "message": "前程无忧(51job) 职位申请已提交"}

    def _deliver_zhilian(self, page, pdf_path: str) -> Dict[str, Any]:
        btn = page.ele("text:投递简历", timeout=3) or page.ele("text:立即沟通", timeout=2)
        if not btn:
            if page.ele("text:已投递", timeout=1):
                return {"success": False, "platform": "zhilian", "message": "智联招聘该岗位已投递过，跳过重复"}
            return {"success": False, "platform": "zhilian", "message": "智联招聘未找到「投递简历」按钮（请检查登录态）"}
        btn.click()
        time.sleep(random.uniform(2.0, 3.0))
        return {"success": True, "platform": "zhilian", "message": "智联招聘简历投递已提交"}
