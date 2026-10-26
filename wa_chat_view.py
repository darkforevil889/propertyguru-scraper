"""Full-screen WhatsApp-style chat window for the online dashboard's "WhatsApp chats" page (a Streamlit component).

Same look as the PC's chat window (wa_chat_ui.html): chats on the left, the conversation on the right, reply box at
the bottom; on a phone the list and the conversation take turns. The page keeps itself on screen between refreshes
(a voice note keeps playing, what you're typing stays), and only redraws what changed.
app.py passes the data (chat list, the open chat's messages, picture links) and gets back two events:
  open = a chat's key (you tapped it), send = {key, text} (you pressed send - the PC sends it).
"""
import streamlit as st

CSS = """
.app {
  --bg: #f4f6f5; --panel: #ffffff; --line: #e6e9e8; --text: #1f2a2e; --muted: #8a9599; --green: #25c35a;
  --green-dark: #1ea64c; --bubble-in: #eef1f2; --bubble-out: #25c35a; --out-text: #ffffff; --hover: #f2f7f4;
  --active: #e5f6eb; --warn-bg: #fff4d6; --warn-text: #7a5b00;
}
.app.dark {
  --bg: #0b141a; --panel: #111b21; --line: #222d34; --text: #e9edef; --muted: #8696a0; --green: #25d366;
  --green-dark: #1fae55; --bubble-in: #202c33; --bubble-out: #005c4b; --out-text: #e9edef; --hover: #1d282f;
  --active: #2a3942; --warn-bg: #3a3214; --warn-text: #f0d68a;
}
* { box-sizing: border-box; }
.app { display: flex; height: calc(100dvh - 110px); min-height: 420px; border: 1px solid var(--line);
  border-radius: 10px; overflow: hidden; background: var(--panel); color: var(--text);
  font: 14px/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
/* ---------- chat list ---------- */
.side { width: 34%; min-width: 260px; max-width: 420px; display: flex; flex-direction: column;
  border-right: 1px solid var(--line); }
.search { padding: 12px; border-bottom: 1px solid var(--line); }
.search input { width: 100%; padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg); color: var(--text); font-size: 14px; outline: none; }
.list { flex: 1; overflow-y: auto; }
.row { display: flex; gap: 12px; align-items: center; padding: 10px 14px; cursor: pointer;
  border-bottom: 1px solid var(--line); }
.row:hover { background: var(--hover); }
.row.on { background: var(--active); }
.row .mid { flex: 1; min-width: 0; }
.row .name { font-weight: 600; color: var(--green-dark); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.row .prev { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 13px; }
.row .time { color: var(--muted); font-size: 12px; white-space: nowrap; align-self: flex-start; padding-top: 2px; }
.av { width: 46px; height: 46px; border-radius: 50%; flex: none; object-fit: cover; background: #cfd8d4;
  display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 600; font-size: 17px; }
.av.sm { width: 38px; height: 38px; font-size: 14px; }
/* ---------- conversation ---------- */
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.warn { background: var(--warn-bg); color: var(--warn-text); padding: 7px 16px; font-size: 13px; }
.warn:empty { display: none; }
.head { display: flex; gap: 12px; align-items: center; padding: 10px 16px; border-bottom: 1px solid var(--line);
  min-height: 60px; }
.head .who { font-weight: 600; }
.head .num { color: var(--muted); font-size: 12px; }
.back { display: none; border: 0; background: none; color: var(--green-dark); font-size: 26px; cursor: pointer;
  padding: 0 4px; }
.thread { flex: 1; overflow-y: auto; padding: 18px 22px; }
.day { text-align: center; margin: 14px 0; }
.day span { background: var(--bubble-in); color: var(--muted); padding: 4px 12px; border-radius: 8px; font-size: 12px; }
.msg { display: flex; gap: 10px; align-items: flex-end; margin: 8px 0; }
.msg.me { flex-direction: row-reverse; }
.msg .col { max-width: 62%; display: flex; flex-direction: column; }
.msg.me .col { align-items: flex-end; }
.bubble { padding: 9px 13px; border-radius: 16px; background: var(--bubble-in); white-space: pre-wrap;
  word-wrap: break-word; overflow-wrap: anywhere; }
.msg.me .bubble { background: var(--bubble-out); color: var(--out-text); }
.msg.me .bubble a { color: var(--out-text); }
.msg.wait .bubble { opacity: .6; }
.bubble.media { padding: 4px; background: transparent; }
.msg.me .bubble.media { background: transparent; }
.bubble img.photo { display: block; max-width: 280px; max-height: 320px; border-radius: 12px; cursor: zoom-in; }
.bubble img.sticker { width: 130px; height: 130px; object-fit: contain; }
.bubble audio { width: 260px; max-width: 100%; }
.from { font-size: 12px; color: var(--green-dark); font-weight: 600; margin: 0 4px 2px; }
.meta { font-size: 11px; color: var(--muted); margin: 3px 6px 0; }
.tick { color: var(--green); }
.empty { color: var(--muted); text-align: center; margin-top: 25%; }
.compose { display: flex; gap: 10px; padding: 12px 16px; border-top: 1px solid var(--line); }
.compose textarea { flex: 1; resize: none; height: 42px; max-height: 120px; padding: 10px 14px; border-radius: 21px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); font: inherit; outline: none; }
.compose button { border: 0; border-radius: 50%; width: 42px; height: 42px; background: var(--green); color: #fff;
  font-size: 18px; cursor: pointer; flex: none; }
.compose button:disabled { opacity: .5; cursor: default; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 16px; }
.chips:not(:empty) { padding: 8px 16px 0; }
.chip { border: 1px solid var(--green); color: var(--green-dark); background: var(--panel); border-radius: 16px;
  padding: 6px 12px; cursor: pointer; font: inherit; font-size: 13px; text-align: left; }
.chip:hover { background: var(--active); }
.zoom { position: fixed; inset: 0; background: rgba(0,0,0,.85); display: none; align-items: center;
  justify-content: center; z-index: 999999; cursor: zoom-out; }
.zoom img { max-width: 92vw; max-height: 92vh; border-radius: 8px; }
@media (max-width: 720px) {
  .app { height: calc(100dvh - 80px); }
  .side { width: 100%; max-width: none; }
  .main { display: none; }
  .app.open .side { display: none; }
  .app.open .main { display: flex; }
  .back { display: block; }
  .thread { padding: 12px; }
  .msg .col { max-width: 80%; }
}
"""

HTML = """
<div class="app" id="app">
  <div class="side">
    <div class="search"><input id="q" placeholder="Search…" autocomplete="off"></div>
    <div class="list" id="list"></div>
  </div>
  <div class="main">
    <div class="warn" id="warn"></div>
    <div class="head" id="head"></div>
    <div class="thread" id="thread"><div class="empty">Pick a chat</div></div>
    <div class="chips" id="chips"></div>
    <div class="compose">
      <textarea id="box" placeholder="Type a message…" disabled></textarea>
      <button id="send" title="Send" disabled>➤</button>
    </div>
  </div>
</div>
<div class="zoom" id="zoom"><img id="zoomimg" alt=""></div>
"""

JS = """
const esc = s => (s || "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const linkify = s => esc(s).replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
const COLORS = ["#7fb3a3", "#e39b7b", "#8fa6d6", "#c99bd6", "#d6b46f", "#6fb8d6", "#d67f8f"];
const KIND = {photo: "📷 Photo", pdf: "📄 PDF", video: "🎬 Video", zip: "🗂 Files", voice: "🎤 Voice message",
              sticker: "Sticker"};

function initials(name) {
  const w = (name || "?").replace(/[^\\p{L}\\p{N} ]/gu, "").trim().split(/\\s+/);
  return ((w[0] || "?")[0] + (w[1] ? w[1][0] : "")).toUpperCase();
}
function avatar(c, cls) {
  const color = COLORS[[...(c.key || "x")].reduce((a, ch) => a + ch.charCodeAt(0), 0) % COLORS.length];
  const fallback = `<div class="av ${cls || ""}" style="background:${color}">${esc(initials(c.name || c.phone))}</div>`;
  if (!c.pic) return fallback;
  return `<img class="av ${cls || ""}" src="${esc(c.pic)}" alt="" onerror="this.outerHTML=this.dataset.fb"
          data-fb='${fallback.replace(/'/g, "&#39;")}'>`;
}
function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], {day: "2-digit", month: "short"});
}
function dayLabel(ts) {
  const d = new Date(ts * 1000), now = new Date(), y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], {day: "numeric", month: "long", year: "numeric"});
}
function title(c) { return (c.is_group ? "👥 " : "") + (c.name || ("+" + c.phone)); }

export default function ({data, parentElement, setTriggerValue}) {
  const $ = id => parentElement.querySelector("#" + id);
  // the page stays between refreshes - what we drew / you picked lives on it
  const S = parentElement.__wa || (parentElement.__wa = {current: null, listSig: "", headSig: null, threadSig: "", sent: []});
  S.data = data;
  S.trigger = setTriggerValue;
  if (S.current === null) S.current = data.current;
  $("app").classList.toggle("dark", !!data.dark);
  $("warn").textContent = data.warning || "";
  const chatOf = key => (S.data.chats || []).find(c => c.key === key);

  function drawList() {
    const q = $("q").value.trim().toLowerCase();
    const shown = (S.data.chats || []).filter(c => !q || (c.name || "").toLowerCase().includes(q) || (c.phone || "").includes(q));
    const sig = q + "|" + S.current + "|" + shown.map(c => c.key + c.last_ts + (c.pic ? 1 : 0)).join(",");
    if (sig === S.listSig) return;
    S.listSig = sig;
    $("list").innerHTML = shown.map(c => `
      <div class="row ${c.key === S.current ? "on" : ""}" data-key="${esc(c.key)}">
        ${avatar(c)}
        <div class="mid"><div class="name">${esc(title(c))}</div><div class="prev">${esc(c.preview || "")}</div></div>
        <div class="time">${when(c.last_ts)}</div>
      </div>`).join("") || '<div class="empty" style="margin-top:20px">No chats</div>';
    $("list").querySelectorAll(".row").forEach(r => r.onclick = () => openChat(r.dataset.key, true));
  }
  function drawHead() {
    const c = chatOf(S.current);
    const sig = c ? c.key + c.name + c.pic : "";
    if (sig === S.headSig) return;
    S.headSig = sig;
    if (!c) { $("head").innerHTML = ""; return; }
    $("head").innerHTML = `<button class="back" id="back">‹</button>${avatar(c, "sm")}
      <div><div class="who">${esc(title(c))}</div>
      ${c.phone && !c.is_group ? `<div class="num">+${esc(c.phone)}</div>` : ""}</div>`;
    $("back").onclick = () => $("app").classList.remove("open");
    $("box").disabled = $("send").disabled = false;
    $("box").placeholder = "Type a message to " + (c.name || "+" + c.phone) + "…";
  }
  function openChat(key, tapped) {
    if (tapped) $("app").classList.add("open");
    if (key === S.current) return;
    S.current = key;
    S.threadSig = "";
    S.sent = [];
    $("thread").innerHTML = '<div class="empty">Loading…</div>';
    $("chips").innerHTML = "";
    $("box").value = "";
    drawHead();
    drawList();
    S.trigger("open", key);
  }
  function mediaHtml(m) {
    if (!m.url) return KIND[m.kind] || "";
    if (m.kind === "photo") return `<img class="photo" src="${esc(m.url)}" loading="lazy" alt="photo" data-zoom="1"
                                     onerror="this.outerHTML='📷 Photo'">`;
    if (m.kind === "sticker") return `<img class="sticker" src="${esc(m.url)}" loading="lazy" alt="sticker"
                                       onerror="this.outerHTML='Sticker'">`;
    if (m.kind === "voice") return `<audio controls preload="none" src="${esc(m.url)}"></audio>`;
    return KIND[m.kind] || "";
  }
  function drawThread() {
    if (S.data.current !== S.current) return;  // still loading the chat you just tapped
    const c = chatOf(S.current), msgs = S.data.messages || [];
    // your replies waiting for the PC: the ones the server knows + the ones just sent from here
    const waiting = [...(S.data.waiting || [])];
    for (const t of S.sent) if (!waiting.includes(t) && !msgs.some(m => m.me && m.text === t)) waiting.push(t);
    const sig = S.current + "|" + msgs.length + "|" + (msgs.length ? msgs[msgs.length - 1].id : "") + "|" + waiting.join("\\u0001");
    if (sig === S.threadSig) return;  // nothing new - leave it (a voice note keeps playing)
    S.threadSig = sig;
    const t = $("thread");
    const first = !t.querySelector(".msg");
    const nearBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 120;
    let html = "", lastDay = "";
    for (const m of msgs) {
      const day = dayLabel(m.ts);
      if (day !== lastDay) { html += `<div class="day"><span>${day}</span></div>`; lastDay = day; }
      const media = m.kind ? mediaHtml(m) : "";
      const who = c && c.is_group && !m.me && m.who ? `<div class="from">${esc(m.who)}</div>` : "";
      html += `<div class="msg ${m.me ? "me" : ""}">${m.me || !c ? "" : avatar(c, "sm")}<div class="col">${who}
        ${media ? `<div class="bubble ${m.text ? "" : "media"}">${media}${m.text ? "<br>" + linkify(m.text) : ""}</div>`
                : `<div class="bubble">${linkify(m.text)}</div>`}
        <div class="meta">${new Date(m.ts * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}
        ${m.me ? '<span class="tick">✓✓</span>' : ""}</div></div></div>`;
    }
    for (const w of waiting) {
      html += `<div class="msg me wait"><div class="col"><div class="bubble">${linkify(w)}</div>
        <div class="meta">⏳ sending through your PC…</div></div></div>`;
    }
    t.innerHTML = html || '<div class="empty">No messages copied for this chat yet</div>';
    t.querySelectorAll("img[data-zoom]").forEach(i => i.onclick = () => zoom(i.src));
    if (first || nearBottom) t.scrollTop = t.scrollHeight;
    const suggest = msgs.length && !msgs[msgs.length - 1].me && !waiting.length ? (S.data.suggest || []) : [];
    $("chips").innerHTML = suggest.map(s => `<button class="chip" title="Put in the message box">⚡ ${esc(s)}</button>`).join("");
    $("chips").querySelectorAll(".chip").forEach((b, i) => b.onclick = () => { $("box").value = suggest[i]; $("box").focus(); });
  }
  function send() {
    const text = $("box").value.trim();
    if (!text || !S.current) return;
    S.sent.push(text);
    S.trigger("send", {key: S.current, text, at: Date.now()});
    $("box").value = "";
    $("box").focus();
    drawThread();
  }
  function zoom(src) { $("zoomimg").src = src; $("zoom").style.display = "flex"; }

  $("zoom").onclick = () => $("zoom").style.display = "none";
  $("send").onclick = send;
  $("box").onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  $("q").oninput = drawList;
  if (!chatOf(S.current)) S.current = data.current;
  drawList();
  drawHead();
  drawThread();
}
"""

chat_view = st.components.v2.component("wa_chat_view", html=HTML, css=CSS, js=JS)
