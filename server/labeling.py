"""RQ1 の人手ラベル付け用の資料（仕様 v1.2 §9：支援提示前の情報だけに基づき、独立評価者2名以上でラベル付け）。

    # 資料を作る（管理画面の「データの書き出し」で得た zip から）
    python -m server.labeling make --zip export.zip --out labeling/ --seed 20261124

    # 手順（2026-11-24 決定：正解は合議で決める）
    # 1) 2名が独立に label_sheet.csv に記入
    # 2) 評価者間の一致（κ）を出し、合議用の一覧を作る
    python -m server.labeling disagree --dir labeling/ --labels rater_a.csv rater_b.csv
    #    → labeling/consensus/disagreements.html（不一致の項目だけ）と gold_draft.csv（一致した値は記入済み）
    # 3) 合議で gold_draft.csv の空欄を埋め、正解として渡す
    python -m server.labeling score --dir labeling/ --labels rater_a.csv rater_b.csv --gold gold.csv

出力（make）：
- 評価者に渡すもの（`for_raters/`）：items/<item_id>.html（1サイクル1件）、index.html、label_sheet.csv（記入用）、
  guide.md（記入の手引きの下書き。研究者が確定させる）。参加者ID・群・システムの判定・支援の内容は含めない。
- 研究者だけが持つもの（`_key/`）：key.csv（item_id とサイクル・参加者・群・システムの判定の対応）、manifest.json。

資料の範囲（README「実装時に置いた解釈」23）：
- 単位は判定エンジンのサイクル（質問から次の新規質問まで）。既定は主課題フェーズ（--phases で変更）。
- 表示するのは分類に使えるイベントだけ：質問の原文（PROMPT_DRAFT_SUBMIT。P-A で変更された文面は出さない）、
  AI 回答、コードの編集（打鍵はまとめ、貼り付けは由来つき）、実行（入力・出力）、コンパイル結果、提出の合否。
  タブ離脱・教材・外部AI利用の疑い・LLM の特徴抽出・判定・支援は出さない。
- 支援が提示されたサイクルは、その提示の時点で打ち切る（以降の行動は支援の影響を受けるため）。key.csv に印を付ける。
- 支援中（in_support）のイベントは出さない。
"""
from __future__ import annotations

import argparse
import csv
import difflib
import html
import json
import os
import random
import statistics
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from engine import make_config
from engine.config import config_hash, load_config
from engine.core import Engine
from engine.events import Event, ordered

from .settings import abspath
from .tasks import load_task

HS_LABELS = ("Passive", "Active", "Constructive")
RU_LABELS = ("Passive", "Active", "Constructive")
NA = "Undeterminable"          # 評価者が「判定不能」としたもの
ALIASES = {"判定不能": NA, "判断できない": NA, "不明": NA}
SHOWN_TYPES = {"PROMPT_DRAFT_SUBMIT", "AI_RESPONSE", "CODE_EDIT", "COMPILE", "RUN", "TASK_SUBMIT",
               "CODE_SNAPSHOT"}   # スナップショットは表示せず、実行時のコードを正確にするためだけに使う


@dataclass
class Item:
    participant_id: str
    group: str
    cycle: Any                      # engine.models.CycleResult
    start_code: str
    end_code: str
    timeline: list[Event]
    cutoff_t: Optional[float]       # 支援の提示で打ち切った時刻
    decision_latency_s: Optional[float]
    responses_before: dict[str, int] = field(default_factory=dict)   # 以前のサイクルの AI 回答（貼り付け元の表示用）


# ==================================================================== 読み込み
def read_zip(path: str) -> tuple[dict, dict[str, list[Event]]]:
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        logs: dict[str, list[Event]] = {}
        for name in z.namelist():
            if name.startswith("events/") and name.endswith("_events.jsonl"):
                evs = [Event.from_dict(json.loads(l)) for l in z.read(name).decode("utf-8").splitlines() if l.strip()]
                if evs:
                    logs[evs[0].participant_id] = evs
    return manifest, logs


# ==================================================================== サイクルごとの資料
def build_items(pid: str, events: list[Event], cfg: dict, phases: Iterable[str]) -> list[Item]:
    """イベントを1つずつ判定エンジンに通し、サイクルの開始・打ち切り・終了時点のコードを取る。"""
    eng = Engine(cfg)
    evs = ordered(events)
    start_code: dict[str, str] = {}
    end_code: dict[str, str] = {}
    cut: dict[str, tuple[float, str]] = {}          # cycle_id -> (支援の提示時刻, その時点のコード)
    opp_cycle: dict[str, str] = {}
    n_closed = 0
    last_t = None
    for ev in evs:
        before = eng.doc.text
        eng.ingest(ev)
        last_t = ev.t
        for d in eng.decisions[len(opp_cycle):]:
            opp_cycle[d.opportunity_id] = d.cycle_id
        if eng.open is not None and eng.open.cycle_id not in start_code:
            start_code[eng.open.cycle_id] = eng.open.start_doc.text
        while n_closed < len(eng.cycles):
            # 閉じるきっかけのイベント（次の質問・課題の開始など）はコードを変えないので、その直前のコード
            end_code[eng.cycles[n_closed].cycle_id] = before
            n_closed += 1
        if ev.type == "SUPPORT_SHOWN":
            cid = opp_cycle.get(ev.payload.get("opportunity_id", ""))
            if cid and cid not in cut:
                cut[cid] = (ev.t, eng.doc.text)
    final_text = eng.doc.text
    eng.finish(last_t)
    for c in eng.cycles[n_closed:]:
        end_code[c.cycle_id] = final_text

    latency = {}
    draft_t = {e.event_id: e.t for e in evs if e.type == "PROMPT_DRAFT_SUBMIT"}
    for d in eng.decisions:
        if d.prompt_event_id in draft_t:
            latency[d.cycle_id] = round(d.t - draft_t[d.prompt_event_id], 3)

    items = []
    seen_responses: dict[str, int] = {}
    phases = set(phases)
    for c in eng.cycles:
        cutoff = cut.get(c.cycle_id)
        limit_t = cutoff[0] if cutoff else None
        prompt_ids = set(c.prompt_event_ids)
        tl = []
        for e in evs:
            if e.type not in SHOWN_TYPES or e.in_support:
                continue
            if e.type == "PROMPT_DRAFT_SUBMIT":
                if e.event_id not in prompt_ids:
                    continue
            elif not (c.start_t <= e.t <= c.end_t) or e.task_id != c.task_id:
                continue
            elif e.type == "AI_RESPONSE" and e.payload.get("prompt_event_id") not in (None, *prompt_ids):
                continue                     # 次の質問への回答（同時刻のとき）
            if limit_t is not None and e.t >= limit_t and e.event_id != c.prompt_event_ids[0]:
                continue
            tl.append(e)
        it = Item(pid, "", c, start_code.get(c.cycle_id, ""),
                  cutoff[1] if cutoff else end_code.get(c.cycle_id, ""), tl, limit_t,
                  latency.get(c.cycle_id), dict(seen_responses))
        for e in tl:
            if e.type == "AI_RESPONSE":
                seen_responses.setdefault(str(e.payload.get("response_id")), len(seen_responses) + 1)
        if c.phase in phases:
            it.group = eng.group or ""
            items.append(it)
    return items


# ==================================================================== HTML
CSS = """
body{font-family:system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;max-width:980px;margin:0 auto;
padding:16px;color:#1d1d1f;background:#fff;line-height:1.6}
h1{font-size:20px} h2{font-size:16px;margin-top:24px;border-bottom:1px solid #ddd}
.ev{border-left:4px solid #ccc;padding:4px 12px;margin:10px 0}
.ev.q{border-color:#2357c6}.ev.a{border-color:#7a4cc2}.ev.e{border-color:#999}.ev.p{border-color:#c27a00}
.ev.r{border-color:#1c7c3a}.ev.x{border-color:#b3261e}
.t{color:#666;font-size:12px;margin-right:8px} .k{font-weight:700}
pre{background:#f6f6f6;padding:8px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;font-size:13px}
.diff .add{background:#e6ffec}.diff .del{background:#ffebe9}
.note{color:#8a5a00} table{border-collapse:collapse} td,th{border:1px solid #ccc;padding:4px 8px}
"""


def _esc(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def _diff_html(a: str, b: str) -> str:
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), "前", "後", lineterm="", n=2))
    if not lines:
        return "<p class='t'>（コードの変化なし）</p>"
    out = []
    for ln in lines[2:]:
        cls = "add" if ln.startswith("+") else "del" if ln.startswith("-") else ""
        out.append(f"<span class='{cls}'>{_esc(ln)}</span>")
    return "<pre class='diff'>" + "\n".join(out) + "</pre>"


def _mmss(dt: float) -> str:
    dt = max(0, int(round(dt)))
    return f"{dt // 60}:{dt % 60:02d}"


def item_html(item_id: str, it: Item, task_text: str) -> str:
    c = it.cycle
    parts = [f"<title>{item_id}</title><style>{CSS}</style>", f"<h1>評価項目 {item_id}</h1>",
             "<h2>課題</h2>", f"<pre>{_esc(task_text)}</pre>",
             "<h2>質問の時点のコード</h2>", f"<pre>{_esc(it.start_code) or '（空）'}</pre>",
             "<h2>経過</h2>"]
    resp_no = dict(it.responses_before)
    shown_code = it.start_code
    doc = it.start_code
    pending_typed = [0, 0]           # 挿入・削除の文字数（連続する打鍵をまとめる）

    def flush_typed():
        if pending_typed != [0, 0]:
            parts.append(f"<div class='ev e'><span class='k'>編集（キーボードなど）</span> "
                         f"挿入 {pending_typed[0]} 文字・削除 {pending_typed[1]} 文字</div>")
            pending_typed[:] = [0, 0]

    for e in it.timeline:
        p = e.payload
        ts = f"<span class='t'>{_mmss(e.t - c.start_t)}</span>"
        if e.type == "CODE_SNAPSHOT":
            if isinstance(p.get("text"), str):
                doc = p["text"]          # 実行の直前などに保存された実際のコード（編集の記録に欠けがあっても正しくなる）
            continue
        if e.type == "CODE_EDIT":
            off, dl, ins = int(p.get("offset", -1)), int(p.get("delete_len", 0)), p.get("insert_text", "")
            if 0 <= off <= len(doc):
                doc = doc[:off] + ins + doc[off + dl:]
            if p.get("kind") == "paste":
                flush_typed()
                src = p.get("source") or {}
                if src.get("type") == "ai":
                    rid = str(src.get("response_id"))
                    n = resp_no.get(rid)
                    origin = f"AI 回答 {n} のコード" if n else "AI 回答のコード"
                elif src.get("type") == "editor":
                    origin = "エディタ内でコピーした文字列"
                else:
                    origin = "由来不明"
                parts.append(f"<div class='ev p'>{ts}<span class='k'>貼り付け</span>（{origin}・{len(ins)} 文字"
                             f"{'・選択範囲 ' + str(dl) + ' 文字を置き換え' if dl else ''}）"
                             f"<pre>{_esc(ins)}</pre></div>")
            else:
                pending_typed[0] += len(ins)
                pending_typed[1] += dl
            continue
        flush_typed()
        if e.type == "PROMPT_DRAFT_SUBMIT":
            first = e.event_id == c.prompt_event_ids[0]
            parts.append(f"<div class='ev q'>{ts}<span class='k'>{'質問' if first else '追加の質問'}</span>"
                         f"<pre>{_esc(p.get('text'))}</pre></div>")
        elif e.type == "AI_RESPONSE":
            rid = str(p.get("response_id"))
            resp_no.setdefault(rid, len(resp_no) + 1)
            parts.append(f"<div class='ev a'>{ts}<span class='k'>AI 回答 {resp_no[rid]}</span>"
                         f"<pre>{_esc(p.get('text'))}</pre></div>")
        elif e.type == "COMPILE":
            if not p.get("ok"):
                parts.append(f"<div class='ev x'>{ts}<span class='k'>実行しようとしたがコンパイルエラー</span>"
                             f"{_diff_html(shown_code, doc)}<pre>{_esc(p.get('diagnostics') or p.get('stderr'))}</pre></div>")
                shown_code = doc
        elif e.type == "RUN":
            parts.append(f"<div class='ev r'>{ts}<span class='k'>実行</span>（前回表示したコードからの変更）"
                         f"{_diff_html(shown_code, doc)}"
                         f"<table><tr><th>入力</th><th>出力</th><th>終了コード</th></tr>"
                         f"<tr><td><pre>{_esc(p.get('stdin'))}</pre></td><td><pre>{_esc(p.get('stdout'))}</pre></td>"
                         f"<td>{_esc(p.get('exit_code'))}{' ・' + _esc(p.get('limit')) if p.get('limit') else ''}</td></tr></table></div>")
            shown_code = doc
        elif e.type == "TASK_SUBMIT":
            parts.append(f"<div class='ev r'>{ts}<span class='k'>提出して採点</span>："
                         f"{'合格' if p.get('passed') else '不合格'}</div>")
    flush_typed()
    if it.cutoff_t is not None:
        parts.append("<p class='note'>（ここから先の記録は評価の対象外です）</p>")
    parts.append("<h2>この区間の最後のコード（前回表示したコードからの変更）</h2>")
    parts.append(_diff_html(shown_code, it.end_code))
    parts.append(f"<details><summary>最後のコード全体</summary><pre>{_esc(it.end_code)}</pre></details>")
    return "<!doctype html><meta charset='utf-8'>" + "\n".join(parts)


GUIDE = """# 記入の手引き（定義の部分は下書き：研究者が確定させる）

## 進め方（正解は合議で決める）

1. **独立に記入**：評価者2名が、相談せずにそれぞれ全項目に記入する。
   `label_sheet.csv` をコピーして自分の名前を付け（例：`rater_a.csv`）、各行に記入する。
2. **一致の確認**：研究者が `python -m server.labeling disagree --dir <資料> --labels rater_a.csv rater_b.csv` を実行し、
   評価者間の一致（一致率・Cohen の κ）を記録する。合議の前の κ を報告する。
3. **合議**：`consensus/disagreements.html` に不一致の項目だけが並ぶ。2名（必要なら第三者）で話し合い、
   `consensus/gold.csv`（`gold_draft.csv` をコピーしたもの）の空欄を埋める。一致した項目は記入済み。
   合議でも決まらないものは `Undeterminable` とする。
4. **集計**：`python -m server.labeling score --dir <資料> --labels rater_a.csv rater_b.csv --gold consensus/gold.csv`。

## 記入のしかた

各項目（index.html から開く）は、学習者が AI に1つの質問をしてから次の新しい質問をするまでの区間です。
画面に出ている情報だけで判断してください（前後の項目は別の学習者のことがあります）。

| 列 | 値（いずれか1つ） |
|---|---|
| HS | 質問のしかた：`Passive` / `Active` / `Constructive` / `Undeterminable`（判定不能） |
| RU | 回答の使い方：`Passive` / `Active` / `Constructive` / `Undeterminable`（判定不能） |
| note | 任意（迷った点など） |

`Undeterminable` の代わりに「判定不能」と書いてもかまいません。空欄は「未記入」として扱います（判定不能とは別）。

**判定不能を選ぶとき**：示された情報だけでは決められないとき。とくに「（ここから先の記録は評価の対象外です）」と
表示された項目は、途中で記録が打ち切られているため、RU は判定不能になることが多いです。無理に Passive にしないでください。

## HS（援助要請）の目安（仕様 §3）
- Constructive：自分の仮説や考えを示している、または完成コード・答えそのもの**以外**の支援の形を主体的に指定している
  （「ヒントだけ」「この行の意味だけ」「手順だけ」など。完成コードや全文を求めることは支援の形の指定ではない）。
- Active：自分の状況の**具体的な一点**を示している（上に当てはまらない）。具体的な一点とは、自分のコードの特定の箇所、
  エラーメッセージ、実際の出力と期待との違い、特定の構文や概念。課題の目標の言い直しだけ、
  「どこから」「どうすれば」と漠然と尋ねるだけのものは含まない。
- Passive：上のどちらでもない（完成コードだけを求める、漠然と尋ねる等）。
- 「完成コードではなく」のような否定に注意。最初の「質問」で判断する（「追加の質問」は RU の確認質問の手がかり）。

境界例：
| 質問 | 判断 | 理由 |
|---|---|---|
| 最大値と最小値を両方返す関数を作りたいです。どこから考えればいいですか | Passive | 課題の目標の言い直しと「どこから」の漠然とした問いで、具体的な一点がない |
| じゃあ配列を関数に渡すときも同じですか | Active | 特定の概念（配列を関数に渡すこと）を示している |
| swap関数のコードを全部書いてください | Passive | 完成コードの要求。支援の形の指定ではない |
| 完成コードではなく、考え方のヒントだけ教えてください | Constructive | 完成コード以外の支援の形（ヒントだけ）を指定している |

## RU（回答利用）の目安（仕様 §4）
- Constructive：仮説を言語化→対応する変更→実行で確認、または異なる入力で2回以上テスト→修正→前のテストを再実行、
  という検証の流れがあり、かつ複数回のテストか自力での新規作成がある。
- Active：AI のコードを意味のある形で書き換えた、構造を変えた、または回答の内容・正しさを確かめる質問をした。
- Passive：AI のコードをほぼそのまま採用した。
- 何もしていない・記録が足りない場合は Passive ではなく `Undeterminable`。
"""


# ==================================================================== make
def make(zip_path: str, out_dir: str, seed: int, phases: list[str], cfg: dict,
         tasks_dir: str, include_notes: bool = False, allow_config_mismatch: bool = False) -> dict:
    manifest, logs = read_zip(zip_path)
    notes = {p["participant_id"]: p.get("note", "") for p in manifest.get("participants", [])}
    # 実験で使った判定エンジンの設定と同じでなければ作らない（システムの判定が本番と食い違うため）
    want = config_hash(cfg)
    used = {}
    for pid, evs in logs.items():
        ss = next((e for e in evs if e.type == "SESSION_START"), None)
        if ss is not None:
            used[pid] = (ss.payload.get("rule_version"), ss.payload.get("config_hash"))
    mismatch = {pid: v for pid, v in used.items() if v[1] != want
                and (include_notes or notes.get(pid) != "loadtest")}
    if mismatch and not allow_config_mismatch:
        raise ValueError(f"判定エンジンの設定が実験時と違います（今: {cfg['rule_version']} {want}）: "
                         f"{dict(list(mismatch.items())[:5])}。実験時の設定を --engine-config で渡してください")
    items: list[Item] = []
    for pid in sorted(logs):
        if not include_notes and notes.get(pid) == "loadtest":
            continue
        items.extend(build_items(pid, logs[pid], cfg, phases))
    rng = random.Random(seed)
    rng.shuffle(items)
    task_cache: dict[str, str] = {}

    def task_text(tid: str) -> str:
        if tid not in task_cache:
            try:
                t = load_task(os.path.join(tasks_dir, f"{tid}.json"))
                task_cache[tid] = f"{t.title}\n\n{t.description}"
            except OSError:
                task_cache[tid] = tid
        return task_cache[tid]

    rdir, kdir = os.path.join(out_dir, "for_raters"), os.path.join(out_dir, "_key")
    os.makedirs(os.path.join(rdir, "items"), exist_ok=True)
    os.makedirs(kdir, exist_ok=True)
    width = max(3, len(str(len(items))))
    rows = []
    for k, it in enumerate(items, 1):
        iid = f"I{k:0{width}d}"
        with open(os.path.join(rdir, "items", f"{iid}.html"), "w", encoding="utf-8") as f:
            f.write(item_html(iid, it, task_text(it.cycle.task_id)))
        c = it.cycle
        rows.append({
            "item_id": iid, "participant_id": it.participant_id, "group": it.group, "cycle_id": c.cycle_id,
            "task_id": c.task_id, "phase": c.phase, "sys_hs": c.hs or "Unknown", "sys_ru": c.ru,
            "sys_state": c.state, "hs_reason": c.hs_reason, "ru_reason": c.ru_reason,
            "unknown_reasons": json.dumps(c.unknown_reasons, ensure_ascii=False),
            "intervened": c.intervened, "truncated_at_support": it.cutoff_t is not None,
            "contamination_suspected": c.features.get("contamination_suspected"),
            "decision_latency_s": it.decision_latency_s,
            "state_available_after_s": round(c.end_t - c.start_t, 3),
            "close_reason": c.close_reason, "rule_version": c.rule_version, "config_hash": c.config_hash,
        })
    with open(os.path.join(rdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset='utf-8'><title>評価項目一覧</title><style>{CSS}</style>"
                f"<h1>評価項目一覧（{len(rows)} 件）</h1>"
                f"<p>記入は guide.md を読んでから。値は Passive / Active / Constructive / Undeterminable（判定不能）。</p><ol>" +
                "".join(f"<li><a href='items/{r['item_id']}.html'>{r['item_id']}</a></li>" for r in rows) + "</ol>")
    with open(os.path.join(rdir, "label_sheet.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "HS", "RU", "note"])
        for r in rows:
            w.writerow([r["item_id"], "", "", ""])
    with open(os.path.join(rdir, "guide.md"), "w", encoding="utf-8") as f:
        f.write(GUIDE)
    with open(os.path.join(kdir, "key.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["item_id"])
        w.writeheader()
        w.writerows(rows)
    info = {"source_zip": os.path.basename(zip_path), "seed": seed, "phases": phases, "items": len(rows),
            "participants": len({r["participant_id"] for r in rows}), "rule_version": cfg["rule_version"],
            "export_rule_version": manifest.get("rule_version"),
            "config_hash": want, "config_mismatch_participants": sorted(mismatch),
            "excluded_notes": [] if include_notes else ["loadtest"]}
    with open(os.path.join(kdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return info


# ==================================================================== score
def read_labels(path: str) -> dict[str, dict[str, str]]:
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            iid = (row.get("item_id") or "").strip()
            if not iid:
                continue
            lab = {}
            for dim, allowed in (("HS", HS_LABELS), ("RU", RU_LABELS)):
                v = (row.get(dim) or "").strip()
                v = ALIASES.get(v, v)
                if v and v not in allowed and v != NA:
                    raise ValueError(f"{path}: {iid} の {dim} が不正です: {v!r}（{'/'.join(allowed)}/{NA}）")
                lab[dim] = v
            out[iid] = lab
    return out


def cohen_kappa(a: list[str], b: list[str]) -> Optional[float]:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def fleiss_kappa(table: list[list[str]], cats: Iterable[str]) -> Optional[float]:
    """table: 項目ごとの評価者のラベル（全項目で同じ人数）。"""
    cats = list(cats)
    if not table:
        return None
    m = len(table[0])
    if m < 2:
        return None
    n = len(table)
    counts = [[row.count(c) for c in cats] for row in table]
    pj = [sum(r[j] for r in counts) / (n * m) for j in range(len(cats))]
    Pi = [(sum(x * x for x in r) - m) / (m * (m - 1)) for r in counts]
    Pbar, Pe = sum(Pi) / n, sum(p * p for p in pj)
    return None if Pe == 1 else (Pbar - Pe) / (1 - Pe)


def macro_f1(gold: list[str], pred: list[str], labels: Iterable[str]) -> float:
    f1s = []
    for lab in labels:
        tp = sum(g == lab and p == lab for g, p in zip(gold, pred))
        fp = sum(g != lab and p == lab for g, p in zip(gold, pred))
        fn = sum(g == lab and p != lab for g, p in zip(gold, pred))
        f1s.append(0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn))
    return sum(f1s) / len(f1s)


def confusion(gold: list[str], pred: list[str], rows: Iterable[str], cols: Iterable[str]) -> dict:
    cols = list(cols)
    return {g: {p: sum(1 for x, y in zip(gold, pred) if x == g and y == p) for p in cols} for g in rows}


def _read_key(dir_: str) -> dict[str, dict]:
    with open(os.path.join(dir_, "_key", "key.csv"), encoding="utf-8-sig", newline="") as f:
        return {r["item_id"]: r for r in csv.DictReader(f)}


def _read_raters(label_paths: list[str]) -> dict[str, dict]:
    raters = {os.path.splitext(os.path.basename(p))[0]: read_labels(p) for p in label_paths}
    if len(raters) != len(label_paths):
        raise ValueError("評価者のファイル名が重複しています（ファイル名を評価者名として使います）")
    return raters


def inter_rater(key: dict, raters: dict) -> dict:
    """評価者間の一致（全員が記入した項目）。"""
    agree = {}
    for dim in ("HS", "RU"):
        ids = [i for i in key if all(r.get(i, {}).get(dim) for r in raters.values())]
        table = [[r[i][dim] for r in raters.values()] for i in ids]
        d: dict[str, Any] = {"n": len(ids),
                             "percent_agreement": None if not ids else sum(len(set(t)) == 1 for t in table) / len(ids),
                             "fleiss_kappa": fleiss_kappa(table, (*HS_LABELS, NA))}
        if len(raters) == 2:
            d["cohen_kappa"] = cohen_kappa([t[0] for t in table], [t[1] for t in table])
        agree[dim] = d
    return agree


def disagree(dir_: str, label_paths: list[str]) -> dict:
    """合議用：不一致の項目だけの一覧（HTML・CSV）と、一致した値を記入済みの gold_draft.csv を作る。"""
    key = _read_key(dir_)
    raters = _read_raters(label_paths)
    names = list(raters)
    out = os.path.join(dir_, "consensus")
    os.makedirs(out, exist_ok=True)
    draft, rows = [], []
    missing = {n: [i for i in key if not all(raters[n].get(i, {}).get(d) for d in ("HS", "RU"))] for n in names}
    for iid in key:
        g, diff = {}, []
        for dim in ("HS", "RU"):
            vals = [raters[n].get(iid, {}).get(dim, "") for n in names]
            if all(vals) and len(set(vals)) == 1:
                g[dim] = vals[0]
            else:
                g[dim] = ""
                diff.append(dim)
        draft.append([iid, g["HS"], g["RU"], ""])
        if diff:
            rows.append((iid, diff, {n: raters[n].get(iid, {}) for n in names},
                         {n: _note(label_paths[k], iid) for k, n in enumerate(names)}))
    with open(os.path.join(out, "gold_draft.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "HS", "RU", "note"])
        w.writerows(draft)
    with open(os.path.join(out, "disagreements.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "dims"] + [f"{d}_{n}" for n in names for d in ("HS", "RU")] + [f"note_{n}" for n in names])
        for iid, diff, labs, notes in rows:
            w.writerow([iid, "+".join(diff)] + [labs[n].get(d, "") for n in names for d in ("HS", "RU")] +
                       [notes[n] for n in names])
    head = "".join(f"<th>{_esc(n)}</th>" for n in names)
    body = []
    for iid, diff, labs, notes in rows:
        cells = []
        for n in names:
            v = " / ".join(f"{d}: {_esc(labs[n].get(d) or '（未記入）')}" for d in ("HS", "RU"))
            cells.append(f"<td>{v}{'<br><span class=t>' + _esc(notes[n]) + '</span>' if notes[n] else ''}</td>")
        body.append(f"<tr><td><a href='../for_raters/items/{iid}.html'>{iid}</a></td><td>{'・'.join(diff)}</td>{''.join(cells)}</tr>")
    ir = inter_rater(key, raters)
    with open(os.path.join(out, "disagreements.html"), "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset='utf-8'><title>合議用の一覧</title><style>{CSS}</style>"
                f"<h1>合議用の一覧（不一致 {len(rows)} 件／全 {len(key)} 件）</h1>"
                f"<p>合議の結果は gold_draft.csv をコピーした gold.csv の空欄に記入する（一致した項目は記入済み）。</p>"
                f"<table><tr><th>項目</th><th>不一致</th>{head}</tr>{''.join(body)}</table>")
    res = {"items": len(key), "disagreements": len(rows), "inter_rater": ir,
           "missing": {n: len(v) for n, v in missing.items()}, "out": out}
    with open(os.path.join(out, "inter_rater_before_consensus.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    return res


def _note(path: str, iid: str) -> str:
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("item_id") or "").strip() == iid:
                return (row.get("note") or "").strip()
    return ""


def score(dir_: str, label_paths: list[str], gold_path: Optional[str] = None) -> dict:
    key = _read_key(dir_)
    raters = _read_raters(label_paths)
    res: dict[str, Any] = {"items": len(key), "raters": list(raters)}
    res["inter_rater"] = inter_rater(key, raters)

    # ---- 正解（合議の結果があればそれ。なければ全員一致の項目だけ）
    if gold_path:
        gold = read_labels(gold_path)
        res["gold"] = "consensus_file"
        res["gold_missing"] = {d: sum(1 for i in key if not gold.get(i, {}).get(d)) for d in ("HS", "RU")}
    else:
        gold = {}
        for i in key:
            g = {}
            for dim in ("HS", "RU"):
                vals = [r.get(i, {}).get(dim) for r in raters.values()]
                g[dim] = vals[0] if vals and all(vals) and len(set(vals)) == 1 else ""
            gold[i] = g
        res["gold"] = "unanimous_only"   # 合議の前の目安（本番の報告は --gold で合議の結果を渡す）

    # ---- システムとの一致
    sys_col = {"HS": "sys_hs", "RU": "sys_ru"}
    # RU は「支援表示で打ち切った項目を除いた結果」（not_truncated）も出す（打ち切ると RU が判定不能になりやすいため）
    res["truncated_items"] = sum(1 for r in key.values() if r["truncated_at_support"] == "True")
    for dim, labels in (("HS", HS_LABELS), ("RU", RU_LABELS)):
        for subset, cond in (("all", lambda r: True),
                             ("not_truncated", lambda r: r["truncated_at_support"] != "True")):
            ids = [i for i in key if cond(key[i]) and gold.get(i, {}).get(dim) in labels]
            g = [gold[i][dim] for i in ids]
            p = [key[i][sys_col[dim]] for i in ids]
            res[f"{dim}_{subset}"] = {
                "n": len(ids),
                "n_gold_undeterminable": sum(1 for i in key if cond(key[i]) and gold.get(i, {}).get(dim) == NA),
                "macro_f1": None if not ids else macro_f1(g, p, labels),
                "accuracy": None if not ids else sum(x == y for x, y in zip(g, p)) / len(ids),
                "system_unknown_rate": None if not ids else sum(x == "Unknown" for x in p) / len(ids),
                "confusion_gold_by_system": confusion(g, p, labels, (*labels, "Unknown")),
            }
    # 9状態の一致（正解の HS・RU がともに3値のどれかの項目）
    ids = [i for i in key if gold.get(i, {}).get("HS") in HS_LABELS and gold.get(i, {}).get("RU") in RU_LABELS]
    gs = [f"{gold[i]['HS'][0]}-{gold[i]['RU'][0]}" for i in ids]
    ps = [key[i]["sys_state"] for i in ids]
    determined = [(g, p) for g, p in zip(gs, ps) if p != "Pending"]
    res["state9"] = {"n": len(ids),
                     "agreement": None if not ids else sum(g == p for g, p in zip(gs, ps)) / len(ids),
                     "pending_rate": None if not ids else sum(p == "Pending" for p in ps) / len(ids),
                     "agreement_when_system_determined": None if not determined else
                     sum(g == p for g, p in determined) / len(determined)}
    # 全項目での Unknown 率と判定遅延（正解の有無によらない）
    res["system_unknown_rate_all_items"] = {
        "HS": sum(r["sys_hs"] == "Unknown" for r in key.values()) / max(1, len(key)),
        "RU": sum(r["sys_ru"] == "Unknown" for r in key.values()) / max(1, len(key)),
        "Pending": sum(r["sys_state"] == "Pending" for r in key.values()) / max(1, len(key))}
    for col in ("decision_latency_s", "state_available_after_s"):
        xs = sorted(float(r[col]) for r in key.values() if r.get(col) not in ("", None, "None"))
        res[col] = None if not xs else {"n": len(xs), "median": statistics.median(xs),
                                        "p95": xs[min(len(xs) - 1, int(len(xs) * 0.95))], "max": xs[-1]}
    return res


# ==================================================================== CLI
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="RQ1 の人手ラベル付け用の資料と集計")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="資料を作る")
    m.add_argument("--zip", required=True, help="管理画面で書き出した zip")
    m.add_argument("--out", required=True)
    m.add_argument("--seed", type=int, required=True, help="項目の並びを決める種（記録に残す）")
    m.add_argument("--phases", default="main", help="対象フェーズ（カンマ区切り）")
    m.add_argument("--engine-config", help="判定エンジンの設定（本番と同じもの。省略時は既定）")
    m.add_argument("--tasks-dir", default=abspath("tasks"))
    m.add_argument("--include-loadtest", action="store_true")
    m.add_argument("--allow-config-mismatch", action="store_true",
                   help="実験時と違う設定でも作る（規則を変えた再分析用。manifest に記録する）")
    d = sub.add_parser("disagree", help="評価者間の一致と、合議用の不一致一覧・gold_draft.csv を作る")
    d.add_argument("--dir", required=True, help="make の --out")
    d.add_argument("--labels", nargs="+", required=True, help="評価者ごとの記入済み label_sheet（ファイル名が評価者名）")
    s = sub.add_parser("score", help="評価者のラベルを集計する")
    s.add_argument("--dir", required=True, help="make の --out")
    s.add_argument("--labels", nargs="+", required=True, help="評価者ごとの記入済み label_sheet（ファイル名が評価者名）")
    s.add_argument("--gold", help="合議で決めた正解（gold_draft.csv を埋めたもの）。省略時は全員一致の項目だけ（合議前の目安）")
    s.add_argument("--json", help="結果を JSON で保存する")
    a = ap.parse_args(argv)
    if a.cmd == "make":
        if os.path.exists(a.out) and os.listdir(a.out):
            ap.error(f"{a.out} は空ではありません（上書きしない）")
        cfg = load_config(a.engine_config) if a.engine_config else make_config()
        info = make(a.zip, a.out, a.seed, [x.strip() for x in a.phases.split(",") if x.strip()], cfg,
                    a.tasks_dir, a.include_loadtest, a.allow_config_mismatch)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        print(f"評価者には {os.path.join(a.out, 'for_raters')} だけを渡してください（_key は渡さない）。")
    elif a.cmd == "disagree":
        res = disagree(a.dir, a.labels)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print(f"合議用の一覧: {os.path.join(res['out'], 'disagreements.html')}")
    else:
        res = score(a.dir, a.labels, a.gold)
        text = json.dumps(res, ensure_ascii=False, indent=2)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                f.write(text)
        print(text)


if __name__ == "__main__":
    main()
