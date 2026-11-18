import time
import random
from pathlib import Path
from typing import Tuple, Dict, Any

PROFILE_DIR = Path(__file__).resolve().parent.parent / "data" / "browser_profile"

class BossStealthDriver:
    """
    BOSS 直聘拟人防封驱动：
    1. 独立 Profile 持久化登录态（扫码一次长期免登录）
    2. HR 活跃度深度过滤（自动剔除数月前活跃的死信箱）
    3. 拟人非匀速击键输入与随机阅读间隔
    4. 支持 Dry-Run 预演模式（不触发最终发送）
    """

    def __init__(self, dry_run: bool = False, local_port: int = 19333):
        self.dry_run = dry_run
        self.local_port = local_port
        self.profile_path = str(PROFILE_DIR)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._page = None

    def _get_page(self):
        if self._page is not None:
            try:
                if not self._page.is_stopped:
                    return self._page
            except Exception:
                pass

        from DrissionPage import ChromiumPage, ChromiumOptions
        co = ChromiumOptions()
        co.set_local_port(self.local_port)
        co.set_user_data_path(self.profile_path)
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_argument('--no-first-run')
        co.set_argument('--no-default-browser-check')
        self._page = ChromiumPage(co)
        return self._page

    def check_hr_active(self, page) -> Tuple[bool, str]:
        """检查 HR 是否在线或近期活跃，过滤僵尸岗位"""
        try:
            status_ele = (
                page.ele('css:.boss-active-time', timeout=2)
                or page.ele('css:.boss-online-tag', timeout=1)
                or page.ele('css:[class*="active-time"]', timeout=1)
            )
            if not status_ele:
                return True, "活跃状态未展示（默认放行）"

            text = status_ele.text.strip()
            # 常见僵尸 HR 标签
            zombie_keywords = ["月前", "半年前", "年前", "未回复", "暂无"]
            for kw in zombie_keywords:
                if kw in text:
                    return False, f"HR 处于沉睡状态（{text}）"

            return True, f"HR 活跃（{text}）"
        except Exception as e:
            return True, f"状态探测异常放行: {str(e)}"

    def deliver(self, job_url: str, greeting_text: str) -> Dict[str, Any]:
        """执行单岗位投递"""
        if not job_url:
            return {"success": False, "message": "岗位 URL 为空"}

        if self.dry_run:
            time.sleep(1.2)
            return {
                "success": True,
                "message": f"【预演成功】已拟人生成打招呼语，未实际点击发送（Dry-Run）",
                "details": {"greeting": greeting_text, "hr_status": "预演通过"}
            }

        try:
            page = self._get_page()
            page.get(job_url)

            # 拟人阅读停顿 2~4 秒
            time.sleep(random.uniform(2.0, 3.8))

            # 检查 HR 活跃度
            is_active, hr_note = self.check_hr_active(page)
            if not is_active:
                return {"success": False, "message": f"已自动跳过：{hr_note}", "details": {"hr_status": hr_note}}

            # 寻找「立即沟通」按钮
            chat_btn = (
                page.ele('text:立即沟通', timeout=3)
                or page.ele('css:.btn-startchat', timeout=2)
                or page.ele('css:[class*="btn-chat"]', timeout=1)
            )

            if not chat_btn:
                # 检查是否已经是「继续沟通」
                if page.ele('text:继续沟通', timeout=1):
                    return {"success": False, "message": "该岗位此前已建立沟通，跳过重复投递"}
                return {"success": False, "message": "未找到「立即沟通」按钮（可能岗位已下线或触发滑块验证）"}

            # 模拟鼠标悬停后再点击
            chat_btn.hover()
            time.sleep(random.uniform(0.3, 0.6))
            chat_btn.click()
            time.sleep(random.uniform(1.8, 3.2))

            # 检查是否有弹窗需要确认
            confirm_btn = page.ele('text:确定', timeout=1) or page.ele('text:留在这个页面', timeout=1)
            if confirm_btn:
                try:
                    confirm_btn.click()
                    time.sleep(1.0)
                except Exception:
                    pass

            # 寻找聊天输入框
            input_box = (
                page.ele('css:#chat-input', timeout=5)
                or page.ele('css:.chat-input', timeout=3)
                or page.ele('css:[contenteditable="true"]', timeout=2)
            )

            if not input_box:
                return {"success": False, "message": "进入聊天室后未定位到输入框，可能新开标签页或需要滑块验证"}

            input_box.click()
            time.sleep(0.5)

            # 逐字拟人打字输入（微小抖动，避免大段瞬间粘贴被风控识别）
            for ch in greeting_text:
                input_box.input(ch)
                time.sleep(random.uniform(0.015, 0.045))

            time.sleep(random.uniform(0.8, 1.5))

            send_btn = (
                page.ele('css:.btn-send', timeout=2)
                or page.ele('text:发送', timeout=2)
                or page.ele('css:[class*="btn-send"]', timeout=1)
            )

            if send_btn and send_btn.states.is_enabled:
                send_btn.click()
            else:
                input_box.input("\n")

            time.sleep(1.5)
            return {
                "success": True,
                "message": "打招呼语已成功送达 HR！",
                "details": {"hr_status": hr_note, "sent_greeting": greeting_text}
            }

        except Exception as e:
            return {"success": False, "message": f"投递异常: {str(e)}"}
