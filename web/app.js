import { ContinuousReader } from "./reader.js";
const $ = (id) => document.getElementById(id);
const route = new URLSearchParams(location.search);
const panelMode = route.get("panel") === "1";
if (panelMode) document.body.classList.add("panel-mode");
const state = {
  doc: null,
  page: 1,
  selection: null,
  job: null,
  mode: "text",
  jobs: [],
  videoJob: null,
};
const labels = {
  queued: "等待中",
  writing: "编写讲解",
  reviewing: "内容复核",
  rendering: "音画合成",
  ready: "已完成",
  failed: "生成失败",
  cancelled: "已取消",
};
let toastTimer,
  refreshBusy = false,
  jobListKey = "",
  offline = false;
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 6000);
}
async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      "X-Paper-Client": "1",
      ...(options.body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" }),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      data = { detail: "请求失败" };
    }
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  }
  return response.json();
}
function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function clock(s) {
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}
async function loadDocuments(selected) {
  const docs = await api("/documents");
  $("documentSelect").replaceChildren();
  for (const doc of docs) {
    const option = element("option", doc.title);
    option.value = doc.id;
    $("documentSelect").append(option);
  }
  if (docs.length) await loadDocument(selected || docs[0].id);
  else $("documentSelect").append(element("option", "尚未导入论文"));
}
async function loadDocument(id) {
  state.doc = await api("/documents/" + id);
  state.selection = null;
  state.job = null;
  state.videoJob = null;
  $("documentSelect").value = id;
  $("docMeta").textContent =
    `${state.doc.page_count} 页 · ${state.doc.text_pages} 页可点选文本`;
  $("pageCount").textContent = state.doc.page_count;
  $("pageNumber").max = state.doc.page_count;
  $("pageList").replaceChildren();
  for (const p of state.doc.pages) {
    const button = element("button");
    button.dataset.page = p.number;
    button.append(
      element("b", String(p.number).padStart(2, "0")),
      element(
        "span",
        p.blocks.find((b) => b.text.length > 8)?.text.split("\n")[0] ||
          "图像页",
      ),
    );
    button.onclick = () => showPage(p.number);
    $("pageList").append(button);
  }
  $("paperStage").style.display = "block";
  $("emptyPaper").hidden = true;
  await reader.load(state.doc);
  showPage(1);
  renderJob();
}
function updatePageNavigation(number) {
  state.page = number;
  $("pageNumber").value = number;
  $("pageBadge").textContent = `P. ${String(number).padStart(2, "0")}`;
  $("prev").disabled = number === 1;
  $("next").disabled = number === state.doc?.page_count;
  for (const b of $("pageList").children) b.classList.toggle("active", Number(b.dataset.page) === number);
}
function showPage(number) { reader.goto(number); }
const reader = new ContinuousReader({ api, onSelection: choose, onPage: updatePageNavigation, onError: toast,
  onPending: () => { state.selection = null; } });
function choose(selected) {
  state.selection = selected;
  reader.paint(selected);
}
function renderJob() {
  const job = state.job;
  $("jobStatus").replaceChildren();
  $("videoArea").hidden = !job || job.state !== "ready";
  if (!job || job.state !== "ready") {
    $("video").pause();
    state.videoJob = null;
  }
  if (!job) return;
  const title = element(
    "div",
    job.message,
    job.state === "ready" ? "success" : "",
  );
  $("jobStatus").append(title);
  if (!["ready", "failed", "cancelled"].includes(job.state)) {
    const progress = element("div", undefined, "progress"),
      stages = ["queued", "writing", "reviewing", "rendering"];
    for (let i = 0; i < 4; i++)
      progress.append(
        element(
          "i",
          undefined,
          i < stages.indexOf(job.state)
            ? "done"
            : i === stages.indexOf(job.state)
              ? "current"
              : "",
        ),
      );
    $("jobStatus").append(progress);
  }
  if (job.error) $("jobStatus").append(element("p", job.error, "error"));
  const actions = element("div", undefined, "status-actions");
  if (["failed", "cancelled"].includes(job.state)) {
    actions.append(element("p", "如需重新生成，请在主会话发送重试指令"));
  } else if (job.state !== "ready") {
    const cancel = element("button", "取消生成");
    cancel.onclick = () => jobAction("cancel");
    actions.append(cancel);
  }
  $("jobStatus").append(actions);
  if (job.state === "ready" && state.videoJob !== job.id) {
    state.videoJob = job.id;
    $("lessonTitle").textContent = job.title;
    const root = `/api/jobs/${job.id}/files/`;
    const revision = encodeURIComponent(job.updated || job.created);
    $("video").pause();
    $("video").src = root + "lesson.mp4?v=" + revision;
    $("video").poster = root + "poster.png?v=" + revision;
    $("video").replaceChildren();
    $("video").load();
    $("downloadVideo").href = root + "lesson.mp4";
    $("downloadCaptions").href = root + "captions.srt";
    $("downloadPlan").href = root + "plan.json";
    $("chapters").replaceChildren();
    for (const chapter of job.result.timeline) {
      const button = element("button", undefined, "chapter");
      button.append(
        element("span", clock(chapter.start)),
        document.createTextNode(chapter.title),
      );
      button.onclick = () => {
        $("video").currentTime = chapter.start;
        $("video")
          .play()
          .catch(() => {});
      };
      $("chapters").append(button);
    }
    $("flowReplay").hidden = !(job.result.flow_steps?.length);
    $("flowStepList").replaceChildren();
    for (const step of job.result.flow_steps || []) {
      const button = element("button", `${clock(step.start)} · ${step.title}`, "chapter");
      button.onclick = () => {
        $("video").pause();
        $("video").currentTime = step.pause_at;
      };
      $("flowStepList").append(button, element("small", `输入：${step.input} → 输出：${step.output}；条件：${step.condition}；${step.evidence}`));
    }
    $("evidence").replaceChildren(
      element(
        "p",
        `${job.validation.covered_points}/${job.validation.knowledge_points} 个知识点已覆盖 · 引用定位通过 · 模型内容复核通过（仍需读者判断）`,
      ),
    );
    for (const point of job.plan.knowledge_points) {
      $("evidence").append(
        element("h4", point.concept),
        element("p", point.explanation),
      );
      if (point.type !== "source")
        $("evidence").append(
          element(
            "small",
            point.type === "inference" ? "讲解推断" : "背景知识",
          ),
        );
      for (const source of point.evidence) {
        const link = element(
          "button",
          `查看第 ${source.page} 页`,
          "source-link",
        );
        link.onclick = () => {
          if (panelMode)
            window.open(
              `/?job=${job.id}&page=${source.page}`,
              "_blank",
              "noopener",
            );
          else showPage(source.page);
        };
        $("evidence").append(link, element("blockquote", source.quote));
      }
    }
    if (job.plan.limitations.length)
      $("evidence").append(
        element("h4", "适用边界"),
        element("p", job.plan.limitations.join("；")),
      );
  }
}
async function jobAction(action) {
  try {
    state.job = await api(`/jobs/${state.job.id}/${action}`, {
      method: "POST",
    });
    renderJob();
    await refreshJobs();
  } catch (e) {
    toast(e.message);
  }
}
async function refreshJobs() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
    state.jobs = await api("/jobs");
    const jobs = state.jobs.filter((j) => j.document_id === state.doc?.id);
    $("jobCount").textContent = jobs.length;
    const nextKey =
      JSON.stringify(
        jobs.map((j) => [j.id, j.state, j.title, j.result?.duration]),
      ) + state.job?.id;
    if (nextKey !== jobListKey) {
      jobListKey = nextKey;
      $("jobList").replaceChildren();
      for (const job of jobs) {
        const button = element(
          "button",
          undefined,
          "job-item" + (job.id === state.job?.id ? " active" : ""),
        );
        button.append(
          element("span", job.state === "ready" ? "▷" : "↗", "job-icon"),
        );
        const text = element("div");
        text.append(
          element("strong", job.title),
          element(
            "small",
            `第 ${job.selection.page} 页 · ${labels[job.state]}${job.result ? " · " + clock(job.result.duration) : ""}`,
          ),
        );
        button.append(text);
        button.onclick = () => selectJob(job);
        $("jobList").append(button);
      }
    }
    if (state.job) {
      const current =
        state.jobs.find((j) => j.id === state.job.id) ||
        (await api("/jobs/" + state.job.id));
      if (current && JSON.stringify(current) !== JSON.stringify(state.job)) {
        state.job = current;
        renderJob();
      }
    }
    if (offline) {
      offline = false;
      renderJob();
    }
  } catch (e) {
    offline = true;
    $("jobStatus").textContent = "本地服务暂时不可达，正在等待恢复";
  } finally {
    refreshBusy = false;
  }
}
function mode(value) {
  state.mode = value;
  for (const name of ["text", "paragraph", "region"]) $(name + "Mode").classList.toggle("active", name === value);
  reader.setMode(value);
  $("readerHint").textContent = value === "text" ? "拖动选择任意文字，可跨页连续选择；也可用 Shift + 方向键调整选区" : value === "paragraph" ? "点击原文中的完整段落查看选区" : "在任意一页拖出矩形，框选公式或架构图";
}
$("textMode").onclick = () => mode("text");
$("paragraphMode").onclick = () => mode("paragraph");
$("regionMode").onclick = () => mode("region");
$("prev").onclick = () => showPage(state.page - 1);
$("next").onclick = () => showPage(state.page + 1);
$("pageNumber").onchange = (e) => showPage(e.target.value);
$("uploadButton").onclick = () => $("upload").click();
$("documentSelect").onchange = (e) =>
  loadDocument(e.target.value).catch((e) => toast(e.message));
$("upload").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  $("uploadButton").disabled = true;
  $("uploadButton").textContent = "正在解析论文…";
  try {
    const doc = await api("/documents", { method: "POST", body: form });
    await loadDocuments(doc.id);
    await refreshJobs();
    toast(`已导入 ${doc.page_count} 页`);
  } catch (error) {
    toast(error.message);
  } finally {
    $("uploadButton").disabled = false;
    $("uploadButton").textContent = "＋ 导入论文 PDF";
    e.target.value = "";
  }
};
async function selectJob(job) {
  if (state.doc?.id !== job.document_id) await loadDocument(job.document_id);
  state.job = job;
  state.selection = {
    page: job.selection.page,
    block_id:
      job.selection.text_range || job.selection.id.startsWith("pasted") || job.selection.id === "region"
        ? null
        : job.selection.id,
    bbox:
      job.selection.id === "region" || job.selection.input_mode === "chat"
        ? job.selection.bbox
        : null,
    text: job.selection.text,
    text_range: job.selection.text_range,
    source_pages: job.selection.source_pages,
  };
  showPage(job.selection.page);
  renderJob();
  const url = new URL(location.href);
  url.searchParams.set("job", job.id);
  history.replaceState(null, "", url);
  $("openStudio").href = "/?job=" + job.id;
}
try {
  if (route.get("job")) {
    const job = await api("/jobs/" + encodeURIComponent(route.get("job")));
    await loadDocuments(job.document_id);
    await selectJob(job);
    if (route.get("page")) showPage(route.get("page"));
  } else {
    await loadDocuments();
  }
} catch (e) {
  toast(e.message);
  $("jobStatus").textContent = "无法加载视频任务：" + e.message;
}
await refreshJobs();
setInterval(refreshJobs, 3000);
