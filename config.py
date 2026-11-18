import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# 纯标准库轻量加载 .env（零外部依赖，避免虚拟环境未激活时报错）
if ENV_FILE.exists():
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k:
                    os.environ[k] = v

# ==========================================
# 核心网络与安全配置
# ==========================================
# 严格绑定本地回环地址，严禁 0.0.0.0 导致内网裸奔
BIND_HOST = os.getenv("BIND_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8765"))

# ==========================================
# 大模型配置（可选，不配则自动启用启发式本地引擎）
# ==========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "deepseek-chat")

# ==========================================
# 防封风控阀门
# ==========================================
# 每日投递上限安全门禁（建议 30-40 封，绝不贪多引发风控）
DAILY_DELIVERY_LIMIT = int(os.getenv("DAILY_DELIVERY_LIMIT", "35"))
BROWSER_DEBUG_PORT = int(os.getenv("BROWSER_DEBUG_PORT", "19333"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# 候选人基本盘（默认读取本地 data/candidate_profile.json，仓库代码不硬编码个人隐私）
CANDIDATE_NAME = os.getenv("CANDIDATE_NAME", "林知行")
CANDIDATE_PHONE = os.getenv("CANDIDATE_PHONE", "13800138000")
CANDIDATE_EMAIL = os.getenv("CANDIDATE_EMAIL", "candidate@example.com")
