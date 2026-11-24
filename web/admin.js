"use strict";
const $ = (id) => document.getElementById(id);
let TOKEN = "";
try { TOKEN = sessionStorage.getItem("adminToken") || ""; } catch (_) { /* 使えない環境 */ }

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._h); toast._h = setTimeout(() => (t.hidden = true), 3500);
}

async function api(path, body) {
  const opt = { headers: { "X-Admin-Token": TOKEN } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (!r.ok) { const j = await r.json().catch(() => ({})); const e = new Error(j.error || r.statusText); e.status = r.status; throw e; }
  return r;
}

function learnerUrl(token) { return `${location.origin}/?t=${encodeURIComponent(token)}`; }
function td(text, cls) { const e = document.createElement("td"); e.textContent = text ?? ""; if (cls) e.className = cls; return e; }
function btn(label, fn, cls) { const b = document.createElement("button"); b.textContent = label; b.onclick = fn; if (cls) b.className = cls; return b; }
function mmss(s) { return s == null ? "" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
function ago(s) { if (s == null) return ""; if (s < 60) return `${s}秒前`; if (s < 3600) return `${Math.floor(s / 60)}分前`; return `${Math.floor(s / 3600)}時間前`; }
const STATUS = { not_started: "未開始", active: "参加中", done: "完了", withdrawn: "不同意", stopped: "停止", expired: "期限切れ" };

async function refresh() {
  let data;
  try { data = await (await api("/api/admin/overview")).json(); }
  catch (e) {
    if (e.status === 403) { $("main").hidden = true; $("login").hidden = false; }
    return;
  }
  $("login").hidden = true; $("main").hidden = false;
  $("meta").textContent = `LLM: ${data.llm_provider} ／ 規則: ${data.rule_version} ／ サーバ設定: ${data.server_config_hash}`;
  $("alloc").textContent = data.allocation.total
    ? `割付表: 残り ${data.allocation.remaining} / ${data.allocation.total} 枠（ハッシュ ${data.allocation.hash}）`
    : "割付表がありません。サーバで python -m server.allocation を実行して作成してください（手動割付のみ可能）。";
  $("sandbox").textContent = `実行中 ${data.sandbox.running}・待ち ${data.sandbox.waiting}`;
  const al = $("alerts"); al.textContent = "";
  for (const a of data.alerts.slice().reverse()) {
    const li = document.createElement("li");
    li.textContent = `${new Date(a.t * 1000).toLocaleString()}　${a.message}`;
    al.append(li);
  }
  $("alerts-card").hidden = !data.alerts.length;
  const tb = $("rows"); tb.textContent = "";
  for (const p of data.participants) {
    const tr = document.createElement("tr");
    tr.append(td(p.participant_id), td(p.group === "adaptive" ? "適応" : "一律"), td(STATUS[p.status] || p.status),
      td(p.phase), td(p.task_id), td(mmss(p.remaining_s), "num"), td(ago(p.idle_s), "num"),
      td(String(p.cycles), "num"), td(String(p.interventions), "num"), td(String(p.questions), "num"),
      td(p.cost_usd.toFixed(2), "num"), td(String(p.contaminated_cycles), "num"), td(`${p.anomalies}/${p.errors}`, "num"));
    const u = td("");
    u.append(btn("URLをコピー", async () => {
      try { await navigator.clipboard.writeText(learnerUrl(p.token)); toast("コピーしました"); }
      catch (_) { $("created").textContent = learnerUrl(p.token); }
    }));
    tr.append(u);
    const ops = td("");
    ops.append(btn("判定", () => detail(p.participant_id)));
    if (p.status === "active") {
      ops.append(" ", btn("次のフェーズへ", () => confirmDo(`${p.participant_id} を次のフェーズへ強制的に進めます。`, () => post("/api/admin/force_next", { participant_id: p.participant_id }))));
      ops.append(" ", btn("停止", () => confirmDo(`${p.participant_id} の参加を停止します。元に戻せません。`, () => post("/api/admin/stop", { participant_id: p.participant_id, reason: "admin" }))));
    }
    tr.append(ops);
    if (p.limits.length || p.errors) tr.style.background = "#fff8e1";
    tb.append(tr);
  }
}

// 確認ダイアログの代わり（ページ内で2度押し）
let pending = null;
function confirmDo(msg, fn) {
  if (pending && pending.msg === msg) { pending = null; fn(); return; }
  pending = { msg };
  toast(msg + " もう一度押すと実行します。");
  setTimeout(() => { pending = null; }, 5000);
}

async function post(path, body) {
  try { await api(path, body); toast("実行しました"); } catch (e) { toast("失敗: " + e.message); }
  refresh();
}

async function detail(pid) {
  const d = await (await api(`/api/admin/participant?id=${encodeURIComponent(pid)}`)).json();
  $("detail").hidden = false;
  $("detail-title").textContent = `${pid} の判定（サイクル ${d.cycles.length}・判断 ${d.decisions.length}）`;
  const brief = {
    cycles: d.cycles.map((c) => ({ id: c.cycle_id, task: c.task_id, state: c.state, hs: c.hs, ru: c.ru, ru_reason: c.ru_reason,
      D: c.features.D, S: c.features.S, C: c.features.C, T: c.features.T, E: c.features.E, I: c.features.I, Q: c.features.Q,
      away_s: c.features.away_s, blocked_pastes: c.features.blocked_pastes, contamination: c.features.contamination_suspected,
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
    $("created").textContent = `${r.participant_id}（${r.group === "adaptive" ? "適応群" : "一律群"}・${r.assignment === "block_randomized" ? `割付表 ${r.slot} 番` : "手動"}）: ${learnerUrl(r.token)}`;
    $("c-id").value = ""; $("c-note").value = "";
  } catch (err) { toast("発行できません: " + err.message); }
  refresh();
});
$("backup").addEventListener("click", () => post("/api/admin/backup", {}));
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
