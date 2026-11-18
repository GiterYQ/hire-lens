import html
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

from .matcher import JobMatcher, apply_truth_guard
from .profile_loader import load_candidate_profile, resolve_resume_path

BASE_DIR = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = BASE_DIR / "data" / "artifacts"
CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _safe_slug(text: str, max_len: int = 18) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())
    return cleaned[:max_len].strip("_") or "目标企业"


def markdown_to_clean_html(md_text: str, highlight_keywords: list[str] | None = None) -> str:
    """将 Markdown 母本安全转换为紧凑一页 A4 排版 HTML，并对命中 JD 的技术/业务词做温和下划线强调"""
    kws = [k.strip() for k in (highlight_keywords or []) if k and len(k.strip()) >= 2]
    lines_out: list[str] = []
    in_ul = False

    def _fmt_inline(raw_s: str) -> str:
        esc = html.escape(raw_s)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        esc = re.sub(
            r"(https?://[^\s<)）]+)",
            r'<a href="\1" style="color:#0f766e;text-decoration:none;">\1</a>',
            esc,
        )
        for kw in kws[:8]:
            pat = re.compile(rf"(?<![\w>])({re.escape(html.escape(kw))})(?![\w<])", re.I)
            esc = pat.sub(r'<span class="jd-hit">\1</span>', esc, count=1)
        return esc

    for raw_ln in (md_text or "").splitlines():
        ln = raw_ln.strip()
        if not ln or ln == "---":
            if in_ul:
                lines_out.append("</ul>")
                in_ul = False
            continue
        if ln.startswith("# "):
            if in_ul:
                lines_out.append("</ul>")
                in_ul = False
            lines_out.append(f"<h1>{_fmt_inline(ln[2:])}</h1>")
        elif ln.startswith("## "):
            if in_ul:
                lines_out.append("</ul>")
                in_ul = False
            lines_out.append(f"<h2>{_fmt_inline(ln[3:])}</h2>")
        elif ln.startswith("### "):
            if in_ul:
                lines_out.append("</ul>")
                in_ul = False
            lines_out.append(f"<h3>{_fmt_inline(ln[4:])}</h3>")
        elif ln.startswith("> "):
            if in_ul:
                lines_out.append("</ul>")
                in_ul = False
            lines_out.append(f'<div class="target-callout">{_fmt_inline(ln[2:])}</div>')
        elif ln.startswith("- "):
            if not in_ul:
                lines_out.append("<ul>")
                in_ul = True
            lines_out.append(f"<li>{_fmt_inline(ln[2:])}</li>")
        else:
            if in_ul:
                lines_out.append("</ul>")
                in_ul = False
            lines_out.append(f"<p>{_fmt_inline(ln)}</p>")

    if in_ul:
        lines_out.append("</ul>")
    return "\n".join(lines_out)


def build_tailored_markdown(job: Dict[str, Any]) -> str:
    """
    严守真实事实边界的插槽式简历定制：
    绝不让大模型重写整份履历捏造数据，而是在已核验 4 赛道母本顶部注入针对目标岗位的核心匹配摘要与真实胜任依据。
    """
    track_key = job.get("selected_track") or "track_ai_builder"
    meta = JobMatcher.TRACK_META.get(track_key, JobMatcher.TRACK_META["track_ai_builder"])
    md_path = resolve_resume_path(meta["file"])
    cname = load_candidate_profile().get("candidate_name", "候选人")
    base_md = md_path.read_text(encoding="utf-8") if md_path.exists() else f"# {cname}\n\n{meta['name']}"

    company = job.get("company_name") or "贵司"
    title = job.get("job_title") or "目标岗位"
    adv = apply_truth_guard(job.get("profile_advantage") or "")
    reasons = [apply_truth_guard(r) for r in (job.get("fit_reasons") or [])][:3]

    summary_lines = [
        f"## 针对「{company} · {title}」核心胜任摘要",
    ]
    if adv:
        summary_lines.append(f"> **核心定位**：{adv}")
    for r in reasons:
        summary_lines.append(f"- {r}")

    summary_block = "\n".join(summary_lines)

    # 插入在第一个二级标题 `## ` 之前，保持姓名与联系方式在最顶部
    parts = base_md.split("\n## ", 1)
    if len(parts) == 2:
        return f"{parts[0].rstrip()}\n\n{summary_block}\n\n## {parts[1]}"
    return f"{base_md.rstrip()}\n\n{summary_block}\n"


def build_a4_standalone_html(job: Dict[str, Any], tailored_md: str) -> str:
    ats = job.get("ats_matrix") or {}
    matched_kws = ats.get("matched_keywords") or []
    body_html = markdown_to_clean_html(tailored_md, highlight_keywords=matched_kws)
    cname = load_candidate_profile().get("candidate_name", "候选人")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>{html.escape(cname)}_简历_{html.escape(job.get('company_name', ''))}</title>
<style>
  @page {{ size: A4; margin: 9mm 12mm; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    background: #ffffff;
    color: #1e293b;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    font-size: 9.0pt;
    line-height: 1.44;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }}
  .page {{
    width: 100%;
    max-width: 820px;
    margin: 0 auto;
    padding: 6mm 8mm;
    background: #ffffff;
  }}
  h1 {{
    font-size: 16pt;
    color: #0f172a;
    border-bottom: 1.5px solid #0f172a;
    padding-bottom: 4px;
    margin-bottom: 5px;
  }}
  h2 {{
    font-size: 10.2pt;
    color: #0f172a;
    border-bottom: 1px solid #cbd5e1;
    padding-bottom: 2px;
    margin-top: 8px;
    margin-bottom: 4px;
  }}
  h3 {{
    font-size: 9.3pt;
    color: #0f766e;
    margin-top: 5px;
    margin-bottom: 2px;
  }}
  p {{ margin-bottom: 3px; color: #334155; }}
  ul {{ padding-left: 15px; margin-bottom: 4px; }}
  li {{ margin-bottom: 2px; text-align: justify; }}
  .target-callout {{
    background: #f0fdfa;
    border-left: 3px solid #0d9488;
    padding: 5px 9px;
    border-radius: 4px;
    margin: 4px 0 5px;
    color: #134e4a;
    font-size: 8.8pt;
  }}
  .jd-hit {{
    background: #ecfeff;
    border-bottom: 1px solid #14b8a6;
    padding: 0 1px;
  }}
</style>
</head>
<body>
  <div class="page">
    {body_html}
  </div>
</body>
</html>"""


def render_job_resume_artifacts(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    使用无头 Chrome (--headless=new) 静默生成：
    1. A4 定制 PDF 简历（用于猎聘 / 51job / 智联 / 邮件附件投递）
    2. 高清 PNG 图片版简历（用于 BOSS 直聘聊天窗一键发图片简历）
    全程无任何桌面弹窗！
    """
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    tailored_md = job.get("tailored_resume_md") or build_tailored_markdown(job)
    html_doc = build_a4_standalone_html(job, tailored_md)

    job_id = job.get("id") or 0
    cname = _safe_slug(load_candidate_profile().get("candidate_name", "候选人"), max_len=10)
    slug = _safe_slug(f"{job.get('company_name', '')}_{job.get('job_title', '')}", max_len=24)
    pdf_path = ARTIFACTS_DIR / f"{cname}_定制简历_{slug}_#{job_id}.pdf"
    png_path = ARTIFACTS_DIR / f"{cname}_图片简历_{slug}_#{job_id}.png"

    if not os.path.exists(CHROME_BIN):
        return {
            "success": False,
            "tailored_resume_md": tailored_md,
            "pdf_path": "",
            "img_path": "",
            "message": "未检测到 Google Chrome，仅保存定制 Markdown 母本",
        }

    with tempfile.TemporaryDirectory() as tmp:
        html_file = Path(tmp) / "tailored_a4.html"
        html_file.write_text(html_doc, encoding="utf-8")
        file_url = html_file.as_uri()

        # 1. 无头渲染净版 A4 PDF
        subprocess.run(
            [
                CHROME_BIN,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                file_url,
            ],
            capture_output=True,
            timeout=25,
            check=False,
        )

        # 2. 无头渲染高清 PNG 图片简历（适配 BOSS 直聘图片发送尺寸）
        subprocess.run(
            [
                CHROME_BIN,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                "--window-size=880,1245",
                "--force-device-scale-factor=2",
                f"--screenshot={png_path}",
                file_url,
            ],
            capture_output=True,
            timeout=25,
            check=False,
        )

    ok_pdf = pdf_path.exists() and pdf_path.stat().st_size > 1000
    ok_png = png_path.exists() and png_path.stat().st_size > 1000

    return {
        "success": ok_pdf or ok_png,
        "tailored_resume_md": tailored_md,
        "pdf_path": str(pdf_path) if ok_pdf else "",
        "img_path": str(png_path) if ok_png else "",
        "message": "已静默生成一页 A4 PDF 与 BOSS 高清图片简历" if (ok_pdf and ok_png) else "部分简历产物渲染完成",
    }
