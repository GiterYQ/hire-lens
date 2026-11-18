import base64
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict

from .jd_parser import parse_raw_job_posting
from .veto_engine import VetoEngine
from .matcher import strip_json_fence

_APPLE_VISION_SWIFT = r"""
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1 else { exit(1) }
let imgPath = args[1]
guard let nsImage = NSImage(contentsOfFile: imgPath),
      let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("IMAGE_LOAD_ERROR\n", stderr)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
    let observations = request.results ?? []
    for obs in observations {
        if let top = obs.topCandidates(1).first {
            print(top.string)
        }
    }
} catch {
    fputs("OCR_ERROR: \(error)\n", stderr)
    exit(3)
}
"""


def _decode_data_uri(image_data: str) -> tuple[bytes, str]:
    s = (image_data or "").strip()
    ext = ".png"
    if s.startswith("data:"):
        header, b64 = s.split(",", 1)
        if "jpeg" in header or "jpg" in header:
            ext = ".jpg"
        elif "webp" in header:
            ext = ".webp"
        return base64.b64decode(b64), ext
    return base64.b64decode(s), ext


def run_apple_vision_ocr(image_bytes: bytes, ext: str = ".png") -> str:
    """调用 macOS 原生 Apple Vision 框架进行 100% 本地离线中英繁高精度 OCR（零云端上传、零 Token 消耗）"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        img_file = tmp_dir / f"job_shot{ext}"
        swift_file = tmp_dir / "ocr.swift"
        img_file.write_bytes(image_bytes)
        swift_file.write_text(_APPLE_VISION_SWIFT, encoding="utf-8")

        proc = subprocess.run(
            ["/usr/bin/swift", str(swift_file), str(img_file)],
            capture_output=True,
            text=True,
            timeout=22,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"Apple Vision OCR exit {proc.returncode}")
        return proc.stdout.strip()


def run_cloud_vl_parse(
    data_uri: str,
    api_key: str,
    base_url: str,
    model: str,
) -> Dict[str, Any]:
    """调用兼容 OpenAI Vision 协议的多模态模型（如 qwen-vl-plus / gpt-4o-mini / glm-4v）直接解析岗位截图"""
    if not data_uri.startswith("data:"):
        data_uri = f"data:image/png;base64,{data_uri}"

    prompt = (
        "请提取这张招聘岗位截图中的全部关键信息，严格返回 JSON：\n"
        '{"job_title": "职位名称", "company_name": "公司名称", "salary": "薪资范围", '
        '"city": "城市", "hr_active_status": "HR活跃度如刚刚活跃", "jd_text": "完整岗位职责与要求正文"}'
    )
    api_url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
        "temperature": 0.1,
    }
    req = urllib.request.Request(api_url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=35) as resp:
        resp_data = json.loads(resp.read().decode("utf-8"))
        raw = strip_json_fence(resp_data["choices"][0]["message"]["content"])
        parsed = json.loads(raw)
        usage = resp_data.get("usage") or {}
        return {"parsed": parsed, "usage": usage}


def parse_job_image(
    image_data: str,
    vision_provider: str = "apple_vision_first",
    vision_api_key: str = "",
    vision_base_url: str = "",
    vision_model: str = "qwen-vl-plus",
) -> Dict[str, Any]:
    """
    双模岗位截图解析入口：
    1. 默认优先调用 macOS 原生离线 Apple Vision OCR（零成本、隐私不出本机、适配 DeepSeek 纯文本用户）；
    2. 若配置并选用 cloud_vl，则调用多模态大模型，失败自动无缝回退 Apple Vision。
    """
    img_bytes, ext = _decode_data_uri(image_data)
    engine_used = "apple_vision_local"
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    raw_ocr_text = ""

    if vision_provider == "cloud_vl" and vision_api_key:
        try:
            vl_res = run_cloud_vl_parse(image_data, vision_api_key, vision_base_url, vision_model)
            p = vl_res["parsed"]
            parsed = {
                "job_title": p.get("job_title", ""),
                "company_name": p.get("company_name", ""),
                "salary": p.get("salary", ""),
                "city": p.get("city", "深圳"),
                "hr_active_status": p.get("hr_active_status", "今日活跃"),
                "job_url": "",
                "platform": "vision_ocr",
                "jd_text": p.get("jd_text", ""),
            }
            usage = {
                "prompt_tokens": int(vl_res["usage"].get("prompt_tokens", 0)),
                "completion_tokens": int(vl_res["usage"].get("completion_tokens", 0)),
            }
            engine_used = f"cloud_vl ({vision_model})"
            veto = VetoEngine().evaluate(
                parsed["job_title"], parsed["company_name"], parsed["jd_text"], parsed["hr_active_status"]
            )
            return {
                "success": True,
                "engine": engine_used,
                "raw_text": parsed["jd_text"],
                "parsed": parsed,
                "veto": veto,
                "usage": usage,
            }
        except Exception:
            # 云端 VL 异常自动回退本地 Apple Vision
            pass

    raw_ocr_text = run_apple_vision_ocr(img_bytes, ext=ext)
    if not raw_ocr_text.strip():
        return {
            "success": False,
            "engine": engine_used,
            "detail": "未在截图中识别到有效文字，请确认图片清晰度。",
        }

    parsed = parse_raw_job_posting(raw_ocr_text)
    parsed["platform"] = "vision_ocr"
    veto = VetoEngine().evaluate(
        parsed["job_title"], parsed["company_name"], parsed["jd_text"], parsed["hr_active_status"]
    )
    return {
        "success": True,
        "engine": engine_used,
        "raw_text": raw_ocr_text,
        "parsed": parsed,
        "veto": veto,
        "usage": usage,
    }
