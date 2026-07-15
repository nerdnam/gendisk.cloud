/* genDISK 공개 공유 페이지 — 로그인 없이 링크로 열람·다운로드 (읽기 전용). */
const $ = (id) => document.getElementById(id);

// /s/<token> 에서 토큰 추출
const TOKEN = decodeURIComponent(location.pathname.replace(/^\/s\//, "").replace(/\/+$/, ""));
const API = `/api/public/share/${encodeURIComponent(TOKEN)}`;

const KIND_ICONS = { folder: "📁", image: "🖼️", video: "🎬", audio: "🎵", file: "📄" };

let META = null;         // {name, is_dir, protected, unlocked, expires_at}
let curPath = "";        // 폴더 공유일 때 현재 하위 경로

/* ---------- 테마 토글 ---------- */
(function () {
  const html = document.documentElement;
  const btn = $("theme-btn");
  const sync = () => { btn.textContent = html.getAttribute("data-theme") === "dark" ? "☀️" : "🌙"; };
  sync();
  btn.addEventListener("click", () => {
    const next = html.getAttribute("data-theme") === "dark" ? "light" : "dark";
    html.setAttribute("data-theme", next);
    try { localStorage.setItem("ncloud_theme", next); } catch (e) {}
    sync();
  });
})();

/* ---------- 유틸 ---------- */
function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }

function formatSize(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${v} ${u[i]}` : `${v.toFixed(1)} ${u[i]}`;
}

function dlUrl(relPath) {
  return `${API}/download?path=${encodeURIComponent(relPath)}`;
}
function rawUrl(relPath) {
  return `${API}/raw?path=${encodeURIComponent(relPath)}`;
}

async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) {
    let msg = `오류 (${res.status})`;
    try { const d = await res.json(); if (typeof d.detail === "string") msg = d.detail; } catch (e) {}
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return res.json();
}

function fatal(message) {
  hide("share-loading"); hide("share-lock"); hide("share-content");
  $("share-error").textContent = message;
  show("share-error");
}

/* ---------- 초기 로드 ---------- */
async function boot() {
  if (!TOKEN) { fatal("잘못된 공유 링크입니다."); return; }
  try {
    META = await apiGet(API);
  } catch (err) {
    if (err.status === 404) fatal("공유를 찾을 수 없습니다. 링크가 해제되었을 수 있습니다.");
    else if (err.status === 410) fatal("만료된 공유 링크입니다.");
    else fatal(err.message);
    return;
  }
  hide("share-loading");
  if (META.protected && !META.unlocked) {
    show("share-lock");
    $("lock-pw").focus();
    return;
  }
  render();
}

/* ---------- 비밀번호 잠금 해제 ---------- */
$("lock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("lock-error").textContent = "";
  const pw = $("lock-pw").value;
  try {
    const res = await fetch(`${API}/unlock`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    if (!res.ok) {
      $("lock-error").textContent = res.status === 401 ? "비밀번호가 올바르지 않습니다." : `오류 (${res.status})`;
      return;
    }
  } catch (err) {
    $("lock-error").textContent = "네트워크 오류가 발생했습니다.";
    return;
  }
  // 언락 성공 → 메타 다시 로드 후 렌더
  hide("share-lock");
  show("share-loading");
  META = await apiGet(API).catch(() => null);
  hide("share-loading");
  if (!META) { fatal("공유를 여는 중 문제가 발생했습니다."); return; }
  render();
});

/* ---------- 렌더 ---------- */
function render() {
  show("share-content");
  $("share-title-name").textContent = META.name || "공유";
  $("share-title-icon").textContent = META.is_dir ? "📁" : (KIND_ICONS[kindOf(META.name)] || "📄");
  if (META.is_dir) {
    hide("share-file");
    $("share-file-download").classList.add("hidden");
    browse("");
  } else {
    hide("share-list"); hide("share-empty"); hide("share-crumb");
    renderSingleFile();
  }
}

function kindOf(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (["jpg", "jpeg", "png", "gif", "webp", "bmp"].includes(ext)) return "image";
  if (["mp4", "webm", "mov", "mkv", "avi"].includes(ext)) return "video";
  if (["mp3", "wav", "ogg", "flac", "m4a"].includes(ext)) return "audio";
  return "file";
}

/* ----- 단일 파일 공유 ----- */
function renderSingleFile() {
  const kind = kindOf(META.name);
  const dl = $("share-file-download");
  dl.href = dlUrl("");          // 파일 공유는 path 무시
  dl.classList.remove("hidden");

  const box = $("share-file");
  box.innerHTML = "";
  const card = document.createElement("div");
  card.className = "file-card";
  if (kind === "image") {
    const img = document.createElement("img");
    img.src = rawUrl(""); img.alt = META.name;
    card.appendChild(img);
  } else if (kind === "video") {
    const v = document.createElement("video");
    v.src = rawUrl(""); v.controls = true;
    card.appendChild(v);
  } else if (kind === "audio") {
    const a = document.createElement("audio");
    a.src = rawUrl(""); a.controls = true;
    card.appendChild(a);
  } else {
    const ic = document.createElement("div");
    ic.className = "file-card-icon"; ic.textContent = "📄";
    card.appendChild(ic);
    const hint = document.createElement("p");
    hint.className = "file-card-hint";
    hint.textContent = "미리보기를 지원하지 않는 파일입니다. 위 버튼으로 다운로드하세요.";
    card.appendChild(hint);
  }
  box.appendChild(card);
  show("share-file");
}

/* ----- 폴더 공유 브라우징 ----- */
async function browse(path) {
  curPath = path;
  const grid = $("share-list");
  grid.innerHTML = "";
  hide("share-empty");
  let data;
  try {
    data = await apiGet(`${API}/list?path=${encodeURIComponent(path)}`);
  } catch (err) {
    if (err.status === 401) { location.reload(); return; } // 언락 만료 → 다시 로드
    fatal(err.message); return;
  }
  renderCrumb(data.path);
  if (!data.entries.length) { show("share-empty"); return; }
  for (const entry of data.entries) {
    grid.appendChild(entryCard(entry));
  }
  show("share-list");
}

function renderCrumb(path) {
  const crumb = $("share-crumb");
  crumb.innerHTML = "";
  const root = document.createElement("a");
  root.textContent = `📁 ${META.name}`;
  root.addEventListener("click", () => browse(""));
  crumb.appendChild(root);
  const parts = path ? path.split("/") : [];
  let acc = "";
  parts.forEach((part, i) => {
    acc = acc ? `${acc}/${part}` : part;
    const sep = document.createElement("span");
    sep.className = "sep"; sep.textContent = "›";
    crumb.appendChild(sep);
    if (i === parts.length - 1) {
      const cur = document.createElement("span");
      cur.className = "current"; cur.textContent = part;
      crumb.appendChild(cur);
    } else {
      const link = document.createElement("a");
      link.textContent = part;
      const target = acc;
      link.addEventListener("click", () => browse(target));
      crumb.appendChild(link);
    }
  });
  crumb.classList.remove("hidden");
}

function entryCard(entry) {
  const card = document.createElement("div");
  card.className = "entry";

  const icon = document.createElement("div");
  icon.className = "icon";
  if (entry.kind === "image") {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = rawUrl(entry.path);
    img.onerror = () => { icon.textContent = KIND_ICONS.image; };
    icon.appendChild(img);
  } else {
    icon.textContent = KIND_ICONS[entry.kind] || KIND_ICONS.file;
  }
  card.appendChild(icon);

  const name = document.createElement("div");
  name.className = "name"; name.textContent = entry.name; name.title = entry.name;
  card.appendChild(name);

  const meta = document.createElement("div");
  meta.className = "meta";
  const date = new Date(entry.mtime * 1000).toLocaleDateString("ko-KR");
  meta.textContent = entry.is_dir ? date : `${formatSize(entry.size)} · ${date}`;
  card.appendChild(meta);

  if (!entry.is_dir) {
    const actions = document.createElement("div");
    actions.className = "actions";
    const dl = document.createElement("a");
    dl.textContent = "⬇"; dl.title = "다운로드";
    dl.href = dlUrl(entry.path);
    dl.onclick = (e) => e.stopPropagation();
    actions.appendChild(dl);
    card.appendChild(actions);
  }

  card.onclick = () => {
    if (entry.is_dir) browse(entry.path);
    else if (["image", "video", "audio"].includes(entry.kind)) openPreview(entry);
    else location.href = dlUrl(entry.path);
  };
  return card;
}

/* ---------- 미리보기 모달 ---------- */
function openPreview(entry) {
  $("preview-name").textContent = entry.name;
  $("preview-download").href = dlUrl(entry.path);
  const c = $("preview-content");
  c.innerHTML = "";
  if (entry.kind === "image") {
    const img = document.createElement("img"); img.src = rawUrl(entry.path); c.appendChild(img);
  } else if (entry.kind === "video") {
    const v = document.createElement("video"); v.src = rawUrl(entry.path); v.controls = true; v.autoplay = true; c.appendChild(v);
  } else if (entry.kind === "audio") {
    const a = document.createElement("audio"); a.src = rawUrl(entry.path); a.controls = true; a.autoplay = true; c.appendChild(a);
  }
  $("preview-modal").classList.remove("hidden");
}
function closePreview() {
  $("preview-modal").classList.add("hidden");
  $("preview-content").innerHTML = "";
}
$("preview-close").addEventListener("click", closePreview);
document.querySelector(".preview-backdrop").addEventListener("click", closePreview);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("preview-modal").classList.contains("hidden")) closePreview();
});

boot();
