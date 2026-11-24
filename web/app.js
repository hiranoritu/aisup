// 学習者画面。編集・コピー・貼り付けをイベントとしてサーバに送る（仕様 §2）。
// 由来（AI からの貼り付けか）はサーバが直前の COPY_FROM_AI と照合して決める。
"use strict";

const TOKEN = new URLSearchParams(location.search).get("t") || "";
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

let S = null;               // サーバの状態
let version = -1;
let prevText = "";          // 直前に送った編集後のエディタ内容
let pendingSel = null;      // beforeinput 時点の選択範囲
let pendingPaste = null;    // paste イベントのクリップボード文字列
let timerHandle = null;

// ------------------------------------------------------------------ 通信
async function api(path, body) {
  const opt = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.status = r.status; throw e; }
  return j;
}

// 編集イベントは順序を保って送る（送信中は次を待たせる）
const queue = [];
let flushing = null;
function enqueue(type, payload) {
  queue.push({ type, payload, client_t: Date.now() / 1000 });
  if (queue.length >= 40) flush();
}
function flush() {
  if (flushing) return flushing.then(() => (queue.length ? flush() : null));
  if (!queue.length) return Promise.resolve();
  const batch = queue.splice(0, queue.length);
  flushing = api("/api/events", { events: batch })
    .then((r) => { if (r.version !== version) load(); })
    .catch((e) => { queue.unshift(...batch); toast("通信に失敗しました。再送します。"); throw e; })
    .finally(() => { flushing = null; });
  return flushing.catch(() => {});
}
setInterval(flush, 400);

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._h);
  toast._h = setTimeout(() => (t.hidden = true), 3000);
}

// ------------------------------------------------------------------ 状態の反映
async function load() {
  try {
    S = await api("/api/state");
  } catch (e) {
    $("task-title").textContent = e.status === 403 ? "URL が正しくありません" : "接続できません";
    return;
  }
  render();
}

function render() {
  if (typeof Materials !== "undefined") Materials.render(S.task ? S.materials : []);
  if (!S.task) {
    $("workspace").hidden = true;
    $("waiting").hidden = false;
    $("waiting-msg").textContent = S.ended ? "すべての課題が終了しました。お疲れさまでした。" : "開始の合図があるまでお待ちください。";
    $("task-title").textContent = S.ended ? "終了" : "待機中";
    $("progress").textContent = "";
    version = S.version;
    return;
  }
  $("waiting").hidden = true;
  $("workspace").hidden = false;
  $("task-title").textContent = S.task.title;
  $("progress").textContent = `課題 ${S.task_index + 1} / ${S.task_count}`;
  if (S.version !== version) {
    // 課題が切り替わった（または初回）：エディタをサーバの内容に合わせる
    renderDescription(S.task.description);
    codeEl.value = S.code;
    prevText = S.code;
    $("output").textContent = "";
    version = S.version;
    codeHL.refresh();
  } else if (codeEl.value !== S.code && !queue.length && !flushing) {
    codeEl.value = S.code;
    prevText = S.code;
    codeHL.refresh();
  }
  updateGutter();
  renderChat();
  renderSupport();
  setBusy(S.busy, "AI が回答を作成しています…");
}

function renderDescription(md) {
  const box = $("task-desc");
  box.textContent = "";
  const parts = md.split(/```[^\n]*\n([\s\S]*?)```/);
  parts.forEach((p, i) => {
    if (i % 2) { const pre = el("pre", { class: "src" }); highlightInto(pre, p); box.append(pre); }
    else box.append(inlineText(p));
  });
}

function inlineText(s) {
  const frag = document.createDocumentFragment();
  s.split(/(`[^`\n]+`)/).forEach((p) => {
    if (p.startsWith("`") && p.endsWith("`") && p.length > 2) frag.append(el("code", { text: p.slice(1, -1) }));
    else frag.append(p);
  });
  return frag;
}

function locked() { return !!(S && (S.support || S.busy)); }

function setBusy(on, msg) {
  $("busy").hidden = !on;
  if (msg) $("busy-msg").textContent = msg;
  const lock = locked() || on;
  $("send").disabled = lock;
  $("prompt").disabled = lock;
  $("run").disabled = lock;
  $("code").readOnly = lock;
  codeHL.wrap.classList.toggle("locked", lock);
  $("editor-lock").hidden = !(S && S.support);
  $("chat").classList.toggle("readonly", !!(S && S.support && S.settings.block_copy_during_support));
}

// ------------------------------------------------------------------ エディタ
const KIND = (t) => {
  if (!t) return "other";
  if (t === "insertFromPaste" || t === "insertFromPasteAsQuotation") return "paste";
  if (t === "historyUndo") return "undo";
  if (t === "historyRedo") return "redo";
  if (t.startsWith("insert") && t !== "insertFromDrop") return "typed";
  if (t.startsWith("delete") && t !== "deleteByCut" && t !== "deleteByDrag") return "typed";
  return "other";
};

function diffEdit(a, b) {
  let p = 0;
  const n = Math.min(a.length, b.length);
  while (p < n && a[p] === b[p]) p++;
  let s = 0;
  while (s < n - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
  return { offset: p, delete_len: a.length - p - s, insert_text: b.slice(p, b.length - s) };
}

function applyEdit(text, e) {
  return text.slice(0, e.offset) + e.insert_text + text.slice(e.offset + e.delete_len);
}

const codeEl = $("code");
const codeHL = attachHighlight(codeEl);
codeEl.addEventListener("beforeinput", () => {
  pendingSel = { start: codeEl.selectionStart, end: codeEl.selectionEnd };
});
codeEl.addEventListener("paste", (e) => {
  const t = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
  pendingPaste = { text: t.replace(/\r\n?/g, "\n"), start: codeEl.selectionStart, end: codeEl.selectionEnd };
});
codeEl.addEventListener("input", (e) => {
  const cur = codeEl.value;
  const kind = KIND(e.inputType);
  let edit = null;
  // 選択範囲とデータから正確な編集を作り、内容が一致しなければ差分で代用する
  if (kind === "paste" && pendingPaste) {
    const c = { offset: pendingPaste.start, delete_len: pendingPaste.end - pendingPaste.start, insert_text: pendingPaste.text };
    if (applyEdit(prevText, c) === cur) edit = c;
  } else if (pendingSel && e.inputType === "insertText" && e.data != null) {
    const c = { offset: pendingSel.start, delete_len: pendingSel.end - pendingSel.start, insert_text: e.data };
    if (applyEdit(prevText, c) === cur) edit = c;
  } else if (pendingSel && e.inputType === "insertLineBreak") {
    const c = { offset: pendingSel.start, delete_len: pendingSel.end - pendingSel.start, insert_text: "\n" };
    if (applyEdit(prevText, c) === cur) edit = c;
  }
  if (!edit) edit = diffEdit(prevText, cur);
  pendingSel = null;
  pendingPaste = null;
  if (edit.delete_len === 0 && edit.insert_text === "") return;
  enqueue("CODE_EDIT", { ...edit, kind, input_type: e.inputType || "" });
  prevText = cur;
  updateGutter();
});
codeEl.addEventListener("keydown", (e) => {
  if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !codeEl.readOnly) {
    e.preventDefault();
    document.execCommand("insertText", false, "    "); // input イベント（insertText）として記録される
  }
});
codeEl.addEventListener("scroll", () => { $("gutter").scrollTop = codeEl.scrollTop; });

function updateGutter() {
  const n = codeEl.value.split("\n").length;
  const g = $("gutter");
  if (g.childElementCount !== n || g.textContent === "") {
    g.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
  }
  g.scrollTop = codeEl.scrollTop;
}

// 定期スナップショット（CODE_EDIT 欠損に備えた冗長記録）
setInterval(() => {
  if (S && S.task) enqueue("CODE_SNAPSHOT", { text: codeEl.value, reason: "interval" });
}, 10000);
window.addEventListener("beforeunload", () => {
  if (S && S.task) {
    enqueue("CODE_SNAPSHOT", { text: codeEl.value, reason: "unload" });
    const batch = queue.splice(0, queue.length);
    fetch("/api/events", { method: "POST", keepalive: true,
      headers: { "X-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify({ events: batch }) });
  }
});

// ------------------------------------------------------------------ 実行
$("run").addEventListener("click", async () => {
  if (locked()) return;
  $("run").disabled = true;
  const out = $("output");
  out.textContent = "";
  out.append(el("span", { class: "meta", text: "コンパイル・実行中…\n" }));
  try {
    await flush();
    const r = await api("/api/run", { code: codeEl.value, stdin: $("stdin").value });
    showRun(out, r);
  } catch (e) {
    out.textContent = "実行できませんでした: " + e.message;
  } finally {
    $("run").disabled = locked();
  }
});

function showRun(out, r) {
  out.textContent = "";
  if (!r.ok) { out.append(el("span", { class: "err", text: r.error })); return; }
  const diag = (r.compile.diagnostics || "").trim();
  if (!r.compile.ok) {
    out.append(el("span", { class: "meta", text: "コンパイルエラー\n" }), el("span", { class: "err", text: diag }));
    return;
  }
  if (diag) out.append(el("span", { class: "warn", text: diag + "\n" }), el("span", { class: "meta", text: "――――\n" }));
  const run = r.run;
  out.append(run.stdout || "");
  if (run.stderr) out.append(el("span", { class: "err", text: run.stderr }));
  const notes = [];
  if (run.limit === "cpu" || run.limit === "wall") notes.push("時間制限を超えたため停止しました");
  else if (run.limit === "killed") notes.push("時間またはメモリの制限を超えたため停止しました");
  else if (run.limit === "output") notes.push("出力が多すぎるため停止しました");
  if (run.sanitizer) notes.push("実行時エラー（メモリやポインタの誤り）が検出されました");
  if (run.truncated && run.limit !== "output") notes.push("出力の一部を省略しました");
  out.append(el("span", { class: "meta", text: `\n[終了コード ${run.exit_code ?? "-"}]` + (notes.length ? " " + notes.join("／") : "") }));
}

// ------------------------------------------------------------------ チャット
function renderChat() {
  const box = $("chat");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.textContent = "";
  if (!S.chat.length) box.append(el("div", { class: "empty", text: "AI に質問できます。回答のコードは「コピー」ボタンや選択してコピーできます。" }));
  for (const m of S.chat) {
    const who = m.role === "user" ? "あなた" : m.role === "assistant" ? "AI" : "エラー";
    const bubble = el("div", { class: "bubble" });
    if (m.role === "assistant") {
      for (const seg of m.segments) {
        if (seg.type === "text") bubble.append(inlineText(seg.text));
        else bubble.append(codeBlock(m.response_id, seg));
      }
    } else bubble.textContent = m.content;
    box.append(el("div", { class: "msg " + m.role, "data-response-id": m.response_id || null },
      el("div", { class: "who", text: who }), bubble));
  }
  if (atBottom || box.dataset.n !== String(S.chat.length)) box.scrollTop = box.scrollHeight;
  box.dataset.n = String(S.chat.length);
}

function codeBlock(responseId, seg) {
  const pre = el("pre", { "data-block-id": seg.block_id, "data-response-id": responseId });
  if (["c", "h", ""].includes(seg.lang)) highlightInto(pre, seg.code); else pre.textContent = seg.code;
  const btn = el("button", { class: "copy", type: "button", text: "コピー", onclick: () => copyBlock(responseId, seg) });
  return el("div", { class: "codeblock" }, pre, btn);
}

function copyBlocked() { return !!(S && S.support && S.settings.block_copy_during_support); }

async function copyBlock(responseId, seg) {
  if (copyBlocked()) {
    enqueue("COPY_FROM_AI", { response_id: responseId, block_id: seg.block_id, text: seg.code, method: "button" });
    toast("支援が終わるまでコピーできません");
    return;
  }
  const ok = await writeClipboard(seg.code);
  if (!ok) { toast("コピーできませんでした。コードを選択してコピーしてください。"); return; }
  enqueue("COPY_FROM_AI", { response_id: responseId, block_id: seg.block_id, text: seg.code, method: "button" });
  toast("コピーしました");
}

async function writeClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch (_) { /* 下の方法を試す */ }
  const ta = el("textarea", { style: "position:fixed;left:-9999px;top:0" });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  ta.remove();
  return ok;
}

// 選択してのコピー（Ctrl+C / 右クリック）
$("chat").addEventListener("copy", (e) => {
  const sel = document.getSelection();
  const text = sel ? sel.toString() : "";
  if (!text) return;
  const node = (n) => (n && n.nodeType === 3 ? n.parentElement : n);
  const a = node(sel.anchorNode), f = node(sel.focusNode);
  const preA = a && a.closest("pre[data-block-id]"), preF = f && f.closest("pre[data-block-id]");
  const msg = a && a.closest("[data-response-id]");
  const payload = { response_id: msg ? msg.getAttribute("data-response-id") : null,
    block_id: preA && preA === preF ? preA.getAttribute("data-block-id") : null, text, method: "selection" };
  if (copyBlocked()) {
    e.preventDefault();
    enqueue("COPY_FROM_AI", payload);
    toast("支援が終わるまでコピーできません");
    return;
  }
  if (payload.response_id) enqueue("COPY_FROM_AI", payload);
});

$("ask").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("prompt").value.trim();
  if (!text || locked()) return;
  setBusy(true, "送信しています…");
  try {
    await flush();
    const r = await api("/api/prompt", { text });
    $("prompt").value = "";
    S = r.state;
    render();
    if (r.stage === "ai_error") toast("AI の応答を取得できませんでした。もう一度送ってください。");
  } catch (err) {
    toast(err.status === 409 ? "前の質問を処理中です" : "送信できませんでした: " + err.message);
    await load();
  }
});
$("prompt").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("ask").requestSubmit(); }
});

// ------------------------------------------------------------------ 支援オーバーレイ
const TITLES = {
  clarify_question: "送る前に：質問を具体的にしましょう",
  decompose: "考えてみよう：課題の手順",
  self_explain: "考えてみよう：コードの役割を説明する",
  predict_verify: "考えてみよう：実行結果を予測する",
  trace_predict: "考えてみよう：実行結果を予測する",
  apply_variation: "やってみよう：少し変えた問題",
};

function renderSupport() {
  const ov = $("overlay");
  const sp = S.support;
  clearInterval(timerHandle);
  if (!sp) { ov.hidden = true; ov.dataset.key = ""; return; }
  ov.hidden = false;
  startTimer(sp);
  // 同じ状態なら作り直さない（入力途中の文字を消さない）
  const key = `${sp.opportunity_id}:${sp.attempts}:${sp.hint_used}:${sp.done}`;
  if (ov.dataset.key === key) return;
  ov.dataset.key = key;
  $("ov-title").textContent = TITLES[sp.support_type] || "考えてみよう";
  const body = $("ov-body"), act = $("ov-actions");
  body.textContent = "";
  act.textContent = "";
  const p = sp.public;
  if (p.kind === "clarify") return renderClarify(sp, body, act);
  if (sp.timing === "post_response") body.append(el("div", { class: "note", text: "右の AI の回答は読めます。この問題が終わるとコードを編集・実行できます。" }));
  body.append(el("div", { class: "q", text: p.question }));
  if (p.code) body.append(srcView(p.code, p.highlight_lines || []));
  if (p.stdin) body.append(el("div", { class: "note", text: "標準入力:" }), el("pre", { class: "src", text: p.stdin }));
  const last = sp.results.length ? sp.results[sp.results.length - 1] : null;
  if (p.kind === "choice") renderChoice(sp, body, act, last);
  else if (p.kind === "text") renderText(sp, body, act, last);
  else if (p.kind === "code") renderCode(sp, body, act, last);
  if (sp.hint) body.append(el("div", { class: "hint", text: "ヒント: " + sp.hint }));
  if (!sp.done) {
    if (sp.attempts >= 1 && !sp.hint_used) act.prepend(el("button", { type: "button", text: "ヒントを見る", onclick: () => hint(sp) }));
    if (S.settings.allow_skip) act.prepend(el("button", { type: "button", text: "スキップ", onclick: () => endSupport(sp, "skipped") }));
  } else if (!(last && last.self_assess)) {
    act.append(el("button", { class: "primary", type: "button", text: "閉じてコードに戻る", onclick: () => endSupport(sp, "completed") }));
  }
}

function startTimer(sp) {
  const offset = Date.now() / 1000 - sp.now_t;   // サーバ時刻との差
  const tick = () => {
    const left = Math.max(0, Math.round(sp.deadline_t - (Date.now() / 1000 - offset)));
    $("ov-timer").textContent = `残り ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    if (left <= 0) {
      clearInterval(timerHandle);
      if (sp.support_type === "clarify_question") clarifySend(sp, "timeout");
      else endSupport(sp, "timeout");
    }
  };
  tick();
  timerHandle = setInterval(tick, 1000);
}

function srcView(code, hl) {
  const pre = el("pre", { class: "src" });
  highlightLines(code).forEach((spans, i) => {
    pre.append(el("span", { class: "ln" + (hl.includes(i + 1) ? " hl" : "") }, el("span", { class: "no", text: String(i + 1) }), spans));
  });
  return pre;
}

function verdict(r) {
  if (r.correct === true) return el("div", { class: "verdict ok", text: "正解です" });
  if (r.correct === false) return el("div", { class: "verdict ng", text: "正解ではありません" });
  return el("div", { class: "verdict", text: "判定できませんでした" });
}

function renderChoice(sp, body, act, last) {
  const p = sp.public;
  const name = "c-" + sp.opportunity_id;
  const box = el("div", { class: "choices", role: "radiogroup" });
  const fin = sp.done && last;
  p.choices.forEach((c, i) => {
    let cls = "choice";
    if (fin && last.answer_index === i) cls += " correct";
    const input = el("input", { type: "radio", name, value: String(i), disabled: sp.done });
    box.append(el("label", { class: cls }, input, el("pre", { text: c })));
  });
  body.append(box);
  if (last) {
    body.append(verdict(last));
    if (sp.done && last.actual_output != null) body.append(el("div", { class: "note", text: "実際の実行結果:" }), el("pre", { class: "src", text: last.actual_output }));
    if (sp.done && last.explanation) body.append(el("div", { text: last.explanation }));
    if (!sp.done) body.append(el("div", { class: "note", text: "もう一度選べます（再挑戦は1回）。" }));
  }
  if (!sp.done) act.append(el("button", { class: "primary", type: "button", text: "回答する", onclick: () => {
    const v = box.querySelector("input:checked");
    if (!v) return toast("選択肢を選んでください");
    answer(sp, Number(v.value));
  } }));
}

function renderText(sp, body, act, last) {
  const ta = el("textarea", { rows: 4, placeholder: "自分の言葉で説明してください", disabled: sp.done });
  body.append(ta);
  if (last) {
    if (last.self_assess) {
      body.append(el("div", { class: "note", text: "次の観点を満たしていたか、自分で確認してください。" }));
      const checks = last.rubric.map((r) => el("input", { type: "checkbox" }));
      last.rubric.forEach((r, i) => body.append(el("label", { class: "choice" }, checks[i], r)));
      body.append(el("div", { class: "note", text: "解答例:" }), el("div", { class: "original", text: last.model_answer }));
      if (sp.done) {
        act.append(el("button", { class: "primary", type: "button", text: "確認して閉じる", onclick: async () => {
          try { await api("/api/support/self_assess", { opportunity_id: sp.opportunity_id, ratings: checks.map((c) => (c.checked ? 1 : 0)) }); }
          catch (e) { /* 自己評価の記録に失敗しても支援は閉じる */ }
          endSupport(sp, "completed");
        } }));
      }
    } else {
      body.append(verdict(last));
      if (last.met && last.met.length) body.append(el("div", { text: "満たしている観点: " + last.met.join("／") }));
      if (last.unmet && last.unmet.length) body.append(el("div", { text: "足りない観点: " + last.unmet.join("／") }));
      if (last.feedback) body.append(el("div", { class: "hint", text: last.feedback }));
      if (!sp.done) body.append(el("div", { class: "note", text: "書き直して、もう一度送れます（再挑戦は1回）。" }));
    }
  }
  if (!sp.done) act.append(el("button", { class: "primary", type: "button", text: "回答する", onclick: () => {
    if (!ta.value.trim()) return toast("説明を書いてください");
    answer(sp, ta.value);
  } }));
}

function renderCode(sp, body, act, last) {
  const p = sp.public;
  const ed = el("textarea", { class: "mini-editor", spellcheck: "false", disabled: sp.done });
  ed.value = sp._draft || p.starter_code;
  ed.addEventListener("input", () => (sp._draft = ed.value));
  const stdin = el("textarea", { rows: 2, class: "mini-editor", style: "min-height:auto" });
  stdin.value = p.tests[0] || "";
  const out = el("pre", { class: "output", style: "height:auto;max-height:20vh" });
  body.append(ed, el("div", { class: "note", text: "標準入力（試し実行用）" }), stdin);
  const edHL = attachHighlight(ed);
  edHL.wrap.classList.add("mini");
  body.append(out);
  if (last) {
    body.append(verdict(last));
    if (last.compile && !last.compile.ok) body.append(el("pre", { class: "src", text: last.compile.diagnostics }));
    if (last.results && last.results.length) {
      const tbl = el("table", { class: "results" }, el("tr", {}, el("th", { text: "入力" }), el("th", { text: "期待される出力" }), el("th", { text: "あなたの出力" }), el("th", { text: "" })));
      last.results.forEach((r) => tbl.append(el("tr", {}, el("td", {}, el("pre", { text: r.stdin })), el("td", {}, el("pre", { text: r.expected })), el("td", {}, el("pre", { text: r.actual })), el("td", { text: r.pass ? "○" : "×" }))));
      body.append(tbl);
    }
  }
  if (!sp.done) {
    act.append(el("button", { type: "button", text: "▶ 試しに実行", onclick: async () => {
      out.textContent = "実行中…";
      try { showRun(out, await api("/api/support/run", { opportunity_id: sp.opportunity_id, code: ed.value, stdin: stdin.value })); }
      catch (e) { out.textContent = "実行できませんでした: " + e.message; }
    } }));
    act.append(el("button", { class: "primary", type: "button", text: "提出する", onclick: () => answer(sp, ed.value) }));
  }
}

function renderClarify(sp, body, act) {
  const p = sp.public;
  body.append(el("div", { class: "note", text: "あなたが書いた質問（このまま送ることもできます）:" }), el("div", { class: "original", text: p.original_text }));
  body.append(el("div", { class: "q", text: "何について困っていますか？" }));
  const name = "cl-" + sp.opportunity_id;
  const box = el("div", { class: "choices", role: "radiogroup" });
  p.choices.forEach((c, i) => box.append(el("label", { class: "choice" }, el("input", { type: "radio", name, value: String(i) }), c)));
  body.append(box);
  body.append(el("label", { class: "q", for: "cl-sentence", text: "今いちばん知りたいことを一文で書いてください" }));
  const ta = el("textarea", { id: "cl-sentence", rows: 2 });
  body.append(ta);
  const choice = () => { const v = box.querySelector("input:checked"); return v ? Number(v.value) : null; };
  if (S.settings.allow_skip) act.append(el("button", { type: "button", text: "スキップ（原文のまま送る）", onclick: () => clarifySend(sp, "skipped") }));
  act.append(el("button", { type: "button", text: "原文のまま送る", onclick: () => clarifySend(sp, "original", choice(), ta.value) }));
  act.append(el("button", { class: "primary", type: "button", text: "原文＋この一文で送る", onclick: () => {
    if (!ta.value.trim()) return toast("一文を書いてください");
    clarifySend(sp, "appended", choice(), ta.value);
  } }));
}

async function clarifySend(sp, mode, choice = null, sentence = "") {
  clearInterval(timerHandle);
  $("overlay").hidden = true;
  setBusy(true, "AI が回答を作成しています…");
  try {
    const r = await api("/api/clarify", { opportunity_id: sp.opportunity_id, mode, choice, sentence });
    S = r.state;
  } catch (e) { toast("送信できませんでした: " + e.message); }
  await load();
}

async function answer(sp, value) {
  const btns = $("ov-actions").querySelectorAll("button");
  btns.forEach((b) => (b.disabled = true));
  try {
    const r = await api("/api/support/answer", { opportunity_id: sp.opportunity_id, answer: value });
    S = r.state;
    render();
  } catch (e) {
    toast(e.status === 409 ? "採点中です" : "送信できませんでした: " + e.message);
    btns.forEach((b) => (b.disabled = false));
    if (e.status === 400) load();
  }
}

async function hint(sp) {
  try { S = (await api("/api/support/hint", { opportunity_id: sp.opportunity_id })).state; render(); }
  catch (e) { toast(e.message); }
}

async function endSupport(sp, reason) {
  clearInterval(timerHandle);
  try { S = (await api("/api/support/end", { opportunity_id: sp.opportunity_id, reason })).state; }
  catch (e) { /* 時間切れ等はサーバ側で終了済み */ }
  await load();
}

// ------------------------------------------------------------------ 起動
if (!TOKEN) {
  $("task-title").textContent = "URL に参加者トークンがありません";
} else {
  load();
  // 待機中・課題切替の検出
  setInterval(() => { if (!S || !S.task) load(); }, 5000);
}
