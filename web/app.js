/* vel（VideoEnglishLearner）前端：单文件无构建。
 *
 * 两个视图（库 / 播放器）。播放器是重点：
 * - 用 requestAnimationFrame 读 video.currentTime，不用 timeupdate（那个只有 4Hz，高亮会一顿一顿）。
 * - 当前行 = 「最后一个 start_ms <= t 的行」，二分查找。不用 [start,end) 区间判断，
 *   否则行与行之间的空隙会闪一下没有高亮。
 * - 点击字幕行跳转时往前预卷 150ms，不然开头那个词会被吞掉。
 */

const API = "/api";
const ACTIVE = ["queued", "downloading", "transcribing", "segmenting", "analyzing"];
const STATUS_TEXT = {
  queued: "排队中",
  downloading: "下载中",
  transcribing: "字幕中",
  segmenting: "切分中",
  analyzing: "解析中",
  "media-only": "只有视频",
  ready: "就绪",
  failed: "失败",
};

/* 解析状态：none 未解析，queued/running 进行中，done 完成，failed 失败 */
const ANALYSIS_ACTIVE = ["queued", "running"];
const SUBS_MODES = ["bilingual", "en-only", "write"];
const SUBS_MODE_TEXT = {
  bilingual: "双语",
  "en-only": "只听英文（遮住中文）",
  write: "遮住英文（听写）",
};
const SUBS_MODE_KEY = "vel.subs-mode";

const state = {
  videos: [],
  current: null,
  segments: [],
  lineEls: [],
  nowSpans: [],
  activeIdx: -1,
  activeWord: -1,
  wordEl: null,
  nowWordEl: null,
  pollTimer: null,
  saveTimer: null,
  follow: true,
  ab: { a: null, b: null },
  replayUntil: null,
  // 刚点过的那一句：预卷的 150ms 里 tick 保持它，别让面板/highlight 抖回上一句
  seekLine: -1,
  settings: null,
  // 当前视频的解析数据：{ status, done, total, model, error, items }
  analysis: null,
  analysisTimer: null,
  analysisPk: null,
  analysisDoneShown: -1,
  subsMode: "bilingual",
  health: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ 工具 */

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtTime(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

/** 词与词之间要不要空格（标点前不加、左括号后不加） */
function needsSpace(prev, next) {
  if (!prev) return false;
  if (/^[,.;:!?%)\]}]/.test(next)) return false;
  if (/[(\[{]$/.test(prev)) return false;
  return true;
}

function clampPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(100, Math.round(n)));
}

function toast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hidden"), kind === "err" ? 6000 : 3000);
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (_) { /* 保持默认 */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

/* ------------------------------------------------------------------ 库视图 */

/** 解析状态徽标：只给已就绪的视频显示；未解析也给个灰的提示 */
function analysisBadgeHtml(v) {
  const status = v.analysis_status || "none";
  if (status === "none") return `<span class="badge">未解析</span>`;
  const done = Number(v.analysis_done) || 0;
  const total = Number(v.analysis_total) || 0;
  if (status === "done") {
    return `<span class="badge ready">${total ? `已解析 ${total}/${total}` : "已解析"}</span>`;
  }
  if (status === "failed") return `<span class="badge failed">解析失败</span>`;
  return `<span class="badge active">${total ? `解析中 ${done}/${total}` : "解析中"}</span>`;
}

function renderLibrary() {
  const list = $("video-list");
  const empty = $("empty-hint");
  list.innerHTML = "";

  if (!state.videos.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const frag = document.createDocumentFragment();
  for (const v of state.videos) {
    const ready = v.status === "ready";
    const working = ACTIVE.includes(v.status);
    const card = document.createElement("div");
    card.className = `card ${ready ? "ready" : "not-ready"}`;
    card.dataset.pk = v.id;

    const thumb = document.createElement("div");
    thumb.className = "thumb";
    if (v.thumb) thumb.style.backgroundImage = `url("${v.thumb}")`;
    if (v.duration_ms) {
      const dur = document.createElement("span");
      dur.className = "duration";
      dur.textContent = fmtTime(v.duration_ms);
      thumb.appendChild(dur);
    }
    card.appendChild(thumb);

    const body = document.createElement("div");
    body.className = "body";
    body.innerHTML =
      `<div class="title">${esc(v.title || v.video_id || "（无标题）")}</div>` +
      `<div class="channel">${esc(v.channel || "")}</div>`;
    card.appendChild(body);

    // 进度条：处理中显示实时进度，就绪了显示满格（完成）
    if (working || ready) {
      const pct = ready ? 100 : clampPct(v.progress);
      const prog = document.createElement("div");
      prog.className = `progress-wrap${ready ? " done" : ""}`;
      prog.innerHTML =
        `<div class="progress-bar"><i style="width:${pct}%"></i></div>` +
        `<div class="progress-meta">` +
        `<span>${esc(v.detail || STATUS_TEXT[v.status] || "")}</span>` +
        `<span>${pct}%</span>` +
        `</div>`;
      card.appendChild(prog);
    }

    const segCount = Number(v.segments_count) || 0;
    const hasMedia = Boolean(v.media_path);

    const foot = document.createElement("div");
    foot.className = "foot";
    const badgeCls = ready ? "ready" : v.status === "failed" ? "failed" : "active";
    // 进度条那行已经写了 detail 和百分比，这里不再重复，只留来源/失败信息
    const detailText = ready ? (v.source || "") : working ? "" : (v.detail || "");
    foot.innerHTML =
      `<span class="badge ${badgeCls}">${STATUS_TEXT[v.status] || v.status}</span>` +
      (ready ? analysisBadgeHtml(v) : "") +
      `<span class="spacer"></span>` +
      (detailText ? `<span class="detail">${esc(detailText)}</span>` : "");

    if (v.status === "failed") {
      const retry = document.createElement("button");
      retry.className = "icon-btn";
      retry.type = "button";
      retry.dataset.retry = v.id;
      // 视频还在、只差字幕时，重试其实只补字幕，标签直接说清楚
      retry.textContent = hasMedia && segCount === 0 ? "补字幕" : "重试";
      retry.style.color = "var(--warn)";
      foot.appendChild(retry);
    }
    const del = document.createElement("button");
    del.className = "icon-btn";
    del.type = "button";
    del.dataset.del = v.id;
    del.textContent = "删除";
    foot.appendChild(del);
    card.appendChild(foot);

    // 视频 / 字幕分开下载 + 只缺字幕时的「补字幕」入口
    const canPlay = ready || v.status === "media-only";
    // 有视频但一行字幕都没有：只剩字幕没搞定（failed 走下面的「补字幕」按钮，不重复出）
    const needSubs = segCount === 0 && hasMedia && (v.status === "media-only" || ready);
    const acts = [];
    if (hasMedia && !working && (canPlay || v.status === "failed")) {
      acts.push(`<a class="icon-btn" href="/api/videos/${v.id}/media" download>下载视频</a>`);
    }
    if (segCount > 0 && !working) {
      acts.push(`<a class="icon-btn" href="/api/videos/${v.id}/subtitle-file" download>下载字幕</a>`);
    }
    if (needSubs && !working) {
      acts.push(`<button class="icon-btn subs-btn" type="button" data-subs="${v.id}">补字幕</button>`);
    }
    if (acts.length) {
      const row = document.createElement("div");
      row.className = "actions";
      row.innerHTML = acts.join("");
      card.appendChild(row);
    }

    if (v.status === "failed" && v.error) {
      const err = document.createElement("div");
      err.className = "error";
      err.textContent = v.error.slice(0, 200);
      card.appendChild(err);
    }
    frag.appendChild(card);
  }
  list.appendChild(frag);
}

async function loadLibrary() {
  try {
    state.videos = await api("/videos");
  } catch (err) {
    toast(`读取列表失败：${err.message}`, "err");
    return;
  }
  renderLibrary();
  updateHealth();
  schedulePoll();
}

function updateHealth() {
  const el = $("health");
  const active = state.videos.filter((v) => ACTIVE.includes(v.status));
  if (active.length) {
    el.className = "health busy";
    el.innerHTML = `<span class="dot"></span>处理中 ${active.length}`;
  } else {
    el.className = "health ok";
    el.innerHTML = `<span class="dot"></span>就绪`;
  }
}

function hasRunningAnalysis() {
  return state.videos.some((v) => ANALYSIS_ACTIVE.includes(v.analysis_status));
}

function schedulePoll() {
  const need = state.videos.some((v) => ACTIVE.includes(v.status)) || hasRunningAnalysis();
  if (need && !state.pollTimer) {
    state.pollTimer = setInterval(pollLibrary, 2000);
  } else if (!need && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

async function pollLibrary() {
  let videos;
  try {
    videos = await api("/videos");
  } catch (_) {
    return;
  }
  state.videos = videos;
  renderLibrary();
  updateHealth();
  schedulePoll();
  refreshHealth();

  // 当前打开的视频刚好处理完了：把字幕补上
  if (state.current) {
    const fresh = videos.find((v) => v.id === state.current.id);
    if (fresh && fresh.status === "ready" && !state.segments.length) {
      await openVideo(fresh.id, true);
    }
  }
}

async function addVideo(url) {
  const value = (url || "").trim();
  if (!value) return;
  // 添加模式：视频 + 字幕（默认）/ 只取字幕（快）/ 只取视频
  const mode = ($("add-mode") && $("add-mode").value) || "video+subs";
  const button = $("add-form").querySelector("button");
  button.disabled = true;
  button.textContent = "解析…";
  try {
    const data = await api("/videos", { method: "POST", body: JSON.stringify({ url: value, mode }) });
    $("url-input").value = "";
    toast(
      mode === "subs-only" ? "已加入队列：只取字幕，几秒就好" : mode === "video-only" ? "已加入队列：只取视频" : "已加入队列，看进度就行",
      "ok"
    );
    await loadLibrary();
    if (data && data.id) {
      const row = state.videos.find((v) => v.id === data.id);
      if (row && row.status === "ready") openVideo(data.id);
    }
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = "添加";
  }
}

async function deleteVideo(pk) {
  const row = state.videos.find((v) => v.id === pk);
  const name = (row && (row.title || row.video_id)) || pk;
  if (!confirm(`删除「${name}」？视频文件和字幕都会删掉。`)) return;
  try {
    await api(`/videos/${pk}`, { method: "DELETE" });
    toast("已删除", "ok");
    await loadLibrary();
  } catch (err) {
    toast(`删除失败：${err.message}`, "err");
  }
}

async function retryVideo(pk) {
  try {
    // 后端现在返回 {ok, kind}：ingest=重跑整个流程，subs=只补字幕，noop=已就绪不用重试
    const data = await api(`/videos/${pk}/retry`, { method: "POST" });
    const kind = (data && data.kind) || "ingest";
    if (kind === "noop") toast("这个视频已经就绪，不用重试", "");
    else if (kind === "subs") toast("已重新排队：只补字幕", "ok");
    else toast("已重新排队", "ok");
    await loadLibrary();
  } catch (err) {
    toast(`重试失败：${err.message}`, "err");
  }
}

/* 视频已下载好、只缺字幕时：只重新取字幕，不重下视频 */
async function requestSubs(pk) {
  try {
    await api(`/videos/${pk}/subs`, { method: "POST" });
    toast("开始补字幕，看进度就行", "ok");
    await loadLibrary();
  } catch (err) {
    if (err && err.status === 409) toast("已经在处理了，等进度就行", "");
    else toast(`补字幕失败：${err.message}`, "err");
  }
}

/* ---------------------------------------------------------------- 播放器 */

function showView(name) {
  $("library-view").classList.toggle("hidden", name !== "library");
  $("player-view").classList.toggle("hidden", name !== "player");
  $("settings-view").classList.toggle("hidden", name !== "settings");
}

async function openVideo(pk, quiet = false) {
  stopAnalysisPoll();
  let video;
  let subs;
  try {
    video = await api(`/videos/${pk}`);
    subs = await api(`/videos/${pk}/subtitles`);
  } catch (err) {
    toast(`打开失败：${err.message}`, "err");
    return;
  }
  if (!video.media_path && video.status !== "ready") {
    toast("这个视频还没处理完，等进度跑完再打开", "err");
    return;
  }

  state.current = video;
  state.segments = subs.segments || [];
  state.activeIdx = -1;
  clearWordHighlight();
  state.seekLine = -1;
  state.ab = { a: null, b: null };
  state.replayUntil = null;
  state.analysis = normalizeAnalysis(null);
  state.analysisDoneShown = -1;

  $("video-title").textContent = video.title || video.video_id || "";
  const parts = [];
  if (video.channel) parts.push(video.channel);
  if (video.duration_ms) parts.push(fmtTime(video.duration_ms));
  if (video.source) parts.push(video.source);
  parts.push(`${state.segments.length} 行字幕`);
  $("video-sub").textContent = parts.join(" · ");

  // 播放页也放一份下载入口：视频和字幕分开取
  const dlVideo = $("dl-video");
  const dlSubs = $("dl-subs");
  dlVideo.href = `/api/videos/${pk}/media`;
  dlVideo.hidden = !video.media_path;
  dlSubs.href = `/api/videos/${pk}/subtitle-file`;
  dlSubs.hidden = !(Number(video.segments_count) > 0 || state.segments.length > 0);
  $("player-actions").hidden = dlVideo.hidden && dlSubs.hidden;

  // 解析接口先拉一次：后端没实现 / 没解析记录时按「未解析」处理，不影响观看
  await loadAnalysis(pk);
  renderSubtitles();
  renderAnalysisPanel(-1);
  showView("player");

  // 解析可能早就在后台跑了：打开视频就直接接上轮询，不用再点一次
  if (ANALYSIS_ACTIVE.includes((state.analysis && state.analysis.status) || "none")) {
    startAnalysisPoll(pk);
  }

  const el = $("video");
  const resumeMs = (video.playback && video.playback.position_ms) || 0;
  const rate = (video.playback && video.playback.rate) || 1;

  el.playbackRate = rate;
  el.addEventListener("loadedmetadata", () => {
    if (resumeMs > 1000 && resumeMs < (video.duration_ms || Infinity) - 2000) {
      el.currentTime = resumeMs / 1000;
      toast(`已续播到 ${fmtTime(resumeMs)}`, "ok");
    }
  }, { once: true });
  el.src = `/api/videos/${pk}/media`;
  el.load();

  startLoop();
  if (!quiet) {
    $("subtitle-list").scrollTop = 0;
  }
  refreshRateBadge();
}

function backToLibrary() {
  const el = $("video");
  savePlayback();
  el.pause();
  el.removeAttribute("src");
  el.load();
  stopAnalysisPoll();
  state.current = null;
  state.segments = [];
  state.analysis = null;
  state.analysisDoneShown = -1;
  state.lineEls = [];
  state.nowSpans = [];
  state.activeIdx = -1;
  state.seekLine = -1;
  stopLoop();
  showView("library");
  renderTaskBar();
  loadLibrary();
}

function wordListFor(seg) {
  if (seg.words && seg.words.length) return seg.words;
  return [{ text: seg.text, start_ms: seg.start_ms, end_ms: seg.end_ms }];
}

function appendWords(container, words, highlightIndex = -1) {
  const spans = [];
  let prev = "";
  words.forEach((w, i) => {
    if (needsSpace(prev, w.text)) container.appendChild(document.createTextNode(" "));
    const span = document.createElement("span");
    span.className = "w";
    if (i === highlightIndex) span.classList.add("on");
    span.textContent = w.text;
    container.appendChild(span);
    spans.push(span);
    prev = w.text;
  });
  return spans;
}

/** 当前视频某一句的解析条目（后端 items 以 seq 为键） */
function analysisItemFor(seg, idx) {
  const items = state.analysis && state.analysis.items;
  if (!items || !seg) return null;
  const key = String(seg.seq ?? idx ?? "");
  return items[key] || null;
}

function translationFor(seg, idx) {
  const item = analysisItemFor(seg, idx);
  return (item && item.translation) || "";
}

function renderSubtitles() {
  const list = $("subtitle-list");
  list.innerHTML = "";
  state.lineEls = [];

  const frag = document.createDocumentFragment();
  state.segments.forEach((seg, i) => {
    const div = document.createElement("div");
    div.className = "line";
    div.dataset.i = String(i);

    const t = document.createElement("span");
    t.className = "t";
    t.textContent = fmtTime(seg.start_ms);
    div.appendChild(t);

    const spans = appendWords(div, wordListFor(seg));

    // 双语：英文行下面挂中文译文；有没有译文由解析状态决定
    const zh = translationFor(seg, i);
    if (zh) {
      const zhEl = document.createElement("div");
      zhEl.className = "zh";
      zhEl.textContent = zh;
      div.appendChild(zhEl);
    }

    frag.appendChild(div);
    state.lineEls.push({ el: div, spans });
  });
  list.appendChild(frag);
  $("subs-count").textContent = `${state.segments.length} 行`;

  const now = $("now-line");
  now.innerHTML = "";
  now.textContent = "点拨过的行会出现在这里";
}

function idxForTime(ms) {
  const arr = state.segments;
  let lo = 0;
  let hi = arr.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid].start_ms <= ms) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

function wordIdxForTime(seg, ms) {
  const words = wordListFor(seg);
  let lo = 0;
  let hi = words.length - 1;
  let found = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid].start_ms <= ms) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

function setActiveLine(idx) {
  if (idx === state.activeIdx) return;
  const prev = state.lineEls[state.activeIdx];
  if (prev) prev.el.classList.remove("on");

  state.activeIdx = idx;
  clearWordHighlight();
  const now = $("now-line");
  now.innerHTML = "";
  state.nowSpans = [];

  if (idx < 0 || !state.lineEls[idx]) {
    renderAnalysisPanel(-1);
    return;
  }

  const item = state.lineEls[idx];
  item.el.classList.add("on");
  state.nowSpans = appendWords(now, wordListFor(state.segments[idx]));

  if (state.follow) {
    const list = $("subtitle-list");
    const target = item.el.offsetTop - list.clientHeight / 2 + item.el.offsetHeight / 2;
    list.scrollTop = Math.max(0, target);
  }

  // 点/播到哪一行，解析面板就跟到哪一行
  renderAnalysisPanel(idx);
}

/** 清掉当前高亮的词（列表里的 + 顶部当前句里的） */
function clearWordHighlight() {
  if (state.wordEl) state.wordEl.classList.remove("on");
  if (state.nowWordEl) state.nowWordEl.classList.remove("on");
  state.wordEl = null;
  state.nowWordEl = null;
  state.activeWord = -1;
}

/**
 * 高亮第 idx 行里的第 wordIdx 个词；wordIdx = -1 表示这一句此刻没有“正在念”的词
 * （句首之前、句末之后都属于这种状态，播完就该灭）。
 */
function setActiveWord(idx, wordIdx) {
  const item = state.lineEls[idx];
  if (!item) return;
  const target = wordIdx >= 0 ? item.spans[wordIdx] || null : null;
  const nowTarget = wordIdx >= 0 ? state.nowSpans[wordIdx] || null : null;
  if (target === state.wordEl && nowTarget === state.nowWordEl) return;

  clearWordHighlight();
  if (target) {
    target.classList.add("on");
    state.wordEl = target;
    state.activeWord = wordIdx;
  }
  if (nowTarget) {
    nowTarget.classList.add("on");
    state.nowWordEl = nowTarget;
  }
}

let rafId = null;

function startLoop() {
  if (rafId) return;
  const step = () => {
    rafId = requestAnimationFrame(step);
    tick();
  };
  rafId = requestAnimationFrame(step);
}

function stopLoop() {
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
}

function tick() {
  const el = $("video");
  if (!state.segments.length) return;
  const ms = el.currentTime * 1000;

  let idx = idxForTime(ms);
  // 刚 seek 过：预卷还没播到目标句开头时，别让 tick 把高亮/面板改回上一句
  if (state.seekLine >= 0 && state.segments[state.seekLine]) {
    const seekStart = state.segments[state.seekLine].start_ms;
    if (ms >= seekStart) state.seekLine = -1;
    else idx = state.seekLine;
  }
  if (idx !== state.activeIdx) setActiveLine(idx);
  if (idx >= 0) {
    const seg = state.segments[idx];
    const words = wordListFor(seg);
    let wi = wordIdxForTime(seg, ms);
    const w = words[wi];
    // 已经念过这个词（或还没念到）就不高亮：播放到句尾时最后一个词要跟着灭掉
    if (!w || ms < w.start_ms || ms >= w.end_ms) wi = -1;
    if (wi !== state.activeWord) setActiveWord(idx, wi);
  }

  // 复读到达终点就停
  if (state.replayUntil != null && ms >= state.replayUntil) {
    state.replayUntil = null;
    el.pause();
  }

  // AB 循环
  if (state.ab.a != null && state.ab.b != null && ms >= state.ab.b) {
    state.seekLine = -1;
    el.currentTime = state.ab.a / 1000;
  }

  // 每 5 秒存一次进度
  if (!state.saveTimer && !el.paused) {
    state.saveTimer = setTimeout(() => {
      state.saveTimer = null;
      savePlayback();
    }, 5000);
  }
}

async function savePlayback() {
  if (!state.current) return;
  const el = $("video");
  if (!el.currentSrc) return;
  try {
    await fetch(`${API}/videos/${state.current.id}/playback`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        position_ms: Math.round(el.currentTime * 1000),
        rate: el.playbackRate,
      }),
      keepalive: true,
    });
  } catch (_) { /* 存不上不影响观看 */ }
}

/* --------------------------------------------------------------- 播放操作 */

function seekTo(ms, { play = false, preRoll = 150 } = {}) {
  const el = $("video");
  const target = Math.max(0, (ms - preRoll) / 1000);
  el.currentTime = target;
  if (play) {
    const p = el.play();
    // 预卷时被 pause 打断是正常现象（浏览器会 reject play() 的 promise），别让它污染控制台
    if (p && p.catch) p.catch(() => {});
  }
  // 立刻把高亮和解析面板切到目标句：暂停时 tick 不会自己走到那一句，
  // 否则点了字幕行，面板还停在空态或上一句。
  const idx = idxForTime(ms);
  state.seekLine = idx;
  setActiveLine(idx);
}

function currentLineIndex() {
  return state.activeIdx >= 0 ? state.activeIdx : idxForTime($("video").currentTime * 1000);
}

function jumpLine(delta) {
  const cur = currentLineIndex();
  const next = Math.min(state.segments.length - 1, Math.max(0, cur + delta));
  if (next === cur && delta !== 0) return;
  seekTo(state.segments[next].start_ms);
}

function replayLine() {
  const idx = currentLineIndex();
  if (idx < 0) return;
  const seg = state.segments[idx];
  const el = $("video");
  el.currentTime = Math.max(0, (seg.start_ms - 50) / 1000);
  const p = el.play();
  if (p && p.catch) p.catch(() => {});
  state.seekLine = idx;
  state.replayUntil = seg.end_ms + 200;
}

function toggleRate() {
  const el = $("video");
  el.playbackRate = el.playbackRate === 1 ? 0.75 : 1;
  refreshRateBadge();
  savePlayback();
}

function refreshRateBadge() {
  const el = $("video");
  const hints = document.querySelector(".hint-row");
  if (!hints) return;
  let badge = document.getElementById("rate-badge");
  if (!badge) {
    badge = document.createElement("span");
    badge.id = "rate-badge";
    hints.appendChild(badge);
  }
  badge.textContent = `当前 ${el.playbackRate.toFixed(2)}×`;
  badge.style.color = el.playbackRate === 1 ? "" : "var(--accent)";
}

function setAB(which) {
  const el = $("video");
  const ms = Math.round(el.currentTime * 1000);
  state.ab[which] = ms;
  toast(`${which.toUpperCase()} 点：${fmtTime(ms)}（${which === "a" ? "再按 B" : "开始循环"}）`, "ok");
}

function clearAB() {
  state.ab = { a: null, b: null };
  toast("已清除 AB 循环", "ok");
}

/* ---------------------------------------------------------- 全局任务指示 */

/** 拉一次 /api/health，更新顶栏任务指示。失败静默（后端没起时不影响界面） */
async function refreshHealth() {
  try {
    const res = await fetch(`${API}/health`);
    if (!res.ok) return state.health;
    state.health = await res.json();
  } catch (_) {
    return state.health;
  }
  renderTaskBar();
  return state.health;
}

function renderTaskBar() {
  const el = $("task-bar");
  if (!el) return;
  const jobs = (state.health && state.health.jobs) || {};
  const cur = jobs.current || null;
  const queued = Number(jobs.queued) || 0;

  if (!cur && !queued) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");

  if (cur) {
    const pct = clampPct(cur.progress);
    const label = cur.kind === "analyze" ? "解析中" : "处理中";
    const stage = cur.detail || cur.stage || "";
    el.innerHTML =
      `<span class="dot"></span>` +
      `<span class="task-text">${esc(label)} · ${pct}%${stage ? " · " + esc(stage) : ""}</span>` +
      `<span class="task-mini"><i style="width:${pct}%"></i></span>` +
      (queued > 0 ? `<span class="task-queued">+${queued} 排队</span>` : "");
  } else {
    el.innerHTML = `<span class="dot"></span><span class="task-text">排队中 · ${queued} 个任务</span>`;
  }
}

/* ---------------------------------------------------------- 语法解析面板 */

function normalizeAnalysis(data) {
  if (!data || typeof data !== "object") {
    return { status: "none", done: 0, total: 0, model: "", error: "", items: {} };
  }
  return {
    status: data.status || "none",
    done: Number(data.done) || 0,
    total: Number(data.total) || 0,
    model: data.model || "",
    error: data.error || "",
    items: data.items && typeof data.items === "object" ? data.items : {},
  };
}

/** 打开视频时拉解析结果；接口不存在 / 出错都按「未解析」降级 */
async function loadAnalysis(pk) {
  let data = null;
  try {
    data = await api(`/videos/${pk}/analysis`);
  } catch (_) {
    data = null;
  }
  if (!state.current || state.current.id !== pk) return null;
  state.analysis = normalizeAnalysis(data);
  state.analysisDoneShown = state.analysis.done;
  renderAnalyzeButton();
  return state.analysis;
}

function renderAnalyzeButton() {
  const btn = $("analyze-btn");
  if (!btn) return;
  const a = state.analysis;
  const status = (a && a.status) || "none";

  if (ANALYSIS_ACTIVE.includes(status)) {
    btn.disabled = true;
    btn.textContent = status === "queued" ? "排队解析…" : `解析中 ${a.done || 0}/${a.total || 0}`;
  } else if (status === "done") {
    btn.disabled = !state.current;
    btn.textContent = "重新解析";
  } else if (status === "failed") {
    btn.disabled = !state.current;
    btn.textContent = "重试解析";
  } else {
    btn.disabled = !state.current;
    btn.textContent = "解析";
  }
}

function renderAnalysisPanel(idx) {
  const el = $("analysis-panel");
  if (!el) return;

  const a = state.analysis;
  const seg = idx >= 0 ? state.segments[idx] : null;
  const item = analysisItemFor(seg, idx);
  const parts = [];

  if (a && ANALYSIS_ACTIVE.includes(a.status)) {
    const pct = a.total ? ` ${a.done || 0}/${a.total}` : "";
    parts.push(`<div class="ana-status">解析中${pct}…</div>`);
  } else if (a && a.status === "failed") {
    parts.push(`<div class="ana-status failed">解析失败${a.error ? "：" + esc(a.error) : ""}</div>`);
  }

  if (!seg) {
    parts.push(`<div class="ana-empty">点一行字幕，这里会显示它的中文译文、语法点和短语。</div>`);
    el.innerHTML = parts.join("");
    return;
  }

  if (!item) {
    let tip;
    if (a && ANALYSIS_ACTIVE.includes(a.status)) {
      tip = "正在解析这个视频，完成的句子会自动出现译文和语法点。";
    } else if (a && a.status === "done") {
      tip = "这一句没有解析结果（可能是空句或标点行）。";
    } else {
      tip = "还没解析，点上方「解析」生成";
    }
    parts.push(`<div class="ana-empty">${esc(tip)}</div>`);
    el.innerHTML = parts.join("");
    return;
  }

  if (item.translation) parts.push(`<div class="ana-zh">${esc(item.translation)}</div>`);

  const grammar = Array.isArray(item.grammar) ? item.grammar : [];
  if (grammar.length) {
    const rows = grammar.map((g) => {
      const point = esc((g && g.point) || "");
      const explain = g && g.explain ? `<div class="ana-g-explain">${esc(g.explain)}</div>` : "";
      const example = g && g.example ? `<div class="ana-g-example mono">${esc(g.example)}</div>` : "";
      return `<div class="ana-g"><div class="ana-g-point">${point}</div>${explain}${example}</div>`;
    }).join("");
    parts.push(`<div class="ana-sec"><h4>语法点</h4>${rows}</div>`);
  }

  const phrases = Array.isArray(item.phrases) ? item.phrases : [];
  if (phrases.length) {
    const chips = phrases.map((p) => {
      const term = esc((p && p.term) || "");
      const meaning = p && p.meaning ? `<span>${esc(p.meaning)}</span>` : "";
      return `<span class="chip"><b>${term}</b>${meaning}</span>`;
    }).join("");
    parts.push(`<div class="ana-sec"><h4>短语 / 生词</h4><div class="ana-chips">${chips}</div></div>`);
  }

  if (item.note) parts.push(`<div class="ana-note">${esc(item.note)}</div>`);

  if (!parts.length) parts.push(`<div class="ana-empty">这一句没有解析内容。</div>`);
  el.innerHTML = parts.join("");
}

/** 解析结果更新后重建字幕行（补上/刷新中文译文），尽量保持当前句与滚动位置 */
function refreshSubtitleTranslations() {
  if (!state.current) return;
  const keepIdx = state.activeIdx;
  const list = $("subtitle-list");
  const keepScroll = list.scrollTop;
  state.activeIdx = -1;
  renderSubtitles();
  list.scrollTop = keepScroll;
  const idx = keepIdx >= 0 ? keepIdx : idxForTime($("video").currentTime * 1000);
  if (idx >= 0) setActiveLine(idx);
  renderAnalysisPanel(state.activeIdx);
}

/** 点「解析」：POST /api/videos/{pk}/analyze，然后每 2 秒轮询进度 */
async function analyzeCurrent() {
  if (!state.current) return;
  const pk = state.current.id;
  const status = (state.analysis && state.analysis.status) || "none";
  const force = status === "done" || status === "failed";
  const btn = $("analyze-btn");

  btn.disabled = true;
  btn.textContent = "请求中…";
  try {
    const res = await api(`/videos/${pk}/analyze`, {
      method: "POST",
      body: JSON.stringify({ force }),
    });
    const total = (res && Number(res.total)) || ((state.analysis && state.analysis.total) || 0);
    state.analysis = {
      status: "queued",
      done: 0,
      total,
      model: "",
      error: "",
      items: (state.analysis && state.analysis.items) || {},
    };
    renderAnalyzeButton();
    renderAnalysisPanel(state.activeIdx);
    toast(`开始解析${total ? `（共 ${total} 句）` : ""}，进度看右上角`, "ok");
    startAnalysisPoll(pk);
    refreshHealth();
  } catch (err) {
    if (err && err.status === 409) {
      toast("已经在解析了，等进度就行", "");
      startAnalysisPoll(pk);
      pollAnalysis(pk);
      return;
    }
    renderAnalyzeButton();
    toast(`解析请求失败：${err.message}`, "err");
  }
}

function startAnalysisPoll(pk) {
  stopAnalysisPoll();
  state.analysisPk = pk;
  state.analysisTimer = setInterval(() => pollAnalysis(pk), 2000);
}

function stopAnalysisPoll() {
  if (state.analysisTimer) clearInterval(state.analysisTimer);
  state.analysisTimer = null;
  state.analysisPk = null;
}

async function pollAnalysis(pk) {
  if (!state.current || state.current.id !== pk) {
    stopAnalysisPoll();
    return;
  }
  let data;
  try {
    data = await api(`/videos/${pk}/analysis`);
  } catch (_) {
    stopAnalysisPoll(); // 接口不可用：别每 2 秒弹错
    return;
  }
  if (!state.current || state.current.id !== pk) return;

  const prev = state.analysis;
  const next = normalizeAnalysis(data);
  const changed = !prev || next.done !== prev.done || next.status !== prev.status;
  state.analysis = next;

  renderAnalyzeButton();
  renderAnalysisPanel(state.activeIdx);

  if (changed && next.done !== state.analysisDoneShown) {
    refreshSubtitleTranslations();
    state.analysisDoneShown = next.done;
  }
  refreshHealth();

  if (!ANALYSIS_ACTIVE.includes(next.status)) {
    stopAnalysisPoll();
    if (next.status === "done") {
      toast(`解析完成：${next.done} 句${next.model ? `（${next.model}）` : ""}`, "ok");
    } else if (next.status === "failed") {
      toast(`解析失败${next.error ? `：${next.error}` : ""}`, "err");
    }
  }
}

/* ------------------------------------------------------------ 精听模式 */

function applySubsMode(mode) {
  const list = $("subtitle-list");
  if (!list) return;
  list.classList.toggle("hide-zh", mode === "en-only");
  list.classList.toggle("hide-en", mode === "write");
  const sel = $("subs-mode");
  if (sel && sel.value !== mode) sel.value = mode;
  if (mode !== "write") {
    for (const item of state.lineEls) item.el.classList.remove("reveal");
  }
}

function setSubsMode(mode, persist = true) {
  state.subsMode = SUBS_MODES.includes(mode) ? mode : "bilingual";
  applySubsMode(state.subsMode);
  if (persist) {
    try {
      localStorage.setItem(SUBS_MODE_KEY, state.subsMode);
    } catch (_) { /* 隐私模式存不了，无所谓 */ }
  }
}

function cycleSubsMode() {
  const next = SUBS_MODES[(SUBS_MODES.indexOf(state.subsMode) + 1) % SUBS_MODES.length];
  setSubsMode(next);
  toast(`精听模式：${SUBS_MODE_TEXT[next]}`, "");
}

function loadSubsMode() {
  let mode = "bilingual";
  try {
    mode = localStorage.getItem(SUBS_MODE_KEY) || "bilingual";
  } catch (_) { /* 忽略 */ }
  setSubsMode(mode, false);
}

/** 听写模式点某行：临时显示英文（5 秒后自动重新遮住） */
function revealLine(line) {
  line.classList.add("reveal");
  clearTimeout(line._revealTimer);
  line._revealTimer = setTimeout(() => line.classList.remove("reveal"), 5000);
}

/* ------------------------------------------------------------------ 事件 */

function bindEvents() {
  $("add-form").addEventListener("submit", (e) => {
    e.preventDefault();
    addVideo($("url-input").value);
  });

  $("video-list").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      deleteVideo(Number(del.dataset.del));
      return;
    }
    const retry = e.target.closest("[data-retry]");
    if (retry) {
      e.stopPropagation();
      retryVideo(Number(retry.dataset.retry));
      return;
    }
    const subs = e.target.closest("[data-subs]");
    if (subs) {
      e.stopPropagation();
      requestSubs(Number(subs.dataset.subs));
      return;
    }
    // 下载链接交给浏览器原生处理，别被卡片点击拦掉
    if (e.target.closest("a[download]")) return;
    const card = e.target.closest(".card");
    if (!card) return;
    const pk = Number(card.dataset.pk);
    const row = state.videos.find((v) => v.id === pk);
    if (!row) return;
    if (row.status === "ready" || row.status === "media-only") openVideo(pk);
    else toast(`还在处理：${STATUS_TEXT[row.status] || row.status} ${row.detail || ""}`, "");
  });

  $("back-btn").addEventListener("click", backToLibrary);
  $("settings-btn").addEventListener("click", openSettings);
  $("settings-back").addEventListener("click", closeSettings);
  $("settings-save").addEventListener("click", saveSettings);
  $("settings-reset").addEventListener("click", resetSettings);
  $("auto-scroll").addEventListener("change", (e) => {
    state.follow = e.target.checked;
  });

  $("analyze-btn").addEventListener("click", analyzeCurrent);
  $("subs-mode").addEventListener("change", (e) => {
    setSubsMode(e.target.value);
    toast(`精听模式：${SUBS_MODE_TEXT[state.subsMode]}`, "");
  });

  $("subtitle-list").addEventListener("click", (e) => {
    const word = e.target.closest(".w");
    const line = e.target.closest(".line");
    if (!line) return;
    // 听写模式：点一下先把这一行的英文临时显示出来
    if (state.subsMode === "write") revealLine(line);
    if (word && word.parentElement.classList.contains("line")) {
      const idx = Number(line.dataset.i);
      const seg = state.segments[idx];
      const spans = state.lineEls[idx].spans;
      const wi = spans.indexOf(word);
      const words = wordListFor(seg);
      if (wi >= 0 && words[wi]) {
        seekTo(words[wi].start_ms, { play: true });
        return;
      }
    }
    const idx = Number(line.dataset.i);
    if (Number.isFinite(idx)) seekTo(state.segments[idx].start_ms, { play: true });
  });

  $("video").addEventListener("pause", () => savePlayback());

  window.addEventListener("beforeunload", () => {
    if (state.current) {
      const el = $("video");
      if (el.currentSrc) {
        const body = JSON.stringify({
          position_ms: Math.round(el.currentTime * 1000),
          rate: el.playbackRate,
        });
        navigator.sendBeacon(
          `${API}/videos/${state.current.id}/playback`,
          new Blob([body], { type: "application/json" }),
        );
      }
    }
  });

  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    // select 也要放过：否则焦点在「精听」下拉里时 ↑↓ 会同时跳字幕行
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if ($("player-view").classList.contains("hidden")) return;
    if (!state.current) return;
    const el = $("video");

    switch (e.key) {
      case " ":
        e.preventDefault();
        if (el.paused) el.play(); else el.pause();
        break;
      case "ArrowRight":
        e.preventDefault();
        state.seekLine = -1;
        el.currentTime = Math.min(el.duration || 1e9, el.currentTime + 3);
        break;
      case "ArrowLeft":
        e.preventDefault();
        state.seekLine = -1;
        el.currentTime = Math.max(0, el.currentTime - 3);
        break;
      case "ArrowDown":
        e.preventDefault();
        jumpLine(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        jumpLine(-1);
        break;
      case "Enter":
        e.preventDefault();
        replayLine();
        break;
      case "a": case "A":
        setAB("a");
        break;
      case "b": case "B":
        setAB("b");
        break;
      case "r": case "R":
        clearAB();
        break;
      case "s": case "S":
        toggleRate();
        break;
      case "n": case "N":
        cycleSubsMode();
        break;
      default:
        break;
    }
  });

  window.addEventListener("popstate", () => {
    if (state.current) backToLibrary();
  });
}

/* ------------------------------------------------------------------ 设置 */

/* 表单不在前端写死：后端 /api/settings 返回 fields 描述（分组/类型/枚举/范围/提示），
 * 这里按描述渲染。加一个配置项只改后端一处，前端自动出现。 */
const GROUP_ORDER = ["网络", "下载", "转写", "解析", "切分", "服务"];

async function openSettings() {
  const el = $("video");
  if (el && !el.paused) el.pause();
  try {
    state.settings = await api("/settings");
  } catch (err) {
    toast(`读取设置失败：${err.message}`, "err");
    return;
  }
  renderSettings();
  showView("settings");
}

function closeSettings() {
  if (state.current) {
    backToLibrary();
    return;
  }
  showView("library");
  loadHealth();
  loadLibrary();
}

function renderFfmpegStatus() {
  const st = (state.settings && state.settings.ffmpeg) || {};
  const el = $("ffmpeg-status");
  el.className = `ffmpeg-status ${st.ok ? "ok" : "err"}`;
  el.textContent = st.ok
    ? `ffmpeg 可用：${st.version || st.exe}`
    : `ffmpeg 不可用：${st.message || "没找到"}。YouTube 现在只提供分离的音视频流，没有 ffmpeg 就合并不出能播放的视频，只能先选「只取字幕」。`;
}

function fieldRow(field, value) {
  const row = document.createElement("div");
  row.className = "field";

  const label = document.createElement("label");
  label.className = "field-label";
  label.textContent = field.label;
  row.appendChild(label);

  const wrap = document.createElement("div");
  wrap.className = "field-ctrl";

  let ctrl;
  if (field.type === "select") {
    ctrl = document.createElement("select");
    for (const [v, text] of field.options) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = text;
      ctrl.appendChild(opt);
    }
    ctrl.value = value ?? "";
  } else if (field.type === "int") {
    ctrl = document.createElement("input");
    ctrl.type = "number";
    ctrl.className = "narrow";
    if (field.min !== undefined && field.min !== null) ctrl.min = field.min;
    if (field.max !== undefined && field.max !== null) ctrl.max = field.max;
    ctrl.value = value ?? "";
  } else {
    ctrl = document.createElement("input");
    ctrl.type = "text";
    ctrl.spellcheck = false;
    if (field.type !== "text") ctrl.classList.add("mono");
    if (field.placeholder) ctrl.placeholder = field.placeholder;
    // 密钥类字段不显示明文（后端可能用 secret:true，也可能直接给 type:"password"，
    // 或者像 llm.api_key 这样只靠 path 认出来）
    if (field.secret || field.type === "password" || /api_?key$/i.test(field.path || "")) {
      ctrl.type = "password";
      ctrl.autocomplete = "off";
    }
    ctrl.value = value ?? "";
  }
  ctrl.dataset.path = field.path;
  wrap.appendChild(ctrl);

  if (field.action === "probe") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost small";
    btn.textContent = "检测";
    btn.addEventListener("click", () => probeFfmpeg(ctrl.value, row));
    wrap.appendChild(btn);
  }

  if (field.action === "probe-llm") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost small";
    btn.textContent = "检测";
    btn.addEventListener("click", () => probeLlm(row));
    wrap.appendChild(btn);
  }
  row.appendChild(wrap);

  if (field.hint) {
    const hint = document.createElement("p");
    hint.className = "hint dim";
    hint.textContent = field.hint;
    row.appendChild(hint);
  }

  const out = document.createElement("p");
  out.className = "probe-out";
  out.hidden = true;
  row.appendChild(out);
  return row;
}

function renderSettings() {
  const data = state.settings;
  const body = $("settings-body");
  body.innerHTML = "";
  $("settings-file").textContent = data.config_file || "";

  const groups = new Map();
  for (const field of data.fields || []) {
    const name = field.group || "其它";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(field);
  }
  const names = [...groups.keys()].sort((a, b) => {
    const ia = GROUP_ORDER.indexOf(a);
    const ib = GROUP_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  for (const name of names) {
    const card = document.createElement("div");
    card.className = "settings-group";
    const title = document.createElement("h3");
    title.textContent = name;
    card.appendChild(title);

    // 「解析」组顶部提示当前 LLM 状态（后端 GET /api/settings 顺带返回 llm 摘要）
    if (name === "解析") {
      const info = data.llm || {};
      const note = document.createElement("p");
      note.className = "hint dim";
      note.textContent = info.configured
        ? `当前模型：${info.model || "?"}${info.base_url ? ` · ${info.base_url}` : ""} · 已配置`
        : `当前模型：${info.model || "未设置"} · 未配置，先填下面的地址 / Key / 模型，点「检测」确认`;
      card.appendChild(note);
    }

    const values = data.values || {};
    for (const field of groups.get(name)) {
      card.appendChild(fieldRow(field, values[field.path]));
    }
    body.appendChild(card);
  }
  renderFfmpegStatus();
}

function collectSettings() {
  const values = {};
  const byPath = new Map((state.settings.fields || []).map((f) => [f.path, f]));
  const nodes = $("settings-body").querySelectorAll("input[data-path], select[data-path]");
  for (const node of nodes) {
    const field = byPath.get(node.dataset.path);
    if (!field) continue;
    if (field.type === "int") {
      values[field.path] = node.value === "" ? "" : Number(node.value);
    } else {
      values[field.path] = node.value;
    }
  }
  return values;
}

async function saveSettings() {
  try {
    state.settings = await api("/settings", {
      method: "PUT",
      body: JSON.stringify({ values: collectSettings() }),
    });
  } catch (err) {
    toast(`保存失败：${err.message}`, "err");
    return;
  }
  renderSettings();
  toast("已保存并生效", "ok");
  loadHealth();
}

function resetSettings() {
  const defaults = (state.settings && state.settings.defaults) || {};
  const nodes = $("settings-body").querySelectorAll("input[data-path], select[data-path]");
  for (const node of nodes) {
    const value = defaults[node.dataset.path];
    node.value = value === undefined || value === null ? "" : value;
  }
  toast("已填回默认值，点「保存并生效」才会写入");
}

async function probeFfmpeg(path, row) {
  const out = row.querySelector(".probe-out");
  out.hidden = false;
  out.className = "probe-out dim";
  out.textContent = "检测中…";
  try {
    const res = await api("/settings/ffmpeg-check", {
      method: "POST",
      body: JSON.stringify({ path: path || "" }),
    });
    out.className = `probe-out ${res.ok ? "ok" : "err"}`;
    out.textContent = res.ok ? `可用：${res.version || res.exe}` : `不可用：${res.message}`;
  } catch (err) {
    out.className = "probe-out err";
    out.textContent = `检测失败：${err.message}`;
  }
}

/** LLM 连通性检测：用设置页当前的 base_url / api_key / model 试一次 */
async function probeLlm(row) {
  const out = row.querySelector(".probe-out");
  out.hidden = false;
  out.className = "probe-out dim";
  out.textContent = "检测中…";
  const values = collectSettings();
  try {
    const res = await api("/settings/llm-check", {
      method: "POST",
      body: JSON.stringify({
        base_url: values["llm.base_url"] || "",
        api_key: values["llm.api_key"] || "",
        model: values["llm.model"] || "",
      }),
    });
    out.className = `probe-out ${res.ok ? "ok" : "err"}`;
    if (res.ok) {
      const latency = res.latency_ms != null ? `（${res.latency_ms} ms）` : "";
      out.textContent = `${res.message || `能用：${res.model || "模型"}`}${latency}`;
    } else {
      out.textContent = res.message || "不可用：未知原因";
    }
  } catch (err) {
    out.className = "probe-out err";
    out.textContent = `检测失败：${err.message}`;
  }
}

/* ------------------------------------------------------------------ 启动 */

/** 环境自检 + 顶栏任务指示。ffmpeg 是硬依赖（YouTube 只给分离的音视频流）。 */
async function loadHealth() {
  const h = await refreshHealth();
  const el = $("env-warn");
  if (!el) return;
  const notes = [];
  if (h && h.ffmpeg === false) {
    notes.push(
      "这台机器上 ffmpeg 不可用：YouTube 现在只提供分离的音视频流，必须有 ffmpeg 才能合并出可播放的视频。" +
        "点右上角「设置」填 ffmpeg.exe 的路径（或先用「只取字幕」模式）。"
    );
  }
  if (h && h.llm && h.llm.configured === false) {
    notes.push(
      "语法解析还没配置 LLM：点右上角「设置」，在「解析」组里填 base_url / API Key / 模型，点「检测」确认可用。"
    );
  }
  el.hidden = !notes.length;
  el.innerHTML = notes.map((n) => `<div>${esc(n)}</div>`).join("");
}

function boot() {
  bindEvents();
  loadSubsMode();
  renderAnalyzeButton();
  renderAnalysisPanel(-1);
  loadHealth();
  loadLibrary();
}

boot();
