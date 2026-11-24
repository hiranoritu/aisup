// 実験の各フェーズの画面（仕様 v1.2 §15.1）：同意・環境確認・テスト・質問紙・終了。
"use strict";

const END_MESSAGES = {
  withdrawn: "参加への同意をいただけなかったため、ここで終了します。このページを閉じてください。",
  stopped: "実験者により参加が停止されました。お手数ですが、実験者にご連絡ください。",
  expired: "最初に開いてから時間が経ちすぎたため、このURLは無効になりました。実験者にご連絡ください。",
  not_started: "読み込み中です…",
};

function showView(id) {
  for (const v of ["view-end", "view-consent", "view-envcheck", "view-form", "workspace"]) $(v).hidden = v !== id;
}

function renderEnd(S) {
  showView("view-end");
  $("end-msg").textContent = S.status === "done" ? S.completion_message : (END_MESSAGES[S.status] || "");
}

// ------------------------------------------------------------------ 同意
function renderConsent(S) {
  showView("view-consent");
  const box = $("consent-body");
  box.textContent = "";
  box.append(renderMarkdown(S.consent.text));
  const chk = $("consent-check");
  chk.checked = false;
  $("consent-agree").disabled = true;
  chk.onchange = () => ($("consent-agree").disabled = !chk.checked);
  $("consent-agree").onclick = () => submitConsent(true);
  $("consent-decline").onclick = () => {
    const c = $("consent-decline-confirm");
    c.hidden = false;
    $("consent-decline-yes").onclick = () => submitConsent(false);
    $("consent-decline-no").onclick = () => (c.hidden = true);
  };
}

async function submitConsent(agree) {
  try { S = (await api("/api/consent", { agree })).state; render(); }
  catch (e) { toast("送信できませんでした: " + e.message); }
}

// ------------------------------------------------------------------ 環境確認
function renderEnvcheck(S) {
  showView("view-envcheck");
  $("env-result").textContent = "";
  $("env-run").disabled = false;
  $("env-run").onclick = runEnvcheck;
}

async function runEnvcheck() {
  $("env-run").disabled = true;
  const res = $("env-result");
  res.textContent = "確認しています…（10秒ほどかかります）";
  const info = clientInfo();
  try {
    const t0 = performance.now();
    const r = await fetch("/api/envcheck/blob", { headers: { "X-Token": TOKEN }, cache: "no-store" });
    await r.arrayBuffer();
    info.net_ms = Math.round(performance.now() - t0);
  } catch (_) { info.net_ms = 99999; }
  try {
    const r = await api("/api/envcheck", { info });
    if (r.ok) {
      if (r.warnings.length) toast(r.warnings.map(envWarning).join(" ／ "));
      S = r.state;
      render();
      return;
    }
    res.textContent = "";
    for (const b of r.blocking) res.append(el("p", { class: "verdict ng", text: envBlocking(b) }));
    res.append(el("p", { class: "note", text: "条件を満たしたら、もう一度「確認する」を押してください。" }));
  } catch (e) {
    res.textContent = "確認できませんでした: " + e.message;
  }
  $("env-run").disabled = false;
}

function envBlocking(code) {
  return {
    mobile: "スマートフォン・タブレットでは参加できません。パソコンのブラウザで同じURLを開いてください。",
    sandbox: "プログラムの実行環境に接続できませんでした。時間をおいて試し、続く場合は実験者に連絡してください。",
  }[code] || code;
}
function envWarning(code) {
  return {
    narrow_screen: "画面が狭いため表示が崩れることがあります。ウィンドウを広げてください",
    slow_network: "通信が遅いようです。安定した回線で参加してください",
    browser: "このブラウザでは一部の機能が動かない可能性があります（Chrome / Edge / Firefox / Safari の最新版を推奨）",
  }[code] || code;
}

// ------------------------------------------------------------------ テスト・質問紙
let formVersion = -1;
let formTimer = null;
const formDraft = {};

function renderForm(S) {
  showView("view-form");
  if (formVersion === S.version) return;     // 入力途中の内容を消さない
  formVersion = S.version;
  const inst = S.instrument;
  $("form-title").textContent = inst.title;
  $("form-intro").textContent = inst.intro;
  const box = $("form-items");
  box.textContent = "";
  Object.keys(formDraft).forEach((k) => delete formDraft[k]);
  Object.assign(formDraft, inst.answers || {});
  inst.items.forEach((it, idx) => box.append(formItem(it, idx)));
  $("form-confirm").hidden = true;
  $("form-submit").hidden = false;
  $("form-submit").onclick = () => { $("form-confirm").hidden = false; $("form-submit").hidden = true; };
  $("form-cancel").onclick = () => { $("form-confirm").hidden = true; $("form-submit").hidden = false; };
  $("form-final").onclick = () => saveForm(true);
}

function formItem(it, idx) {
  const wrap = el("fieldset", { class: "item" });
  wrap.append(el("legend", { text: `${idx + 1}. ${it.prompt}${it.required ? "（必須）" : ""}` }));
  if (it.code) { const pre = el("pre", { class: "src" }); highlightInto(pre, it.code); wrap.append(pre); }
  const cur = formDraft[it.id];
  const name = "f-" + it.id;
  const set = (v) => { formDraft[it.id] = v; scheduleSave(); };
  if (it.type === "choice") {
    it.choices.forEach((c, i) => {
      const r = el("input", { type: "radio", name, value: String(i) });
      r.checked = cur === i;
      r.onchange = () => set(i);
      wrap.append(el("label", { class: "choice" }, r, c));
    });
  } else if (it.type === "multi") {
    it.choices.forEach((c, i) => {
      const r = el("input", { type: "checkbox", value: String(i) });
      r.checked = Array.isArray(cur) && cur.includes(i);
      r.onchange = () => set([...wrap.querySelectorAll("input:checked")].map((x) => Number(x.value)));
      wrap.append(el("label", { class: "choice" }, r, c));
    });
  } else if (it.type === "scale") {
    const row = el("div", { class: "scale" });
    const lo = it.min ?? 1, hi = it.max ?? 7;
    if (it.labels) row.append(el("span", { class: "note", text: it.labels[0] }));
    for (let v = lo; v <= hi; v++) {
      const r = el("input", { type: "radio", name, value: String(v), "aria-label": String(v) });
      r.checked = cur === v;
      r.onchange = () => set(v);
      row.append(el("label", { class: "scale-pt" }, r, el("span", { text: String(v) })));
    }
    if (it.labels) row.append(el("span", { class: "note", text: it.labels[1] }));
    wrap.append(row);
  } else {
    const ta = el("textarea", { rows: it.type === "code" ? 8 : 3, class: it.type === "code" ? "mini-editor" : "",
      spellcheck: it.type === "code" ? "false" : "true" });
    ta.value = typeof cur === "string" ? cur : (it.starter || "");
    ta.oninput = () => set(ta.value);
    wrap.append(ta);
    if (it.type === "code") {
      guardPaste(ta, "form");
      const hl = attachHighlight(ta);
      hl.wrap.classList.add("mini");
    }
  }
  return wrap;
}

function scheduleSave() {
  clearTimeout(formTimer);
  formTimer = setTimeout(() => saveForm(false), 800);
}

async function saveForm(final) {
  clearTimeout(formTimer);
  try {
    const r = await api("/api/answers", { answers: { ...formDraft }, final });
    if (final) { S = r.state; render(); }
  } catch (e) {
    if (final) {
      toast(e.message.startsWith("required") ? "必須の質問に答えてください" : "送信できませんでした: " + e.message);
      $("form-confirm").hidden = true;
      $("form-submit").hidden = false;
    } else netWarn();
  }
}

// ------------------------------------------------------------------ ヘッダの残り時間
let headerTimer = null;
function renderHeader(S) {
  const ph = S.phase;
  $("task-title").textContent = ph ? ph.title : "";
  $("progress").textContent = ph ? `${ph.index + 1} / ${ph.count}` : "";
  clearInterval(headerTimer);
  const t = $("phase-timer");
  if (!ph || !ph.deadline_t || S.status !== "active") { t.textContent = ""; return; }
  const offset = Date.now() / 1000 - S.now_t;
  const tick = () => {
    const left = Math.max(0, Math.round(ph.deadline_t - (Date.now() / 1000 - offset)));
    t.textContent = `残り ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    t.classList.toggle("low", left <= 120);
    if (left <= 0) { clearInterval(headerTimer); setTimeout(load, 2500); }   // サーバが次のフェーズへ進める
  };
  tick();
  headerTimer = setInterval(tick, 1000);
}
