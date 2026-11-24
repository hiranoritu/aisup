"use strict";
const $ = (id) => document.getElementById(id);
let TOKEN = "";
try { TOKEN = sessionStorage.getItem("adminToken") || ""; } catch (_) { /* 使えない環境 */ }

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._h); toast._h = setTimeout(() => (t.hidden = true), 3000);
}

async function api(path, body) {
  const opt = { headers: { "X-Admin-Token": TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (!r.ok) { const j = await r.json().catch(() => ({})); const e = new Error(j.error || r.statusText); e.status = r.status; throw e; }
  return r;
}

function learnerUrl(token) { return `${location.origin}/?t=${encodeURIComponent(token)}`; }

function td(text, cls) { const e = document.createElement("td"); e.textContent = text; if (cls) e.className = cls; return e; }

async function refresh() {
  let data;
  try { data = await (await api("/api/admin/overview")).json(); }
  catch (e) {
    if (e.status === 403) { $("main").hidden = true; $("login").hidden = false; }
    return;
  }
  $("login").hidden = true; $("main").hidden = false;
  $("meta").textContent = `LLM: ${data.llm_provider} ／ 規則: ${data.rule_version} ／ サーバ設定: ${data.server_config_hash}`;
  $("tasks").textContent = data.tasks.join(" → ");
  const tb = $("rows"); tb.textContent = "";
  for (const p of data.participants) {
    const tr = document.createElement("tr");
    tr.append(td(p.participant_id), td(p.group === "adaptive" ? "適応" : "一律"),
      td(p.ended ? "終了" : p.task_id || "未開始"), td(String(p.cycles), "num"), td(String(p.interventions), "num"),
      td(p.states.join(" "), "states"));
    const sup = td(""); if (p.support_active) { const s = document.createElement("span"); s.className = "pill on"; s.textContent = p.support_active; sup.append(s); }
    if (p.busy) { const s = document.createElement("span"); s.className = "pill"; s.textContent = "AI応答中"; sup.append(" ", s); }
    tr.append(sup, td(String(p.anomalies), "num"));
    const u = td(""); const copy = document.createElement("button"); copy.textContent = "URLをコピー";
    copy.onclick = async () => { try { await navigator.clipboard.writeText(learnerUrl(p.token)); toast("コピーしました"); } catch (_) { prompt("学習者URL", learnerUrl(p.token)); } };
    u.append(copy); tr.append(u);
    const ops = td("");
    const adv = document.createElement("button"); adv.textContent = p.ended ? "終了済み" : p.task_id ? "次の課題へ" : "開始"; adv.disabled = p.ended;
    adv.onclick = async () => { await advance({ participant_id: p.participant_id }); };
    const det = document.createElement("button"); det.textContent = "判定"; det.onclick = () => detail(p.participant_id);
    ops.append(adv, " ", det); tr.append(ops);
    tb.append(tr);
  }
}

async function advance(body) {
  try { const r = await (await api("/api/admin/advance", body)).json(); toast(Object.entries(r.result).map(([k, v]) => `${k}: ${v}`).join(", ")); }
  catch (e) { toast("失敗: " + e.message); }
  refresh();
}

async function detail(pid) {
  const d = await (await api(`/api/admin/participant?id=${encodeURIComponent(pid)}`)).json();
  $("detail").hidden = false;
  $("detail-title").textContent = `${pid} の判定（サイクル ${d.cycles.length}・判断 ${d.decisions.length}）`;
  const brief = {
    cycles: d.cycles.map((c) => ({ id: c.cycle_id, task: c.task_id, state: c.state, hs: c.hs, ru: c.ru, ru_reason: c.ru_reason,
      D: c.features.D, S: c.features.S, C: c.features.C, T: c.features.T, E: c.features.E, I: c.features.I, Q: c.features.Q,
      intervened: c.intervened, support: c.support_types, unknown: c.unknown_reasons, review: c.review })),
    decisions: d.decisions.map((x) => ({ id: x.opportunity_id, cycle: x.cycle_id, window: x.window_states, support: x.support_type, timing: x.timing, reason: x.reason })),
    open_cycle: d.open_cycle, anomalies: d.anomalies,
  };
  $("detail-body").textContent = JSON.stringify(brief, null, 1);
}

$("login-form").addEventListener("submit", (e) => {
  e.preventDefault(); TOKEN = $("token").value.trim();
  try { sessionStorage.setItem("adminToken", TOKEN); } catch (_) { /* 保存できなくても続行 */ }
  refresh();
});
$("create").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await (await api("/api/admin/participants", { participant_id: $("c-id").value.trim(), group: $("c-group").value, note: $("c-note").value })).json();
    $("created").textContent = `${r.participant_id}（${r.group === "adaptive" ? "適応群" : "一律群"}・${r.assignment === "random" ? "無作為" : "指定"}）: ${learnerUrl(r.token)}`;
    $("c-id").value = ""; $("c-note").value = "";
  } catch (err) { toast("登録できません: " + err.message); }
  refresh();
});
$("advance-all").addEventListener("click", () => advance({ all: true }));
$("export").addEventListener("click", async () => {
  try {
    const blob = await (await api("/api/admin/export")).blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `aisup_export_${Date.now()}.zip`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) { toast("失敗: " + e.message); }
});

refresh();
setInterval(refresh, 5000);
