// 通信と記録の土台（仕様 v1.2 §2, §15.2）。
// - イベントには連番 client_seq と端末の単調時刻 client_t（ページ読込からの ms）を付け、約1秒ごとにまとめて送る。
// - サーバが適用済み（acked）と返すまで端末（localStorage）に保持して再送する。開き直したら先に送る。
// - 操作（実行・質問・提出など）は after_seq を付け、未適用のイベントがあればサーバが断るので、送り切ってから再試行する。
"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const PAGE_ID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)) + "";
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(c));
  return e;
};

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.hidden = true), 3500);
}

let pageBlocked = false;     // 別のタブで開かれた
function blockPage(msg) {
  pageBlocked = true;
  document.body.textContent = "";
  document.body.append(el("main", { class: "waiting" }, el("p", { text: msg })));
}

async function api(path, body) {
  const opt = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(j.error || r.statusText);
    e.status = r.status;
    e.body = j;
    if (j.error === "other_page") blockPage("このページは別のタブ（またはウィンドウ）で開かれています。このタブは閉じてください。");
    throw e;
  }
  return j;
}

// ------------------------------------------------------------------ 連番つきの送信
const Q = {
  key: "aisup:" + TOKEN,
  seq: 0,          // 最後に付けた連番
  items: [],       // 未適用（acked になっていない）イベント
  load() {
    try {
      const d = JSON.parse(localStorage.getItem(this.key) || "{}");
      this.seq = d.seq || 0;
      this.items = Array.isArray(d.items) ? d.items : [];
    } catch (_) { /* 使えない環境ではメモリだけで動く */ }
  },
  save() {
    try { localStorage.setItem(this.key, JSON.stringify({ seq: this.seq, items: this.items })); }
    catch (_) { /* 容量不足など。メモリには残る */ }
  },
  ack(n) {
    const before = this.items.length;
    this.items = this.items.filter((it) => it.client_seq > n);
    if (this.items.length !== before) this.save();
  },
};
Q.load();

function enqueue(type, payload) {
  if (pageBlocked) return;
  Q.seq += 1;
  Q.items.push({ client_seq: Q.seq, type, payload, client_t: Math.round(performance.now()), page_id: PAGE_ID });
  Q.save();
  if (Q.items.length >= 50) flush();
}

let flushing = null;
let onServerVersion = null;   // (version, status) => void（画面側が登録）
async function sendBatch(bodyPage) {
  const batch = Q.items.slice(0, 200);
  const r = await api("/api/events", { page_id: bodyPage, events: batch });
  Q.ack(r.acked);
  if (onServerVersion) onServerVersion(r.version, r.status);
  return r;
}

function flush() {
  if (pageBlocked) return Promise.resolve();
  if (flushing) return flushing;
  if (!Q.items.length) return Promise.resolve();
  flushing = (async () => {
    try {
      for (let i = 0; i < 5 && Q.items.length; i++) {
        const before = Q.items.length;
        await sendBatch(PAGE_ID);
        if (Q.items.length === before) break;    // 欠落待ち（サーバが先へ進めない）なら次の周期で再送
      }
    } catch (e) {
      if (e.status !== 409) netWarn();
    } finally {
      flushing = null;
    }
  })();
  return flushing;
}
setInterval(flush, 1000);

let warnAt = 0;
function netWarn() {
  const now = Date.now();
  if (now - warnAt > 15000) { toast("通信が不安定です。記録は端末に保存され、つながると自動で送られます。"); warnAt = now; }
}

// 操作の送信。未送信のイベントを送り切ってから、after_seq を付けて送る
async function action(path, body = {}) {
  for (let i = 0; i < 4; i++) {
    await flush();
    try {
      return await api(path, { ...body, after_seq: Q.seq, page_id: PAGE_ID });
    } catch (e) {
      if (e.body && e.body.error === "not_synced") { await new Promise((r) => setTimeout(r, 400)); continue; }
      throw e;
    }
  }
  throw new Error("同期できませんでした。通信を確認してください。");
}

// 開き直したとき：前のページで送れなかったイベントを先に送る（§15.4）
async function flushPrevious() {
  if (!Q.items.length) return;
  const prevPage = Q.items[0].page_id;
  try { await sendBatch(prevPage); } catch (_) { /* hello の後に現在のページとして送る */ }
}

// ------------------------------------------------------------------ タブ離脱（FOCUS_CHANGE）
let away = null;
function checkAway(reason) {
  const now = document.hidden || !document.hasFocus();
  if (now === away) return;
  away = now;
  enqueue("FOCUS_CHANGE", { away: now, reason });
  if (!now) flush();
}
document.addEventListener("visibilitychange", () => checkAway(document.hidden ? "hidden" : "visible"));
window.addEventListener("blur", () => checkAway("blur"));
window.addEventListener("focus", () => checkAway("focus"));

// ------------------------------------------------------------------ 貼り付けの判定（§15.2）
// このページの中でコピーした文字列だけを貼り付けられる（AI 回答・エディタ・出力欄など）。外部からは遮断する。
const recentClips = [];
const normClip = (s) => (s || "").replace(/\r\n?/g, "\n").replace(/\s+$/, "");
function rememberClip(text) {
  const t = normClip(text);
  if (!t) return;
  recentClips.unshift(t);
  recentClips.length = Math.min(recentClips.length, 20);
}
document.addEventListener("copy", (e) => rememberClip(selectedText(e.target)), true);
document.addEventListener("cut", (e) => rememberClip(selectedText(e.target)), true);
function selectedText(target) {
  if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT")) {
    return target.value.substring(target.selectionStart, target.selectionEnd);
  }
  const sel = document.getSelection();
  return sel ? sel.toString() : "";
}
// このページ内で始まったドラッグ（外部からのドロップと区別する）
let pageDrag = false;
document.addEventListener("dragstart", () => (pageDrag = true), true);
document.addEventListener("dragend", () => (pageDrag = false), true);

function pasteAllowed(text) {
  const t = normClip(text);
  return !t || recentClips.includes(t);
}

// textarea に貼り付けの制限を付ける（エディタ・支援の小エディタ・テストのコード欄）
function guardPaste(ta, via) {
  ta.addEventListener("paste", (e) => {
    const t = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
    if (pasteAllowed(t)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    enqueue("PASTE_BLOCKED", { length: t.length, via: via + ":paste" });
    toast("このシステムの外からの貼り付けはできません（AI の回答や自分のコードからのコピーは使えます）");
  }, true);
  let internalDrag = false;
  ta.addEventListener("dragstart", () => (internalDrag = true));
  ta.addEventListener("dragend", () => (internalDrag = false));
  ta.addEventListener("drop", (e) => {
    if (internalDrag) return;                 // 同じ欄の中での移動
    const t = (e.dataTransfer && e.dataTransfer.getData("text/plain")) || "";
    e.preventDefault();
    if (pageDrag) {
      // このページの別の場所（AI 回答など）からのドラッグ：外部ではないので記録しないが、由来を追えないので受け付けない
      pageDrag = false;
      toast("ドラッグでは入れられません。コピーして貼り付けてください");
      return;
    }
    enqueue("PASTE_BLOCKED", { length: t.length, via: via + ":drop" });
    toast("このシステムの外からの貼り付けはできません");
  }, true);
}

function clientInfo() {
  const ua = navigator.userAgent || "";
  // iPadOS は Macintosh を名乗るので、タッチ点の数で見分ける
  const mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  const supported = typeof fetch === "function" && typeof InputEvent !== "undefined" && "inputType" in InputEvent.prototype;
  return {
    user_agent: ua.slice(0, 300), platform: navigator.platform || "", mobile,
    touch: navigator.maxTouchPoints > 0, viewport_w: innerWidth, viewport_h: innerHeight,
    screen_w: screen.width, screen_h: screen.height, language: navigator.language || "",
    timezone: (Intl.DateTimeFormat().resolvedOptions().timeZone || ""), supported_browser: supported,
  };
}
