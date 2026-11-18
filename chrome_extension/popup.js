function extractJobFromPage() {
  const q = sel => {
    const el = document.querySelector(sel);
    return el ? el.innerText.trim() : "";
  };

  const url = window.location.href;
  let job_title = "";
  let company_name = "";
  let salary = "";
  let hr_active_status = "今日活跃";
  let jd_text = "";
  let platform = "manual";

  if (url.includes("zhipin.com")) {
    platform = "boss";
    job_title = q(".info-primary .name h1") || q(".job-banner .name h1") || q("h1");
    salary = q(".info-primary .salary") || q(".job-banner .salary");
    company_name = q(".sider-company .company-info a[ka*='brand']") || q(".company-info .name") || q(".level-list .company-name");
    hr_active_status = q(".boss-active-time") || q(".boss-online-tag") || "今日活跃";
    jd_text = q(".job-sec-text") || q(".job-detail-section");
  } else if (url.includes("liepin.com")) {
    platform = "liepin";
    job_title = q(".name-box .name") || q("h1");
    salary = q(".name-box .salary");
    company_name = q(".company-info-container .name") || q("aside .company-name");
    jd_text = q(".job-intro-container") || q("dd[data-selector='job-intro-content']");
  } else if (url.includes("51job.com")) {
    platform = "51job";
    job_title = q(".cn h1") || q(".job-header h1") || q("h1");
    salary = q(".cn strong") || q(".job-header .salary");
    company_name = q(".cname a") || q(".com_name");
    jd_text = q(".bmsg.job_msg") || q(".job_msg");
  } else if (url.includes("zhaopin.com")) {
    platform = "zhilian";
    job_title = q(".summary-plane__title") || q("h1");
    salary = q(".summary-plane__salary");
    company_name = q(".company__title") || q(".company-name");
    jd_text = q(".describtion__detail-content") || q(".pos-ul");
  } else if (url.includes("jobsdb.com")) {
    platform = "jobsdb";
    job_title = q("[data-automation='job-detail-title']") || q("h1");
    company_name = q("[data-automation='advertiser-name']");
    salary = q("[data-automation='job-detail-salary']");
    jd_text = q("[data-automation='jobAdDetails']");
  } else if (url.includes("linkedin.com")) {
    platform = "linkedin";
    job_title = q(".job-details-jobs-unified-top-card__job-title") || q("h1");
    company_name = q(".job-details-jobs-unified-top-card__company-name");
    jd_text = q("#job-details") || q(".jobs-description__content");
  }

  const raw_text = document.body ? document.body.innerText.slice(0, 8000) : "";
  return {
    job_title,
    company_name,
    salary,
    hr_active_status,
    job_url: url,
    platform,
    jd_text,
    raw_text
  };
}

function injectGreetingToChat(text) {
  const box =
    document.querySelector("#chat-input") ||
    document.querySelector(".chat-input") ||
    document.querySelector("[contenteditable='true']") ||
    document.querySelector("textarea");
  if (!box) return false;
  box.focus();
  if (box.tagName === "TEXTAREA" || box.tagName === "INPUT") {
    box.value = text;
    box.dispatchEvent(new Event("input", { bubbles: true }));
  } else {
    box.innerText = text;
    box.dispatchEvent(new InputEvent("input", { bubbles: true }));
  }
  return true;
}

document.getElementById("btn-clip").addEventListener("click", async () => {
  const btn = document.getElementById("btn-clip");
  const status = document.getElementById("status");
  btn.disabled = true;
  status.innerText = "⏳ 正在提取页面并发送至本地 HireLens 脱敏评估...";

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractJobFromPage
    });

    result.language = document.getElementById("lang").value;

    const resp = await fetch("http://127.0.0.1:8765/api/clip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result)
    });
    const data = await resp.json();
    if (!resp.ok || !data.success) {
      throw new Error(data.detail || "后端返回异常");
    }

    const job = data.job;
    document.getElementById("res-box").classList.remove("hidden");
    document.getElementById("res-job").innerText = `${job.company_name} · ${job.job_title}`;
    document.getElementById("res-track").innerText = `母本: ${data.eval.track_name || job.selected_track}`;
    document.getElementById("res-score").innerText = job.match_score;
    document.getElementById("res-veto").innerText = job.veto_report?.advice || "";
    document.getElementById("res-greeting").value = job.greeting_final || job.greeting_draft || "";
    document.getElementById("res-adv").value = job.profile_advantage || data.eval.profile_advantage || "";
    status.innerText = "✅ 已生成双文案并入库本地 SQLite！";
  } catch (err) {
    status.innerText = "❌ 连接失败：" + err.message;
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("btn-copy").addEventListener("click", async () => {
  const txt = document.getElementById("res-greeting").value;
  await navigator.clipboard.writeText(txt);
  document.getElementById("status").innerText = "✅ 已复制破冰语到剪贴板！";
});

document.getElementById("btn-copy-adv").addEventListener("click", async () => {
  const txt = document.getElementById("res-adv").value;
  await navigator.clipboard.writeText(txt);
  document.getElementById("status").innerText = "✅ 已复制个人优势，可直接粘贴至BOSS在线简历！";
});

document.getElementById("btn-fill").addEventListener("click", async () => {
  const txt = document.getElementById("res-greeting").value;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: injectGreetingToChat,
    args: [txt]
  });
  document.getElementById("status").innerText = result
    ? "✅ 已自动填入当前页聊天输入框（请人工检查后点发送）"
    : "⚠️ 当前页未找到打开的聊天输入框，已建议直接复制粘贴";
});
