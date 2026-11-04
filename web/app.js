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
  // AB 循环自己把 currentTime 拨回去时置 true，seeked 处理器据此区分「我们干的」和「用户拖的」
  abJump: false,
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
  // ---- 生词本 / 词典 ----
  knownVocab: null,      // { learning:Set, known:Set, ignored:Set }，字幕打点用；未拿到时为 null
  popover: null,         // 查词弹层状态
  lookupToken: 0,        // 连点不同词时丢弃过期响应
  dictStatus: null,      // 最近一次 GET /api/dict/status
  dictStatusError: "",
  dictTimer: null,
  dictWasBusy: false,
  prevView: "library",   // 从哪个视图进的设置（关闭设置后回哪）
  vocab: {
    status: "all", q: "", tag: "", sort: "recent",
    items: [], total: 0, stats: {}, loading: false, loaded: false,
  },
  review: null,          // 复习队列 { items, index, revealed, done, submitting }
  vocabBadge: 0,
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
  const dict = state.health && state.health.dict;
  let dictText = "";
  let dictTitle = "";
  if (dict) {
    if (dict.installed) {
      dictText = ` · 词典 ${fmtNumber(dict.entries)}`;
      dictTitle = `本地词典已安装：${fmtNumber(dict.entries)} 词条`;
    } else {
      dictText = " · 词典未装";
      dictTitle = "本地词典未安装，点「设置 → 词典」下载";
    }
  } else {
    dictTitle = "本地词典状态未知（/api/health 没有 dict 字段）";
  }
  if (active.length) {
    el.className = "health busy";
    el.innerHTML = `<span class="dot"></span>处理中 ${active.length}${esc(dictText)}`;
  } else {
    el.className = "health ok";
    el.innerHTML = `<span class="dot"></span>就绪${esc(dictText)}`;
  }
  el.title = dictTitle;
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
  $("vocab-view").classList.toggle("hidden", name !== "vocab");
  if (name !== "player") closeWordPopover();
  if (name !== "vocab") closeReview();
  if (name !== "settings") stopDictPoll();
  // 顶栏导航高亮当前视图
  const navLibrary = $("nav-library");
  const navVocab = $("nav-vocab");
  if (navLibrary) navLibrary.classList.toggle("on", name === "library");
  if (navVocab) navVocab.classList.toggle("on", name === "vocab");
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
  state.knownVocab = null;
  closeWordPopover();
  closeReview();
  clearWordHighlight();
  state.seekLine = -1;
  state.ab = { a: null, b: null };
  state.abJump = false;
  refreshABBadge();
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
  // 字幕生词标记：加载视频后拉一次已知词，异步补上 .saved，失败静默
  loadKnownVocab();

  // 焦点可能还停在添加框、卡片按钮或设置控件上：不清掉的话空格会先被它们吃掉
  if (
    document.activeElement &&
    document.activeElement !== document.body &&
    typeof document.activeElement.blur === "function"
  ) {
    document.activeElement.blur();
  }

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
  closeWordPopover();
  closeReview();
  state.knownVocab = null;
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
    // 生词本里 learning 的词：虚线下划线打点。只加类，不改字幕文本
    if (isLearningWord(w.text)) span.classList.add("saved");
    span.textContent = w.text;
    container.appendChild(span);
    spans.push(span);
    prev = w.text;
  });
  return spans;
}

/** 重新应用字幕生词标记（/api/vocab/known 晚到、收藏/掌握后立即刷新用） */
function applySavedMarks() {
  state.lineEls.forEach((item) => {
    const idx = Number(item.el.dataset.i);
    const seg = state.segments[idx];
    const words = seg ? wordListFor(seg) : [];
    item.spans.forEach((span, wi) => {
      const w = words[wi];
      span.classList.toggle("saved", Boolean(w) && isLearningWord(w.text));
    });
  });
  if (state.nowSpans && state.nowSpans.length && state.activeIdx >= 0 && state.segments[state.activeIdx]) {
    const words = wordListFor(state.segments[state.activeIdx]);
    state.nowSpans.forEach((span, wi) => {
      const w = words[wi];
      span.classList.toggle("saved", Boolean(w) && isLearningWord(w.text));
    });
  }
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
  // 切到别的行了：查词卡跟着关掉（它还钉在上一行的词旁边）
  if (state.popover) closeWordPopover();
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
  if (state.ab.a != null && state.ab.b != null) {
    if (state.ab.b <= state.ab.a) {
      clearAB("AB 区间无效（终点不在起点之后），已退出循环");
    } else if (ms >= state.ab.b || ms < state.ab.a - 500) {
      state.seekLine = -1;
      state.abJump = true;
      el.currentTime = state.ab.a / 1000;
    }
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

/** 顶住「AB 循环中不知道怎么退出」：把状态显式画出来，Esc 随时能退 */
function refreshABBadge() {
  const badge = $("ab-badge");
  if (!badge) return;
  const { a, b } = state.ab;
  if (a == null) {
    badge.hidden = true;
    badge.textContent = "";
    return;
  }
  badge.hidden = false;
  badge.textContent =
    b == null
      ? `AB 循环：起点 A ${fmtTime(a)}，再按 B 定终点（Esc 取消）`
      : `AB 循环中：${fmtTime(a)} ↔ ${fmtTime(b)}（按 Esc 或拖到区间外退出）`;
}

function setAB(which) {
  const el = $("video");
  const ms = Math.round(el.currentTime * 1000);
  if (which === "b") {
    if (state.ab.a == null) {
      toast("先按 A 定循环起点", "warn");
      return;
    }
    // 终点在起点之前（或离得太近）会让循环变成原地抽搐
    if (ms <= state.ab.a + 300) {
      toast("B 点要在 A 点之后（至少 0.3 秒），已忽略", "warn");
      return;
    }
  }
  state.ab[which] = ms;
  refreshABBadge();
  toast(
    which === "a"
      ? `A 点：${fmtTime(ms)}（再按 B 定终点，Esc 取消）`
      : `B 点：${fmtTime(ms)}，开始循环（Esc 退出）`,
    "ok",
  );
}

function clearAB(message) {
  const had = state.ab.a != null || state.ab.b != null;
  state.ab = { a: null, b: null };
  state.abJump = false;
  refreshABBadge();
  if (had || message) toast(message || "已退出 AB 循环", "ok");
}

/** 用户自己把进度拖/跳到 AB 区间外，就当他要正常看，顺手退出循环 */
function onVideoSeeked() {
  if (state.abJump) {
    state.abJump = false;
    return;
  }
  const { a, b } = state.ab;
  if (a == null || b == null) return;
  const ms = Math.round($("video").currentTime * 1000);
  if (ms < a - 200 || ms > b + 200) clearAB("已退出 AB 循环（跳到区间外）");
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
  updateHealth();
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

/* ------------------------------------------------------------ 词典 / 生词本 */

/* 后端 tags 给的是 [{code,label}]，label 已是中文；这里只兜底常见 code → 中文 */
const TAG_LABELS = {
  zk: "中考", gk: "高考", cet4: "四级", cet6: "六级", ky: "考研",
  toefl: "托福", ielts: "雅思", gre: "GRE", gmat: "GMAT",
};
const VOCAB_STATUS_TEXT = { learning: "学习中", known: "已掌握", ignored: "已忽略" };
const VOCAB_TABS = [
  ["all", "全部"],
  ["learning", "学习中"],
  ["known", "已掌握"],
  ["ignored", "已忽略"],
];
const VOCAB_PAGE = 50;

function fmtNumber(n) {
  if (n === null || n === undefined || n === "") return "—";
  const v = Number(n);
  if (!Number.isFinite(v)) return String(n);
  return v.toLocaleString("zh-CN");
}

function fmtBytes(bytes) {
  const v = Number(bytes);
  if (!Number.isFinite(v) || v <= 0) return "0 MB";
  const mb = v / 1024 / 1024;
  if (mb < 1024) return `${mb >= 10 ? Math.round(mb) : mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

/** 后端的时间可能是 ISO 字符串，也可能是秒/毫秒时间戳，统一显示成本地时间 */
function fmtDateTime(value) {
  if (value === null || value === undefined || value === "") return "";
  let d;
  if (typeof value === "number") d = new Date(value < 1e12 ? value * 1000 : value);
  else d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 收藏/查词比较用的词形键：小写、去标点、去所有格。和字幕里的词形做精确匹配 */
function wordKey(text) {
  let s = String(text === null || text === undefined ? "" : text)
    .toLowerCase()
    .replace(/[’`]/g, "'");
  s = s.replace(/^[^a-z']+|[^a-z']+$/g, "");
  if (s.endsWith("'s")) s = s.slice(0, -2);
  return s;
}

/** 这个字幕词是不是生词本里 learning 的词（known/ignored 不打点） */
function isLearningWord(text) {
  const key = wordKey(text);
  if (!key || !state.knownVocab) return false;
  return state.knownVocab.learning.has(key);
}

function normalizeKnownSet(list) {
  const set = new Set();
  for (const w of list || []) {
    const text = typeof w === "string" ? w : (w && (w.word || w.lemma)) || "";
    const key = wordKey(text);
    if (key) set.add(key);
  }
  return set;
}

/** 拉一次 /api/vocab/known：把 learning 词标到字幕上。失败静默，不影响播放 */
async function loadKnownVocab() {
  let data;
  try {
    data = await api("/vocab/known");
  } catch (_) {
    return;
  }
  state.knownVocab = {
    learning: normalizeKnownSet(data && data.learning),
    known: normalizeKnownSet(data && data.known),
    ignored: normalizeKnownSet(data && data.ignored),
  };
  // 后端附带该词的变形（walked/walking…），这样字幕里屈折形式也能一起打点
  for (const form of (data && data.forms) || []) {
    const key = wordKey(form);
    if (key) state.knownVocab.learning.add(key);
  }
  applySavedMarks();
}

/* ---------------------------------------------------------- 生词本徽标 */

function setVocabBadge(n) {
  const badge = $("vocab-badge");
  const count = Math.max(0, Number(n) || 0);
  state.vocabBadge = count;
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? "99+" : String(count);
    badge.classList.remove("hidden");
  } else {
    badge.textContent = "0";
    badge.classList.add("hidden");
  }
}

async function refreshVocabBadge() {
  try {
    const data = await api("/vocab?status=due&limit=1&offset=0&sort=due");
    const stats = (data && data.stats) || {};
    setVocabBadge(stats.due_now != null ? stats.due_now : 0);
  } catch (_) {
    /* 后端没起或接口还没实现：不显示徽标即可 */
  }
}

/* ------------------------------------------------------------ 查词弹层 */

function tagChipsHtml(tags) {
  if (!Array.isArray(tags) || !tags.length) return "";
  const chips = [];
  for (const t of tags) {
    const code = typeof t === "string" ? t : (t && (t.code || t.label)) || "";
    const label = (t && typeof t === "object" && t.label) || TAG_LABELS[String(code).toLowerCase()] || code;
    if (!label) continue;
    chips.push(`<span class="tag-chip">${esc(label)}</span>`);
  }
  return chips.length ? `<span class="tag-chips">${chips.join("")}</span>` : "";
}

function openWordPopover(wordEl, seg, idx, wi) {
  const words = wordListFor(seg);
  const w = words[wi];
  if (!w) return;
  const text = String(w.text || "").trim();
  const key = wordKey(text);
  if (!key) return;
  state.popover = {
    word: text,
    sentence: String(seg.text || words.map((x) => x.text).join(" ")),
    pk: state.current ? state.current.id : null,
    seq: seg.seq !== undefined && seg.seq !== null ? seg.seq : idx,
    startMs: Number(w.start_ms) || 0,
    rect: wordEl.getBoundingClientRect(),
    data: null,
    status: null,
    vocabId: null,
    loading: true,
    error: "",
    needDict: false,
    explain: null,
    explainLoading: false,
    explainError: "",
  };
  renderWordPopover();
  const token = ++state.lookupToken;
  lookupWord(token);
}

function closeWordPopover() {
  const el = $("word-popover");
  const overlay = $("popover-overlay");
  if (el) {
    el.classList.add("hidden");
    el.classList.remove("sheet");
    el.innerHTML = "";
    el.style.left = "";
    el.style.top = "";
  }
  if (overlay) overlay.classList.add("hidden");
  state.popover = null;
  state.lookupToken += 1;
}

async function lookupWord(token) {
  const p = state.popover;
  if (!p) return;
  const params = new URLSearchParams({ q: p.word });
  if (p.pk !== null && p.pk !== undefined) params.set("pk", String(p.pk));
  if (p.seq !== null && p.seq !== undefined) params.set("seq", String(p.seq));
  let data = null;
  let error = "";
  let needDict = false;
  try {
    data = await api(`/dict/lookup?${params.toString()}`);
  } catch (err) {
    error = err && err.message ? err.message : "查词失败";
    needDict = err && (err.status === 404 || err.status === 503) || /未安装|not installed|不存在|词典/i.test(error);
  }
  if (token !== state.lookupToken || state.popover !== p) return;
  p.loading = false;
  p.error = error;
  p.needDict = Boolean(needDict);
  if (data && data.installed === false) {
    // 词典没装时后端仍返回 200 + {found:false,installed:false}：当成「去装词典」处理
    p.needDict = true;
    p.error = data.message || "本地词典未安装，请到设置页「词典」里下载";
  } else if (data) {
    p.data = data;
    const found = Boolean(data.found);
    p.status = found ? data.status || null : null;
    p.vocabId = found ? data.vocab_id || null : null;
    if (found && data.word) p.word = String(data.word);
  }
  renderWordPopover();
}

function retryLookup() {
  const p = state.popover;
  if (!p) return;
  p.loading = true;
  p.error = "";
  p.needDict = false;
  renderWordPopover();
  lookupWord(++state.lookupToken);
}

function positionWordPopover() {
  const el = $("word-popover");
  if (!el || el.classList.contains("hidden")) return;
  const p = state.popover;
  const narrow = window.innerWidth <= 640;
  const rect = p && p.rect ? p.rect : null;
  if (narrow || !rect) {
    el.classList.add("sheet");
    el.style.left = "";
    el.style.top = "";
    return;
  }
  el.classList.remove("sheet");
  el.style.left = "0px";
  el.style.top = "0px";
  const width = el.offsetWidth;
  const height = el.offsetHeight;
  const margin = 10;
  let left = rect.left + rect.width / 2 - width / 2;
  left = Math.max(margin, Math.min(window.innerWidth - width - margin, left));
  let top = rect.bottom + 8;
  if (top + height + margin > window.innerHeight) top = rect.top - height - 8;
  top = Math.max(margin, Math.min(window.innerHeight - height - margin, top));
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
}

function speakWord(text) {
  const word = String(text || "").trim();
  if (!word) return;
  if (!("speechSynthesis" in window)) {
    toast("这个浏览器不支持语音朗读", "");
    return;
  }
  try {
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(word);
    utter.lang = "en-US";
    utter.rate = 0.95;
    window.speechSynthesis.speak(utter);
  } catch (_) {
    toast("朗读失败", "err");
  }
}

function explainHtml(ex) {
  if (!ex || typeof ex !== "object") return "";
  const parts = [];
  if (ex.meaning) parts.push(`<div class="wp-explain-sec"><h4>意思</h4><p>${esc(ex.meaning)}</p></div>`);
  if (ex.usage) parts.push(`<div class="wp-explain-sec"><h4>用法</h4><p>${esc(ex.usage)}</p></div>`);
  if (Array.isArray(ex.collocations) && ex.collocations.length) {
    const rows = ex.collocations
      .map((c) => {
        const phrase = (c && (c.phrase || c.text)) || "";
        const meaning = (c && c.meaning) || "";
        if (!phrase && !meaning) return "";
        return `<li><b>${esc(phrase)}</b>${meaning ? ` — ${esc(meaning)}` : ""}</li>`;
      })
      .filter(Boolean);
    if (rows.length) parts.push(`<div class="wp-explain-sec"><h4>搭配</h4><ul>${rows.join("")}</ul></div>`);
  }
  if (ex.grammar) parts.push(`<div class="wp-explain-sec"><h4>语法</h4><p>${esc(ex.grammar)}</p></div>`);
  if (Array.isArray(ex.examples) && ex.examples.length) {
    const rows = ex.examples
      .map((x) => {
        if (!x) return "";
        const en = x.en || x.en_text || "";
        const zh = x.zh || x.zh_text || "";
        return `<li><span class="wp-ex-en">${esc(en)}</span><span class="wp-ex-zh dim">${esc(zh)}</span></li>`;
      })
      .filter(Boolean);
    if (rows.length) parts.push(`<div class="wp-explain-sec"><h4>例句</h4><ul class="wp-examples">${rows.join("")}</ul></div>`);
  }
  if (ex.note) parts.push(`<div class="wp-explain-sec"><h4>备注</h4><p>${esc(ex.note)}</p></div>`);
  return parts.length ? parts.join("") : `<p class="dim">模型没有返回内容</p>`;
}

function renderWordPopover() {
  const el = $("word-popover");
  const overlay = $("popover-overlay");
  if (!el) return;
  const p = state.popover;
  if (!p) {
    el.classList.add("hidden");
    el.innerHTML = "";
    if (overlay) overlay.classList.add("hidden");
    return;
  }
  const d = p.data || {};
  const found = Boolean(d.found);
  const word = found && d.word ? d.word : p.word;
  const phonetic = found ? d.phonetic || "" : "";
  const lemma = found ? d.lemma || "" : "";
  const status = p.status || (found ? d.status : null);
  const tags = found ? d.tags : null;

  let html = `<div class="wp-head"><div class="wp-title-row">`;
  html += `<span class="wp-word">${esc(word)}</span>`;
  if (phonetic) html += `<span class="wp-phon">/${esc(phonetic)}/</span>`;
  html += `<span class="wp-spacer"></span>`;
  html += `<button type="button" class="ghost small" data-wp="speak" title="朗读这个词">🔊 发音</button>`;
  html += `<button type="button" class="icon-btn wp-close" data-wp="close" title="关闭（Esc）">×</button>`;
  html += `</div>`;
  if (lemma && String(lemma).toLowerCase() !== String(word).toLowerCase()) {
    html += `<div class="wp-meta dim">原形：${esc(lemma)}</div>`;
  }
  if (found && d.pos) html += `<div class="wp-meta dim">${esc(d.pos)}</div>`;
  const chips = tagChipsHtml(tags);
  const statusBadge = status
    ? `<span class="badge ${status === "known" ? "ready" : status === "ignored" ? "muted" : ""}">${esc(VOCAB_STATUS_TEXT[status] || status)}</span>`
    : "";
  if (chips || statusBadge) {
    html += `<div class="wp-tags">${chips}${statusBadge}</div>`;
  }
  html += `</div><div class="wp-body">`;

  if (p.loading) {
    html += `<p class="dim">查词中…</p>`;
  } else if (p.error) {
    html += `<div class="wp-empty"><p>${esc(p.error)}</p>`;
    html += `<div class="wp-actions">`;
    if (p.needDict) {
      html += `<button type="button" class="small primary" data-wp="settings">去设置页下载词典</button>`;
    } else {
      html += `<button type="button" class="ghost small" data-wp="retry">重试</button>`;
    }
    html += `</div></div>`;
  } else if (!found) {
    html += `<div class="wp-empty"><p>本地词典没有收录「${esc(p.word)}」。</p>`;
    html += `<p class="dim">也可以先把它收藏进生词本，之后补释义。</p>`;
    html += `<div class="wp-actions"><button type="button" class="small primary" data-wp="collect">收藏生词</button>`;
    html += `<button type="button" class="ghost small" data-wp="explain">AI 详解</button></div></div>`;
  } else {
    const trans = (Array.isArray(d.translations) ? d.translations : [])
      .map((t) => (typeof t === "string" ? t : (t && (t.translation || t.text || t.zh)) || ""))
      .filter(Boolean);
    if (trans.length) html += `<p class="wp-trans">${trans.map((t) => esc(t)).join("；")}</p>`;
    const defs = (Array.isArray(d.definition) ? d.definition : [])
      .map((t) => (typeof t === "string" ? t : (t && (t.text || t.definition)) || ""))
      .filter(Boolean);
    if (defs.length) html += `<ul class="wp-defs">${defs.slice(0, 4).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`;
    const ranks = [];
    if (d.collins) ranks.push(`柯林斯 ${d.collins}`);
    if (d.oxford) ranks.push("牛津核心");
    if (d.bnc) ranks.push(`BNC ${d.bnc}`);
    if (d.frq) ranks.push(`词频 ${d.frq}`);
    if (d.known_rank) ranks.push(`难度排名 ${d.known_rank}`);
    if (ranks.length) html += `<p class="wp-ranks dim">${ranks.map((t) => esc(t)).join(" · ")}</p>`;
    if (Array.isArray(d.forms) && d.forms.length) {
      const forms = d.forms
        .map((f) => (typeof f === "string" ? f : (f && (f.word || f.text)) || ""))
        .filter(Boolean);
      if (forms.length) html += `<p class="wp-forms dim">变形：${forms.map((f) => esc(f)).join(" / ")}</p>`;
    }
    html += `<div class="wp-actions">`;
    if (status === "learning") {
      html += `<span class="wp-state">已在生词本 · 学习中</span>`;
      html += `<button type="button" class="ghost small" data-wp="known">标为已掌握</button>`;
    } else if (status === "known") {
      html += `<span class="wp-state ok">已在生词本 · 已掌握</span>`;
    } else if (status === "ignored") {
      html += `<span class="wp-state">已在生词本 · 已忽略</span>`;
      html += `<button type="button" class="ghost small" data-wp="known">标为已掌握</button>`;
    } else {
      html += `<button type="button" class="small primary" data-wp="collect">收藏生词</button>`;
      html += `<button type="button" class="ghost small" data-wp="known">标为已掌握</button>`;
    }
    html += `<button type="button" class="ghost small" data-wp="explain">${p.explain ? "重新生成 AI 详解" : "AI 详解"}</button>`;
    html += `</div>`;
  }

  if (p.explainLoading) {
    html += `<div class="wp-explain"><p class="dim">AI 正在生成详解…</p></div>`;
  } else if (p.explainError) {
    html += `<div class="wp-explain"><p class="vocab-err">${esc(p.explainError)}</p>`;
    html += `<div class="wp-actions"><button type="button" class="ghost small" data-wp="explain">重试</button>`;
    if (/llm|模型|设置|配置|未配置/i.test(p.explainError)) {
      html += `<button type="button" class="ghost small" data-wp="settings">去设置页配置 LLM</button>`;
    }
    html += `</div></div>`;
  } else if (p.explain) {
    html += `<div class="wp-explain">${explainHtml(p.explain)}</div>`;
  }

  html += `</div>`;
  el.innerHTML = html;
  el.classList.remove("hidden");
  if (overlay) overlay.classList.remove("hidden");
  positionWordPopover();
}

function vocabPayloadFromPopover(p, status) {
  const d = (p && p.data) || {};
  const word = String(d.word || (p && p.word) || "").trim();
  const lemma = String(d.lemma || word).trim();
  const translations = Array.isArray(d.translations)
    ? d.translations
        .map((t) => (typeof t === "string" ? t : (t && (t.translation || t.text || t.zh)) || ""))
        .filter(Boolean)
    : [];
  // 接口里 definition 是一个多行字符串（词典 lookup 返回的是数组，这里拼回去）
  const defs = Array.isArray(d.definition)
    ? d.definition.map((t) => (typeof t === "string" ? t : "")).filter(Boolean)
    : [];
  return {
    word,
    lemma,
    phonetic: d.phonetic || "",
    pos: d.pos || "",
    tags: Array.isArray(d.tags) ? d.tags : [],
    translation: translations.join("；"),
    definition: defs.join("\n"),
    status,
    note: "",
    source: {
      pk: p.pk,
      seq: p.seq,
      sentence: p.sentence || "",
      start_ms: p.startMs || 0,
    },
  };
}

/** 收藏进生词本；收藏成功后当前字幕里这个词立即打点 */
async function collectWord() {
  const p = state.popover;
  if (!p) return;
  const payload = vocabPayloadFromPopover(p, "learning");
  if (!payload.word) return;
  let res;
  try {
    res = await api("/vocab", { method: "POST", body: JSON.stringify(payload) });
  } catch (err) {
    toast(`收藏失败：${err.message}`, "err");
    return;
  }
  if (state.popover === p) {
    p.status = "learning";
    p.data = p.data || {};
    if (res && res.item && res.item.id) p.vocabId = res.item.id;
    if (res && res.item && res.item.status) p.status = res.item.status;
    renderWordPopover();
  }
  toast(res && res.created === false ? "这个词已经在生词本里了" : "已收藏到生词本", "ok");
  loadKnownVocab();
  refreshVocabBadge();
  if (state.vocab.loaded) loadVocab(true);
}

/** 标为已掌握：生词本里已有就 PATCH，没有就直接按 known 建一条 */
async function markWordKnown() {
  const p = state.popover;
  if (!p) return;
  try {
    if (p.vocabId) {
      const res = await api(`/vocab/${p.vocabId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "known" }),
      });
      if (res && res.item && res.item.id) p.vocabId = res.item.id;
    } else {
      const payload = vocabPayloadFromPopover(p, "known");
      if (!payload.word) return;
      const res = await api("/vocab", { method: "POST", body: JSON.stringify(payload) });
      if (res && res.item && res.item.id) p.vocabId = res.item.id;
    }
  } catch (err) {
    toast(`操作失败：${err.message}`, "err");
    return;
  }
  p.status = "known";
  if (p.data) p.data.status = "known";
  if (state.popover === p) renderWordPopover();
  toast("已标为掌握，不再进入复习", "ok");
  loadKnownVocab();
  refreshVocabBadge();
  if (state.vocab.loaded) loadVocab(true);
}

async function explainWord() {
  const p = state.popover;
  if (!p || p.explainLoading) return;
  p.explainLoading = true;
  p.explainError = "";
  renderWordPopover();
  let res;
  try {
    res = await api("/dict/explain", {
      method: "POST",
      body: JSON.stringify({
        word: (p.data && p.data.word) || p.word,
        lemma: (p.data && p.data.lemma) || p.word,
        sentence: p.sentence || "",
        pk: p.pk,
        seq: p.seq,
      }),
    });
  } catch (err) {
    if (state.popover !== p) return;
    p.explainLoading = false;
    p.explainError = err && err.message ? err.message : "AI 详解失败";
    renderWordPopover();
    return;
  }
  if (state.popover !== p) return;
  p.explainLoading = false;
  p.explain = (res && res.explanation) || res || null;
  renderWordPopover();
}

/* ------------------------------------------------------------ 生词本视图 */

let vocabLoadToken = 0;

function openVocabView() {
  // 从播放页切过来：把视频停掉（播放进度会保存），别让声音继续放
  if (state.current) {
    backToLibrary();
  }
  showView("vocab");
  refreshVocabBadge();
  loadVocab(true);
}

function openLibraryView() {
  if (state.current) {
    backToLibrary();
    return;
  }
  showView("library");
  loadHealth();
  loadLibrary();
}

/** 从生词本跳回视频并定位到出现句 */
async function openVideoAt(pk, seq, startMs) {
  if (!Number.isFinite(Number(pk))) return;
  closeReview();
  await openVideo(Number(pk), true);
  if (!state.current || Number(state.current.id) !== Number(pk)) return;
  let idx = -1;
  if (Number.isFinite(Number(seq))) {
    idx = state.segments.findIndex((s) => Number(s.seq) === Number(seq));
  }
  if (idx < 0 && Number.isFinite(Number(startMs))) idx = idxForTime(Number(startMs));
  const ms = Number.isFinite(Number(startMs)) && Number(startMs) > 0
    ? Number(startMs)
    : idx >= 0 ? state.segments[idx].start_ms : 0;
  const el = $("video");
  const go = () => {
    try {
      el.currentTime = Math.max(0, ms / 1000);
    } catch (_) { /* 元数据还没好就算了 */ }
    if (idx >= 0) {
      state.seekLine = idx;
      setActiveLine(idx);
    }
    el.play().catch(() => {});
  };
  if (el.readyState >= 1) go();
  else el.addEventListener("loadedmetadata", go, { once: true });
}

async function loadVocab(reset = false) {
  const v = state.vocab;
  const token = ++vocabLoadToken;
  const offset = reset ? 0 : v.items.length;
  if (reset) {
    v.items = [];
    v.total = 0;
    v.loaded = false;
  }
  v.loading = true;
  if (reset) renderVocabList();
  const params = new URLSearchParams({
    status: v.status,
    limit: String(VOCAB_PAGE),
    offset: String(offset),
    sort: v.sort || "recent",
  });
  if (v.q.trim()) params.set("q", v.q.trim());
  if (v.tag) params.set("tag", v.tag);
  let data;
  try {
    data = await api(`/vocab?${params.toString()}`);
  } catch (err) {
    if (token !== vocabLoadToken) return;
    v.loading = false;
    renderVocabList();
    toast(`读取生词本失败：${err.message}`, "err");
    return;
  }
  if (token !== vocabLoadToken) return;
  v.items = reset ? data.items || [] : v.items.concat(data.items || []);
  v.total = Number(data.total) || 0;
  v.stats = data.stats || {};
  v.loaded = true;
  v.loading = false;
  setVocabBadge(v.stats.due_now != null ? v.stats.due_now : 0);
  renderVocabStats();
  renderVocabTabs();
  renderVocabTagOptions();
  renderVocabList();
}

function renderVocabStats() {
  const box = $("vocab-stats");
  if (!box) return;
  const s = state.vocab.stats || {};
  const cells = [
    ["总词数", s.all],
    ["学习中", s.learning],
    ["待复习", s.due_now],
    ["今日已复习", s.reviews_today],
    ["连续天数", s.streak_days],
  ];
  box.innerHTML = cells
    .map(
      ([label, value]) =>
        `<div class="vocab-stat"><div class="vocab-stat-num">${esc(fmtNumber(value))}</div><div class="vocab-stat-label">${esc(label)}</div></div>`
    )
    .join("");
  const total = $("vocab-total");
  if (total) total.textContent = state.vocab.loaded ? `共 ${fmtNumber(state.vocab.total)} 个` : "";
}

function renderVocabTabs() {
  const box = $("vocab-tabs");
  if (!box) return;
  box.innerHTML = VOCAB_TABS.map(
    ([value, label]) =>
      `<button type="button" class="vocab-tab${state.vocab.status === value ? " on" : ""}" data-status="${esc(value)}">${esc(label)}</button>`
  ).join("");
}

function renderVocabTagOptions() {
  const sel = $("vocab-tag");
  if (!sel) return;
  const map = new Map();
  for (const item of state.vocab.items) {
    for (const t of item.tags || []) {
      const code = typeof t === "string" ? t : (t && (t.code || t.label)) || "";
      const label = (t && typeof t === "object" && t.label) || TAG_LABELS[String(code).toLowerCase()] || code;
      if (code && !map.has(String(code))) map.set(String(code), String(label || code));
    }
  }
  const current = state.vocab.tag || "";
  if (current && !map.has(current)) map.set(current, TAG_LABELS[current] || current);
  const opts = [`<option value="">全部 tag</option>`];
  for (const [code, label] of map) opts.push(`<option value="${esc(code)}">${esc(label)}</option>`);
  sel.innerHTML = opts.join("");
  sel.value = current;
}

function vocabDueText(item) {
  const d = Number(item && item.due_in_days);
  if (!Number.isFinite(d)) return { text: "", cls: "" };
  if (d <= 0) return { text: "今天到期", cls: "due-now" };
  if (d === 1) return { text: "明天到期", cls: "" };
  return { text: `${d} 天后到期`, cls: "" };
}

function vocabRowHtml(item) {
  const status = item.status || "learning";
  const due = status === "learning" ? vocabDueText(item) : { text: "", cls: "" };
  const word = item.word || item.lemma || "";
  const parts = [];
  parts.push(`<div class="vocab-row" data-id="${esc(item.id)}">`);
  parts.push(`<div class="vocab-main">`);
  parts.push(`<div class="vocab-wordline">`);
  parts.push(`<span class="vocab-word">${esc(word)}</span>`);
  if (item.phonetic) parts.push(`<span class="vocab-phon">/${esc(item.phonetic)}/</span>`);
  if (item.pos) parts.push(`<span class="vocab-pos dim">${esc(item.pos)}</span>`);
  parts.push(`<span class="badge ${status === "known" ? "ready" : status === "ignored" ? "muted" : ""}">${esc(VOCAB_STATUS_TEXT[status] || status)}</span>`);
  if (due.text) parts.push(`<span class="vocab-due ${esc(due.cls)}">${esc(due.text)}</span>`);
  parts.push(`</div>`);
  parts.push(`<div class="vocab-zh">${esc(item.translation || "（没有释义）")}</div>`);
  const chips = tagChipsHtml(item.tags);
  if (chips || item.source_title) {
    parts.push(`<div class="vocab-meta">`);
    if (chips) parts.push(chips);
    if (item.source_title) parts.push(`<span class="vocab-src dim" title="${esc(item.source_title)}">${esc(item.source_title)}</span>`);
    parts.push(`</div>`);
  }
  parts.push(`<div class="vocab-occ hidden"></div>`);
  parts.push(`</div>`);
  parts.push(`<div class="vocab-actions">`);
  if (status !== "known") {
    parts.push(`<button type="button" class="ghost small" data-vact="known">标已掌握</button>`);
  }
  parts.push(`<button type="button" class="ghost small" data-vact="occ">出现句</button>`);
  parts.push(`<button type="button" class="ghost small danger" data-vact="delete">删除</button>`);
  parts.push(`</div>`);
  parts.push(`</div>`);
  return parts.join("");
}

function renderVocabList() {
  const list = $("vocab-list");
  const more = $("vocab-more");
  if (!list) return;
  const v = state.vocab;
  if (!v.items.length) {
    let text = "生词本还是空的：在播放页点字幕里的单词，就能收藏进来。";
    if (v.loading) text = "加载中…";
    else if (v.q.trim() || v.tag || v.status !== "all") text = "没有符合条件的生词。";
    list.innerHTML = `<p class="vocab-empty dim">${esc(text)}</p>`;
  } else {
    list.innerHTML = v.items.map(vocabRowHtml).join("");
  }
  if (more) more.classList.toggle("hidden", !(v.items.length < v.total));
}

async function patchVocabStatus(item, status) {
  try {
    const res = await api(`/vocab/${item.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    if (res && res.item) Object.assign(item, res.item);
  } catch (err) {
    toast(`更新失败：${err.message}`, "err");
    return;
  }
  toast(status === "known" ? "已标为掌握，不再复习" : "已更新", "ok");
  if (state.vocab.status !== "all" && state.vocab.status !== status) loadVocab(true);
  else renderVocabList();
  loadKnownVocab();
  refreshVocabBadge();
}

async function deleteVocabItem(item) {
  const name = item.word || item.lemma || "这个词";
  if (!confirm(`从生词本删除「${name}」？`)) return;
  try {
    await api(`/vocab/${item.id}`, { method: "DELETE" });
  } catch (err) {
    toast(`删除失败：${err.message}`, "err");
    return;
  }
  toast("已删除", "ok");
  loadVocab(true);
  loadKnownVocab();
  refreshVocabBadge();
}

async function toggleOccurrences(row, item) {
  const box = row.querySelector(".vocab-occ");
  if (!box) return;
  if (!box.classList.contains("hidden")) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = `<p class="dim">读取出现位置…</p>`;
  let data;
  try {
    data = await api(`/vocab/${item.id}/occurrences`);
  } catch (err) {
    box.innerHTML = `<p class="vocab-err">读取失败：${esc(err.message)}</p>`;
    return;
  }
  const items = (data && data.items) || [];
  if (!items.length) {
    box.innerHTML = `<p class="dim">没有找到可跳转的出现位置。</p>`;
    return;
  }
  const rows = items.map((o) => {
    const pk = o.pk != null ? o.pk : o.video_pk;
    const ms = o.start_ms != null ? o.start_ms : 0;
    return `<button type="button" class="occ-item" data-pk="${esc(pk)}" data-seq="${esc(o.seq)}" data-ms="${esc(ms)}" title="在视频里定位到这一句">
      <span class="occ-title dim">${esc(o.title || `视频 ${pk}`)}</span>
      <span class="occ-text">${esc(o.text || "")}</span>
    </button>`;
  });
  box.innerHTML =
    rows.join("") + (data && data.truncated ? `<p class="dim small">只显示前 ${items.length} 条</p>` : "");
}

function onVocabListClick(e) {
  const occ = e.target.closest("[data-pk]");
  if (occ && occ.classList.contains("occ-item")) {
    openVideoAt(Number(occ.dataset.pk), Number(occ.dataset.seq), Number(occ.dataset.ms));
    return;
  }
  const btn = e.target.closest("[data-vact]");
  if (!btn) return;
  const row = btn.closest(".vocab-row");
  if (!row) return;
  const id = Number(row.dataset.id);
  const item = state.vocab.items.find((x) => Number(x.id) === id);
  if (!item) return;
  const act = btn.dataset.vact;
  if (act === "known") patchVocabStatus(item, "known");
  else if (act === "delete") deleteVocabItem(item);
  else if (act === "occ") toggleOccurrences(row, item);
}

function exportVocab(format) {
  const fmt = ["tsv", "csv", "json"].includes(format) ? format : "tsv";
  // 后端直接回文件；交给浏览器下载，不走 fetch（避免把文件读进内存）
  window.location = `${API}/vocab/export?format=${fmt}`;
  toast(`正在导出 ${fmt.toUpperCase()}${fmt === "tsv" ? "（可直接导入 Anki）" : ""}`, "");
}

/* ------------------------------------------------------------ 复习模式 */

function escapeRegExp(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 来源句把目标词挖空；revealed 时显示原词。返回的是已转义的 HTML */
function reviewCloze(item, revealed) {
  const raw = String((item && item.source_sentence) || "").trim();
  const word = String((item && (item.word || item.lemma)) || "").trim();
  if (!raw) return `<span class="dim">（这个词没有记录来源句）</span>`;
  if (!word) return `<span class="review-plain">${esc(raw)}</span>`;
  const forms = [word];
  if (item.lemma && String(item.lemma).toLowerCase() !== word.toLowerCase()) forms.push(String(item.lemma));
  const pattern = new RegExp(
    `(?<![A-Za-z])(${forms.map(escapeRegExp).join("|")})(?![A-Za-z])`,
    "i"
  );
  const m = raw.match(pattern);
  if (!m) return `<span class="review-plain">${esc(raw)}</span>`;
  const before = raw.slice(0, m.index);
  const after = raw.slice(m.index + m[0].length);
  const hit = revealed
    ? `<span class="review-answer">${esc(m[0])}</span>`
    : `<span class="review-blank">＿＿＿</span>`;
  return `${esc(before)}${hit}${esc(after)}`;
}

async function startReview() {
  let data;
  try {
    data = await api("/vocab?status=due&limit=50&offset=0&sort=due");
  } catch (err) {
    toast(`读取待复习生词失败：${err.message}`, "err");
    return;
  }
  const items = (data && data.items) || [];
  if (!items.length) {
    toast("现在没有到期的生词，先去播放页收藏几个吧", "");
    return;
  }
  showView("vocab");
  state.review = {
    items,
    index: 0,
    revealed: false,
    submitting: false,
    done: false,
    graded: 0,
    error: "",
  };
  renderReview();
}

function closeReview() {
  const modal = $("review-modal");
  const had = state.review;
  if (modal) {
    modal.classList.add("hidden");
    modal.innerHTML = "";
  }
  document.body.classList.remove("modal-open");
  state.review = null;
  if (had && had.graded > 0) {
    refreshVocabBadge();
    if (state.vocab.loaded) loadVocab(true);
  }
}

function renderReviewDone(r) {
  return `<div class="review-box review-done">
    <div class="review-head"><span class="dim">生词复习</span><span class="wp-spacer"></span>
      <button type="button" class="icon-btn" data-review="close" title="关闭（Esc）">×</button></div>
    <h3>这一轮复习完成</h3>
    <p class="dim">复习了 ${esc(String(r.graded))} 个词。</p>
    <div class="review-actions"><button type="button" class="primary" data-review="close">完成</button></div>
  </div>`;
}

function renderReview() {
  const modal = $("review-modal");
  const r = state.review;
  if (!modal) return;
  if (!r) {
    modal.classList.add("hidden");
    modal.innerHTML = "";
    document.body.classList.remove("modal-open");
    return;
  }
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  if (r.done) {
    modal.innerHTML = renderReviewDone(r);
    return;
  }
  const item = r.items[r.index];
  if (!item) {
    closeReview();
    return;
  }
  const total = r.items.length;
  const left = total - r.index;
  const word = item.word || item.lemma || "";
  const tags = tagChipsHtml(item.tags);
  const gradeRow = `<div class="review-grades">
    <button type="button" class="grade g1" data-review="grade" data-grade="1"><b>1</b><span>忘了</span></button>
    <button type="button" class="grade g2" data-review="grade" data-grade="2"><b>2</b><span>模糊</span></button>
    <button type="button" class="grade g3" data-review="grade" data-grade="3"><b>3</b><span>记得</span></button>
    <button type="button" class="grade g4" data-review="grade" data-grade="4"><b>4</b><span>熟练</span></button>
  </div>`;
  let html = `<div class="review-box">
    <div class="review-head">
      <span class="dim">生词复习</span>
      <span class="wp-spacer"></span>
      <span class="review-count">${esc(String(r.index + 1))} / ${esc(String(total))}</span>
      <span class="review-left dim">剩余 ${esc(String(left))}</span>
      <button type="button" class="icon-btn" data-review="close" title="关闭（Esc）">×</button>
    </div>
    <div class="review-progress"><i style="width:${clampPct((r.index / Math.max(1, total)) * 100)}%"></i></div>
    <p class="review-source">${reviewCloze(item, r.revealed)}</p>
    <div class="review-wordrow">
      <span class="review-word">${esc(word)}</span>
      ${item.phonetic ? `<span class="review-phon">/${esc(item.phonetic)}/</span>` : ""}
      ${item.pos ? `<span class="dim">${esc(item.pos)}</span>` : ""}
      ${tags}
    </div>`;
  if (r.revealed) {
    html += `<p class="review-trans">${esc(item.translation || "（没有释义）")}</p>`;
    if (item.source_title) html += `<p class="review-src dim">来源：${esc(item.source_title)}</p>`;
    html += `<div class="review-actions">${r.submitting ? `<p class="dim">提交中…</p>` : gradeRow}</div>`;
  } else {
    html += `<p class="review-hint dim">按空格或点「显示答案」查看释义</p>`;
    html += `<div class="review-actions"><button type="button" class="primary" data-review="reveal">显示答案 <kbd>空格</kbd></button></div>`;
  }
  if (r.error) html += `<p class="vocab-err">${esc(r.error)}</p>`;
  html += `</div>`;
  modal.innerHTML = html;
}

function revealReview() {
  const r = state.review;
  if (!r || r.done || r.revealed) return;
  r.revealed = true;
  renderReview();
}

async function gradeReview(grade) {
  const r = state.review;
  if (!r || r.done || r.submitting || !r.revealed) return;
  const item = r.items[r.index];
  if (!item) return;
  const g = Number(grade);
  if (![1, 2, 3, 4].includes(g)) return;
  r.submitting = true;
  r.error = "";
  renderReview();
  try {
    await api(`/vocab/${item.id}/review`, {
      method: "POST",
      body: JSON.stringify({ grade: g }),
    });
  } catch (err) {
    r.submitting = false;
    r.error = `提交评分失败：${err.message}`;
    renderReview();
    return;
  }
  r.submitting = false;
  r.graded += 1;
  if (r.index + 1 >= r.items.length) {
    r.done = true;
    renderReview();
    refreshVocabBadge();
    if (state.vocab.loaded) loadVocab(true);
    return;
  }
  r.index += 1;
  r.revealed = false;
  r.error = "";
  renderReview();
}

/** 复习/弹层的键盘：1-4 评分、空格揭示、Esc 关闭 */
function onOverlayKeydown(e) {
  const modal = $("review-modal");
  const popover = $("word-popover");
  if (modal && !modal.classList.contains("hidden")) {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeReview();
      return;
    }
    const r = state.review;
    if (!r || r.done) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (e.key === " " || e.code === "Space") {
      e.preventDefault();
      e.stopPropagation();
      if (!r.revealed) revealReview();
      return;
    }
    if (r.revealed && ["1", "2", "3", "4"].includes(e.key)) {
      e.preventDefault();
      e.stopPropagation();
      gradeReview(Number(e.key));
    }
    return;
  }
  if (popover && !popover.classList.contains("hidden") && e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    closeWordPopover();
  }
}

/* ---------------------------------------------------------- 设置页词典卡 */

function dictBusy(st) {
  if (!st) return false;
  return Boolean(st.downloading) || st.phase === "downloading" || st.phase === "building";
}

function dictPhaseText(st) {
  if (!st) return "";
  if (dictBusy(st)) return st.phase === "building" ? "构建词典中" : "下载词典中";
  return { idle: "未安装", downloading: "下载中", building: "构建中", done: "已就绪", error: "安装失败" }[st.phase] || "";
}

function renderDictStatusCard() {
  const card = $("dict-status-card");
  if (!card) return;
  const st = state.dictStatus;
  if (!st) {
    card.className = "dict-status-card dim";
    const msg = state.dictStatusError
      ? `读取词典状态失败：${state.dictStatusError}`
      : "读取词典状态…";
    card.innerHTML = `<p class="dict-card-line">${esc(msg)}</p>`;
    return;
  }
  const busy = dictBusy(st);
  const installed = Boolean(st.installed);
  const pct = clampPct(st.progress);
  card.className = `dict-status-card ${busy ? "busy" : installed ? "ok" : st.phase === "error" ? "err" : ""}`;
  let html = "";
  if (busy) {
    const phase = dictPhaseText(st);
    html += `<div class="dict-card-line"><span class="dot"></span>${esc(phase)}${st.message ? ` · ${esc(st.message)}` : ""} · ${pct}%</div>`;
    html += `<div class="dict-progress"><i style="width:${pct}%"></i></div>`;
    html += `<div class="dict-card-actions"><button type="button" class="ghost small" data-dict="refresh">刷新</button></div>`;
  } else if (installed) {
    const bits = [`已安装 · ${fmtNumber(st.entries)} 词条`];
    if (st.forms) bits.push(`${fmtNumber(st.forms)} 词形`);
    bits.push(fmtBytes(st.size_bytes));
    if (st.built_at) bits.push(`构建于 ${fmtDateTime(st.built_at)}`);
    html += `<div class="dict-card-line">${esc(bits.join(" · "))}</div>`;
    if (st.path) html += `<div class="dict-card-line dim mono">${esc(st.path)}</div>`;
    html += `<div class="dict-card-actions">`;
    html += `<button type="button" class="ghost small" data-dict="rebuild">重建</button>`;
    html += `<button type="button" class="ghost small danger" data-dict="delete">删除</button>`;
    html += `</div>`;
  } else if (st.phase === "error") {
    html += `<div class="dict-card-line vocab-err">安装失败${st.error ? `：${esc(st.error)}` : ""}</div>`;
    html += `<div class="dict-card-actions"><button type="button" class="small primary" data-dict="install">重试下载</button></div>`;
  } else {
    html += `<div class="dict-card-line">本地词典未安装。下载 ECDICT 并构建后，字幕点词就能查音标、释义和考试标签。</div>`;
    html += `<div class="dict-card-actions"><button type="button" class="small primary" data-dict="install">下载并构建</button></div>`;
  }
  card.innerHTML = html;
}

function handleDictStatus(st) {
  const wasBusy = state.dictWasBusy;
  const wasInstalled = Boolean(state.dictStatus && state.dictStatus.installed);
  state.dictStatus = st || null;
  state.dictStatusError = "";
  const busy = dictBusy(st);
  state.dictWasBusy = busy;
  if (busy) startDictPoll();
  else stopDictPoll();
  if (wasBusy && !busy) {
    if (st && st.phase === "error") toast(`词典安装失败${st.error ? `：${st.error}` : ""}`, "err");
    else if (st && st.installed) toast("词典已就绪", "ok");
  }
  if (!wasInstalled && st && st.installed) {
    // 刚装好：补一下字幕打点和生词本数据
    if (state.current) loadKnownVocab();
    if (state.vocab.loaded) loadVocab(true);
  }
}

async function loadDictStatus() {
  try {
    const st = await api("/dict/status");
    handleDictStatus(st);
  } catch (err) {
    state.dictStatusError = err && err.message ? err.message : "读取失败";
    stopDictPoll();
  }
  renderDictStatusCard();
}

function startDictPoll() {
  if (state.dictTimer) return;
  state.dictTimer = setInterval(pollDictStatus, 1000);
}

function stopDictPoll() {
  if (state.dictTimer) {
    clearInterval(state.dictTimer);
    state.dictTimer = null;
  }
}

async function pollDictStatus() {
  try {
    const st = await api("/dict/status");
    handleDictStatus(st);
  } catch (_) {
    // 偶发失败保留定时器，下一轮再试
    return;
  }
  renderDictStatusCard();
}

async function dictInstall(force = false) {
  if (force && !confirm("重新下载并构建词典？当前的词典文件会被覆盖，构建期间不能查词。")) return;
  try {
    // body 可省；force 重建时才带 {"force": true}
    await api("/dict/install", force ? { method: "POST", body: JSON.stringify({ force: true }) } : { method: "POST" });
  } catch (err) {
    if (err.status === 409) {
      toast("词典正在下载或构建中，请稍候", "");
      loadDictStatus();
    } else {
      toast(`开始下载失败：${err.message}`, "err");
    }
    return;
  }
  toast(force ? "已开始重建词典" : "已开始下载词典", "ok");
  state.dictWasBusy = true;
  await loadDictStatus();
  startDictPoll();
}

async function dictDelete() {
  if (!confirm("删除本地词典？删除后不能查词，需要时可以重新下载。")) return;
  try {
    await api("/dict", { method: "DELETE" });
  } catch (err) {
    if (err.status === 409) toast("词典正在构建中，构建完再删", "");
    else toast(`删除失败：${err.message}`, "err");
    return;
  }
  toast("词典已删除", "ok");
  state.dictWasBusy = false;
  stopDictPoll();
  await loadDictStatus();
}

function onDictAction(e) {
  const btn = e.target.closest("[data-dict]");
  if (!btn) return;
  const act = btn.dataset.dict;
  if (act === "install") dictInstall(false);
  else if (act === "rebuild") dictInstall(true);
  else if (act === "delete") dictDelete();
  else if (act === "refresh") loadDictStatus();
}

/* ------------------------------------------------------------ 事件绑定 */

function onWordPopoverClick(e) {
  const btn = e.target.closest("[data-wp]");
  if (!btn) return;
  e.preventDefault();
  const act = btn.dataset.wp;
  const p = state.popover;
  if (act === "close") closeWordPopover();
  else if (act === "speak") speakWord(p ? p.word : "");
  else if (act === "collect") collectWord();
  else if (act === "known") markWordKnown();
  else if (act === "explain") explainWord();
  else if (act === "retry") retryLookup();
  else if (act === "settings") {
    closeWordPopover();
    openSettings();
  }
}

function onReviewClick(e) {
  const btn = e.target.closest("[data-review]");
  if (!btn) return;
  const act = btn.dataset.review;
  if (act === "close") closeReview();
  else if (act === "reveal") revealReview();
  else if (act === "grade") gradeReview(Number(btn.dataset.grade));
}

function bindDictVocabEvents() {
  $("nav-library").addEventListener("click", openLibraryView);
  $("nav-vocab").addEventListener("click", openVocabView);
  $("review-start").addEventListener("click", startReview);

  $("vocab-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-status]");
    if (!btn) return;
    const status = btn.dataset.status;
    if (!["all", "learning", "known", "ignored"].includes(status)) return;
    if (state.vocab.status === status) return;
    state.vocab.status = status;
    renderVocabTabs();
    loadVocab(true);
  });

  let searchTimer = null;
  $("vocab-q").addEventListener("input", (e) => {
    state.vocab.q = e.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadVocab(true), 250);
  });
  $("vocab-tag").addEventListener("change", (e) => {
    state.vocab.tag = e.target.value || "";
    loadVocab(true);
  });
  $("vocab-sort").addEventListener("change", (e) => {
    state.vocab.sort = e.target.value || "recent";
    loadVocab(true);
  });
  $("vocab-list").addEventListener("click", onVocabListClick);
  $("vocab-more").addEventListener("click", () => loadVocab(false));
  document.querySelectorAll("[data-export]").forEach((btn) => {
    btn.addEventListener("click", () => exportVocab(btn.dataset.export));
  });

  $("word-popover").addEventListener("click", onWordPopoverClick);
  $("popover-overlay").addEventListener("click", closeWordPopover);
  $("review-modal").addEventListener("click", (e) => {
    if (e.target === $("review-modal")) {
      closeReview();
      return;
    }
    onReviewClick(e);
  });

  // 设置页「词典」卡片的按钮：用委托，卡片重渲染后不用重新绑
  $("settings-body").addEventListener("click", onDictAction);

  window.addEventListener("resize", () => {
    if (state.popover) positionWordPopover();
  });
  // 滚动了锚点就偏了：直接关掉，别飘在错的地方
  window.addEventListener("scroll", () => {
    if (state.popover) closeWordPopover();
  }, true);

  // capture 阶段先处理弹层/复习框，避免被播放器的 Esc/空格快捷键抢走
  document.addEventListener("keydown", onOverlayKeydown, true);
}

/* ------------------------------------------------------------------ 事件 */

function bindEvents() {
  bindDictVocabEvents();
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
        // 单击单词：弹查词卡，不跳转（点行内非词区域才保持原来的跳转行为）
        openWordPopover(word, seg, idx, wi);
        return;
      }
    }
    const idx = Number(line.dataset.i);
    if (Number.isFinite(idx)) seekTo(state.segments[idx].start_ms, { play: true });
  });

  $("video").addEventListener("pause", () => savePlayback());
  $("video").addEventListener("seeked", onVideoSeeked);

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
    if ($("player-view").classList.contains("hidden")) return;
    if (!state.current) return;
    const el = $("video");
    const isSpace = e.key === " " || e.code === "Space";

    // 空格在播放页里永远等于播放/暂停。
    // 焦点若停在输入框 / 下拉 / 复选框上，浏览器会先把空格交给控件（打字、拉开下拉、勾选），
    // 快捷键就「不好使」了——所以这里先 preventDefault，再把焦点收回来自己处理。
    if (isSpace) {
      if (e.isComposing || e.repeat) return;
      e.preventDefault();
      const active = document.activeElement;
      if (
        active &&
        active !== document.body &&
        typeof active.blur === "function"
      ) {
        active.blur();
      }
      if (el.paused) el.play().catch(() => {});
      else el.pause();
      return;
    }

    // select 也要放过：否则焦点在「精听」下拉里时 ↑↓ 会同时跳字幕行
    if (tag === "input" || tag === "textarea" || tag === "select") return;

    switch (e.key) {
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
      case "Escape": {
        // ESC 一次退掉所有「特殊播放状态」：AB 循环、单句复读、0.75 倍速。
        // 否则调慢速之后想回到正常速度，只能再摸一次 S 键，容易以为「被卡住了」。
        const vEl = $("video");
        const slow = Math.abs(vEl.playbackRate - 1) > 1e-6;
        const hasAB = state.ab.a != null || state.ab.b != null;
        const replaying = state.replayUntil != null;
        if (slow || hasAB || replaying) {
          e.preventDefault();
          const parts = [];
          if (replaying) parts.push("单句复读");
          if (hasAB) parts.push("AB 循环");
          if (slow) parts.push("0.75 倍速");

          state.replayUntil = null;
          state.ab = { a: null, b: null };
          state.abJump = false;
          refreshABBadge();
          if (slow) {
            vEl.playbackRate = 1;
            refreshRateBadge();
            savePlayback();
          }
          toast(
            `已退出：${parts.join(" / ")}${slow ? "，已回到 1.0 倍速" : ""}`,
            "ok",
          );
        }
        break;
      }
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
const GROUP_ORDER = ["网络", "下载", "转写", "解析", "词典", "切分", "服务"];

async function openSettings() {
  const el = $("video");
  if (el && !el.paused) el.pause();
  // 记住从哪儿进来的：从生词本进来，关掉设置就回生词本
  state.prevView = !$("vocab-view").classList.contains("hidden")
    ? "vocab"
    : state.current ? "player" : "library";
  try {
    state.settings = await api("/settings");
  } catch (err) {
    toast(`读取设置失败：${err.message}`, "err");
    return;
  }
  renderSettings();
  showView("settings");
  loadDictStatus();
}

function closeSettings() {
  if (state.prevView === "vocab") {
    showView("vocab");
    refreshVocabBadge();
    loadVocab(true);
    return;
  }
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
  // 后端还没加「词典」字段也没关系：按接口契约的三个配置项兜底渲染。
  // 后端一旦返回同组字段，就用后端的（不重复加）。
  if (!groups.has("词典")) {
    groups.set("词典", [
      {
        path: "dict.db_path",
        label: "词典数据库路径",
        group: "词典",
        type: "path",
        hint: "ECDICT 的 SQLite 文件；相对路径相对项目根目录",
      },
      {
        path: "dict.source_url",
        label: "ECDICT 下载地址",
        group: "词典",
        type: "text",
        hint: "下载 ECDICT CSV 的地址",
      },
      {
        path: "dict.max_rank",
        label: "收录词数上限（rank）",
        group: "词典",
        type: "int",
        min: 5000,
        max: 500000,
        hint: "只收录排名在这个范围内的词（5000–500000）",
      },
    ]);
  }
  // GET /api/settings 顺带返回的 dict 摘要可以先当状态卡数据
  if (data.dict && !state.dictStatus) state.dictStatus = data.dict;
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

    // 「词典」组顶部：状态卡（状态/进度/下载/重建/删除）
    if (name === "词典") {
      const status = document.createElement("div");
      status.id = "dict-status-card";
      status.className = "dict-status-card dim";
      status.textContent = "读取词典状态…";
      card.appendChild(status);
    }

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
  renderDictStatusCard();
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
  refreshVocabBadge();
}

boot();
