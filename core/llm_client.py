import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

LLM_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "name": "DeepSeek 官方",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "requires_key": True,
        "price_in": 2.0,
        "price_out": 8.0,
        "key_hint": "前往 platform.deepseek.com 获取 sk-... 密钥",
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "protocol": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct", "Qwen/Qwen2.5-7B-Instruct"],
        "requires_key": True,
        "price_in": 2.0,
        "price_out": 8.0,
        "key_hint": "前往 cloud.siliconflow.cn 获取 API 密钥（含免费额度模型）",
    },
    "dashscope": {
        "name": "阿里云百炼 (通义千问)",
        "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-vl-plus"],
        "requires_key": True,
        "price_in": 0.8,
        "price_out": 2.0,
        "key_hint": "前往 bailian.console.aliyun.com 获取 sk-... 密钥",
    },
    "moonshot": {
        "name": "月之暗面 Kimi",
        "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-latest"],
        "requires_key": True,
        "price_in": 12.0,
        "price_out": 12.0,
        "key_hint": "前往 platform.moonshot.cn 获取密钥",
    },
    "zhipu": {
        "name": "智谱清言 GLM",
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-plus", "glm-4-air"],
        "requires_key": True,
        "price_in": 0.1,
        "price_out": 0.1,
        "key_hint": "前往 open.bigmodel.cn 获取密钥（glm-4-flash 免费）",
    },
    "volcengine": {
        "name": "火山方舟 (豆包 Doubao)",
        "protocol": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-1-5-pro-32k-250115",
        "models": ["doubao-1-5-pro-32k-250115", "deepseek-v3-250324"],
        "requires_key": True,
        "price_in": 0.8,
        "price_out": 2.0,
        "key_hint": "填入火山方舟 API Key 与推理接入点 ep-... 或模型名",
    },
    "openai": {
        "name": "OpenAI 官方",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "requires_key": True,
        "price_in": 1.1,
        "price_out": 4.4,
        "key_hint": "填入 OpenAI sk-proj-... 密钥",
    },
    "anthropic": {
        "name": "Anthropic Claude (原生协议)",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-haiku-latest",
        "models": ["claude-3-5-haiku-latest", "claude-sonnet-4-20250514"],
        "requires_key": True,
        "price_in": 5.8,
        "price_out": 29.0,
        "key_hint": "填入 sk-ant-... 密钥，原生适配 /v1/messages 接口",
    },
    "gemini": {
        "name": "Google Gemini",
        "protocol": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "requires_key": True,
        "price_in": 1.0,
        "price_out": 4.0,
        "key_hint": "填入 Google AI Studio API Key (AIza...)",
    },
    "openrouter": {
        "name": "OpenRouter 全球聚合",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini",
        "models": ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet", "deepseek/deepseek-chat"],
        "requires_key": True,
        "price_in": 1.2,
        "price_out": 4.8,
        "key_hint": "填入 sk-or-v1-... 密钥",
    },
    "ollama": {
        "name": "本地 Ollama (免 Key 离线)",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5:7b",
        "models": ["qwen2.5:7b", "qwen2.5:14b", "deepseek-r1:8b", "llama3.1:8b"],
        "requires_key": False,
        "price_in": 0.0,
        "price_out": 0.0,
        "key_hint": "无需 API Key！本机运行 `ollama serve` 即可 100% 离线调用",
    },
    "lmstudio": {
        "name": "本地 LM Studio (免 Key)",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:1234/v1",
        "default_model": "local-model",
        "models": ["local-model"],
        "requires_key": False,
        "price_in": 0.0,
        "price_out": 0.0,
        "key_hint": "无需 API Key！在 LM Studio 开启 Local Server (端口 1234)",
    },
    "custom": {
        "name": "自定义 OpenAI 兼容网关 / 中转站",
        "protocol": "openai",
        "base_url": "",
        "default_model": "",
        "models": [],
        "requires_key": True,
        "price_in": 2.0,
        "price_out": 8.0,
        "key_hint": "支持任意兼容 OpenAI `/v1/chat/completions` 的私有部署或第三方中转站",
    },
}


def is_local_keyless_url(base_url: str) -> bool:
    u = (base_url or "").lower()
    return ("127.0.0.1" in u or "localhost" in u or "0.0.0.0" in u) and ("11434" in u or "1234" in u or "8000" in u or "vllm" in u)


def detect_protocol(base_url: str, provider: str = "") -> str:
    if provider == "anthropic" or "api.anthropic.com" in (base_url or "").lower():
        return "anthropic"
    return "openai"


def normalize_chat_endpoint(base_url: str, protocol: str = "openai") -> str:
    """
    智能规范化用户填写的各种格式 Base URL：
    - 无论用户填 `https://api.openai.com`、`https://api.openai.com/v1` 还是完整 `.../v1/chat/completions`，都能准确拼接不报 404！
    """
    b = (base_url or "https://api.deepseek.com").strip().rstrip("/")
    if protocol == "anthropic":
        if b.endswith("/messages"):
            return b
        if b.endswith("/v1"):
            return f"{b}/messages"
        return f"{b}/v1/messages"

    if b.endswith("/chat/completions"):
        return b

    parsed = urllib.parse.urlparse(b)
    path = parsed.path.rstrip("/")
    # DeepSeek 根路径直接支持 /chat/completions；其余标准网关若无路径则自动补 /v1
    if not path and "deepseek.com" not in parsed.netloc.lower():
        return f"{b}/v1/chat/completions"
    return f"{b}/chat/completions"


def normalize_models_endpoint(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        b = b[: -len("/chat/completions")]
    if b.endswith("/messages"):
        b = b[: -len("/messages")]
    parsed = urllib.parse.urlparse(b)
    if not parsed.path.rstrip("/") and "deepseek.com" not in parsed.netloc.lower():
        return f"{b}/v1/models"
    return f"{b}/models"


def clean_reasoning_and_extract_json(raw_text: str) -> Dict[str, Any]:
    """
    兼容推理模型（如 deepseek-reasoner / QwQ / R1）与不严格遵循 json_object 的本地小模型：
    1. 剥离 `<think>...</think>` 思维链标签；
    2. 剥离 Markdown ```json 代码围栏；
    3. 精准定位最外层 `{...}` 解析为字典。
    """
    s = (raw_text or "").strip()
    s = re.sub(r"<think>[\s\S]*?</think>", "", s, flags=re.I).strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(s[start : end + 1])
        raise


def is_llm_available(api_key: Optional[str], base_url: str, llm_enabled: bool = True) -> bool:
    if not llm_enabled:
        return False
    if api_key and str(api_key).strip():
        return True
    return is_local_keyless_url(base_url)


def call_llm(
    messages: List[Dict[str, str]],
    api_key: str = "",
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-chat",
    expect_json: bool = False,
    temperature: float = 0.25,
    timeout: int = 38,
    provider: str = "",
) -> Dict[str, Any]:
    """
    统一多协议大模型调用网关（支持 OpenAI 兼容全系 + Anthropic Claude 原生 + 本地免 Key Ollama/LM Studio）：
    当推理模型不支持 `response_format: json_object` 返回 400 时，自动降级重试纯文本提取 JSON。
    """
    protocol = detect_protocol(base_url, provider)
    endpoint = normalize_chat_endpoint(base_url, protocol)

    if protocol == "anthropic":
        sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
        user_msgs = [
            {"role": "assistant" if m.get("role") == "assistant" else "user", "content": m["content"]}
            for m in messages
            if m.get("role") != "system"
        ]
        body: Dict[str, Any] = {
            "model": model,
            "max_tokens": 2048,
            "temperature": temperature,
            "messages": user_msgs,
        }
        if sys_parts:
            body["system"] = "\n".join(sys_parts)
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        blocks = data.get("content") or []
        txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        usage_raw = data.get("usage") or {}
        usage = {
            "prompt_tokens": int(usage_raw.get("input_tokens", 0)),
            "completion_tokens": int(usage_raw.get("output_tokens", 0)),
        }
        return {
            "content": txt,
            "json": clean_reasoning_and_extract_json(txt) if expect_json else None,
            "usage": usage,
        }

    # OpenAI 兼容协议（DeepSeek / Qwen / Kimi / GLM / OpenAI / Gemini / OpenRouter / Ollama）
    effective_key = api_key.strip() if api_key else ("ollama" if is_local_keyless_url(base_url) else "")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "HireLens/3.0",
    }
    if effective_key:
        headers["Authorization"] = f"Bearer {effective_key}"

    is_reasoner = "reasoner" in model.lower() or "deepseek-r1" in model.lower() or "qwq" in model.lower()

    def _send_openai(use_json_fmt: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if not is_reasoner:
            payload["temperature"] = temperature
        if use_json_fmt and not is_reasoner:
            payload["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        resp_data = _send_openai(use_json_fmt=expect_json)
    except urllib.error.HTTPError as he:
        if expect_json and he.code == 400:
            # 部分第三方模型不支持 response_format，自动降级纯 Prompt JSON 重试
            resp_data = _send_openai(use_json_fmt=False)
        else:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="ignore")[:240]
            except Exception:
                pass
            raise RuntimeError(f"HTTP {he.code}: {err_body or he.reason}") from he

    msg_obj = (resp_data.get("choices") or [{}])[0].get("message") or {}
    content = (msg_obj.get("content") or "").strip()
    usage_raw = resp_data.get("usage") or {}
    usage = {
        "prompt_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "completion_tokens": int(usage_raw.get("completion_tokens", 0)),
    }
    return {
        "content": content,
        "json": clean_reasoning_and_extract_json(content) if expect_json else None,
        "usage": usage,
    }


def test_llm_connectivity(
    api_key: str,
    base_url: str,
    model: str,
    provider: str = "",
) -> Dict[str, Any]:
    """一键实测用户自定义 LLM API 连通性、鉴权有效性与首响延迟（支持保存前预检）"""
    if not is_llm_available(api_key, base_url, llm_enabled=True):
        return {
            "success": False,
            "latency_ms": 0,
            "message": "未提供 API Key（若使用本地 Ollama 请确保地址为 http://127.0.0.1:11434/v1）",
        }

    t0 = time.perf_counter()
    try:
        res = call_llm(
            messages=[
                {"role": "system", "content": "Reply concisely."},
                {"role": "user", "content": "请仅回复 OK 两个字母以确认接口连通。"},
            ],
            api_key=api_key,
            base_url=base_url,
            model=model,
            expect_json=False,
            temperature=0.0,
            timeout=18,
            provider=provider,
        )
        ms = int((time.perf_counter() - t0) * 1000)
        preview = re.sub(r"<think>[\s\S]*?</think>", "", res["content"]).strip()[:40]
        return {
            "success": True,
            "latency_ms": ms,
            "endpoint": normalize_chat_endpoint(base_url, detect_protocol(base_url, provider)),
            "model": model,
            "reply_preview": preview,
            "usage": res["usage"],
            "message": f"连通成功！延迟 {ms}ms · 模型响应：「{preview or 'OK'}」",
        }
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        err_s = str(e)
        hint = err_s
        if "401" in err_s:
            hint = "HTTP 401 密钥无效或已过期，请核对 API Key 是否复制完整"
        elif "402" in err_s or "insufficient" in err_s.lower():
            hint = "账户余额不足（HTTP 402 / Insufficient Balance），请充值或切换免费模型（如智谱 glm-4-flash / 本地 Ollama）"
        elif "404" in err_s:
            hint = f"HTTP 404 路径或模型名不存在，请检查 Base URL ({base_url}) 与模型名称 ({model})"
        elif "Connection refused" in err_s or "urlopen error" in err_s:
            hint = f"网络连接失败（{err_s[:80]}），若为本地 Ollama 请确认已运行 `ollama serve`"
        return {
            "success": False,
            "latency_ms": ms,
            "message": f"连接未通过：{hint}",
        }


def fetch_available_models(api_key: str, base_url: str, provider: str = "") -> Dict[str, Any]:
    """从服务商 `/models` 接口自动拉取可用模型列表，失败则返回内置精选推荐列表"""
    preset = LLM_PROVIDERS.get(provider) or {}
    fallback_models = list(preset.get("models") or [])

    if detect_protocol(base_url, provider) == "anthropic":
        return {"success": True, "source": "preset", "models": fallback_models or ["claude-3-5-haiku-latest", "claude-sonnet-4-20250514"]}

    url = normalize_models_endpoint(base_url)
    headers = {"User-Agent": "HireLens/3.0"}
    eff_key = api_key.strip() if api_key else ("ollama" if is_local_keyless_url(base_url) else "")
    if eff_key:
        headers["Authorization"] = f"Bearer {eff_key}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or data.get("models") or []
        ids = []
        for it in items:
            if isinstance(it, dict):
                mid = it.get("id") or it.get("name")
                if mid:
                    ids.append(str(mid))
        if ids:
            return {"success": True, "source": "remote_api", "models": ids[:80]}
    except Exception as e:
        if fallback_models:
            return {"success": True, "source": "preset_fallback", "models": fallback_models, "note": str(e)[:80]}
        return {"success": False, "models": [], "message": f"无法自动拉取模型列表（可手动输入模型名）: {str(e)[:80]}"}

    return {"success": True, "source": "preset", "models": fallback_models}
