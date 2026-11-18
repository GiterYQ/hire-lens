import json
import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = BASE_DIR / "data" / "settings.json"

SECRET_FIELDS = {
    "openai_api_key",
    "vision_api_key",
    "feishu_webhook_url",
    "feishu_app_secret",
    "serper_api_key",
    "local_api_token",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    # LLM 核心配置（支持 13+ 主流云端/中转站与本地免Key Ollama）
    "llm_enabled": True,
    "llm_provider": "deepseek",
    "openai_api_key": "",
    "openai_base_url": "https://api.deepseek.com",
    "openai_model": "deepseek-chat",
    # 视觉识图配置（留空则优先使用 macOS 原生离线 Apple Vision OCR 引擎）
    "vision_provider": "apple_vision_first",  # apple_vision_first | cloud_vl
    "vision_api_key": "",
    "vision_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "vision_model": "qwen-vl-plus",
    # Token 计费单价（元 / 百万 Token，默认对齐 DeepSeek V3 标准价）
    "price_prompt_per_m_cny": 2.0,
    "price_completion_per_m_cny": 8.0,
    # 飞书全生态配置（支持 Webhook 极简模式 + 企业自建应用多维表格双向同步 + 机器人指令）
    "feishu_enabled": False,
    "feishu_mode": "webhook",  # webhook | bitable_app | both
    "feishu_webhook_url": "",
    "feishu_app_id": "",
    "feishu_app_secret": "",
    "feishu_app_token": "",
    "feishu_jobs_table_id": "",
    "feishu_resumes_table_id": "",
    "feishu_chat_id": "",
    "feishu_personal_tenant_ack": False,  # 防在职泄密安全确认锁
    # 巡航与投递防封安全门禁
    "dry_run": True,
    "daily_delivery_limit": 35,
    "browser_debug_port": 19333,
    "cruise_platforms": ["boss", "liepin", "51job", "zhilian"],
    "cruise_keywords": "AI产品经理, AI Agent",
    "cruise_city": "深圳",
    "cruise_max_per_platform": 6,
    "cruise_simulate": True,  # 默认开启安全沙盒巡航预演，关闭后调用本机真实持久化浏览器登录态
    "approval_mode": "manual_gate",  # manual_gate | auto_grade_s
    "delivery_attach_mode": "greeting_and_img",  # greeting_only | greeting_and_img | greeting_and_pdf
    # 定时自动巡航与战报调度
    "scheduler_enabled": False,
    "cruise_cron_time": "09:30",
    "report_cron_time": "20:30",
    # 背调增强与本地接口鉴权
    "serper_api_key": "",
    "local_api_token": "",
}


def mask_secret(val: str) -> str:
    """对敏感凭证生成安全脱敏指纹，严禁明文回传前端或日志"""
    s = (val or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return s[:2] + "****"
    return f"{s[:5]}****{s[-4:]}"


class SettingsManager:
    """
    热重载运行时配置中心：
    融合 .env 环境变量与 data/settings.json 持久化配置，
    支持前端设置中心可视化修改、即时热生效与敏感密钥脱敏输出。
    """

    def __init__(self, settings_path: Path | None = None):
        self.settings_path = settings_path or SETTINGS_FILE

    def load(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)

        # 1. 从环境变量读取初始值（兼容 .env）
        env_map = {
            "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
            "openai_base_url": os.getenv("OPENAI_BASE_URL", ""),
            "openai_model": os.getenv("OPENAI_MODEL", ""),
            "vision_api_key": os.getenv("VISION_API_KEY", ""),
            "vision_base_url": os.getenv("VISION_BASE_URL", ""),
            "vision_model": os.getenv("VISION_MODEL", ""),
            "feishu_webhook_url": os.getenv("FEISHU_WEBHOOK_URL", ""),
            "feishu_app_id": os.getenv("FEISHU_APP_ID", ""),
            "feishu_app_secret": os.getenv("FEISHU_APP_SECRET", ""),
            "feishu_app_token": os.getenv("FEISHU_APP_TOKEN", ""),
            "feishu_jobs_table_id": os.getenv("FEISHU_JOBS_TABLE_ID", ""),
            "feishu_resumes_table_id": os.getenv("FEISHU_RESUMES_TABLE_ID", ""),
            "feishu_chat_id": os.getenv("FEISHU_CHAT_ID", ""),
            "serper_api_key": os.getenv("SERPER_API_KEY", ""),
            "local_api_token": os.getenv("LOCAL_API_TOKEN", ""),
        }
        for k, v in env_map.items():
            if v:
                merged[k] = v

        if "DRY_RUN" in os.environ:
            merged["dry_run"] = os.environ["DRY_RUN"].lower() in ("true", "1", "yes")
        if os.getenv("DAILY_DELIVERY_LIMIT"):
            try:
                merged["daily_delivery_limit"] = int(os.environ["DAILY_DELIVERY_LIMIT"])
            except ValueError:
                pass
        if os.getenv("BROWSER_DEBUG_PORT"):
            try:
                merged["browser_debug_port"] = int(os.environ["BROWSER_DEBUG_PORT"])
            except ValueError:
                pass

        # 2. 叠加 settings.json 用户保存项
        if self.settings_path.exists():
            try:
                disk = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(disk, dict):
                    for k, v in disk.items():
                        if k in DEFAULT_SETTINGS:
                            if k in SECRET_FIELDS and not v and merged.get(k):
                                continue
                            merged[k] = v
            except Exception:
                pass

        return merged

    def get_public(self) -> Dict[str, Any]:
        """返回供前端展示的安全脱敏配置"""
        raw = self.load()
        pub: Dict[str, Any] = {}
        for k, v in raw.items():
            if k in SECRET_FIELDS:
                pub[k] = mask_secret(str(v or ""))
                pub[f"has_{k}"] = bool(v)
            else:
                pub[k] = v
        return pub

    def save(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        current = self.load()
        for k, v in updates.items():
            if k not in DEFAULT_SETTINGS:
                continue
            if k in SECRET_FIELDS:
                s_val = str(v or "").strip()
                if "****" in s_val:
                    continue
                if s_val == "__CLEAR__":
                    current[k] = ""
                elif s_val:
                    current[k] = s_val
            else:
                current[k] = v

        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._apply_to_runtime(current)
        return self.get_public()

    def _apply_to_runtime(self, cfg: Dict[str, Any]):
        import config

        if cfg.get("openai_api_key") is not None:
            if cfg["openai_api_key"]:
                os.environ["OPENAI_API_KEY"] = cfg["openai_api_key"]
            config.OPENAI_API_KEY = cfg["openai_api_key"]
        if cfg.get("openai_base_url"):
            os.environ["OPENAI_BASE_URL"] = cfg["openai_base_url"]
            config.OPENAI_BASE_URL = cfg["openai_base_url"]
        if cfg.get("openai_model"):
            os.environ["OPENAI_MODEL"] = cfg["openai_model"]
            config.OPENAI_MODEL = cfg["openai_model"]

        config.DRY_RUN = bool(cfg.get("dry_run", True))
        config.DAILY_DELIVERY_LIMIT = int(cfg.get("daily_delivery_limit", 35))
        config.BROWSER_DEBUG_PORT = int(cfg.get("browser_debug_port", 19333))
