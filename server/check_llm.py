"""OpenAI 実接続の確認（本番前・モデル変更時に実行する）。

    OPENAI_API_KEY=... python -m server.check_llm [--config server_config.json] [--repeat 3]

1. 役割ごとの疎通（モデル名・パラメータが受け付けられるか、応答時間、実際に応答したモデル名）
2. 質問特徴抽出：例文に対する h1〜h4 等が期待どおりか（人手ラベルとの一致確認の代わりではない）
3. 支援問題の生成→サンドボックス検証：型ごとの成否・理由・所要時間（上限 15 秒に収まるか）
4. 記述採点：観点の判定が返るか
結果は data/llm_check_<時刻>.json にも保存する。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

from .features import extract_features
from .llm import OpenAIClient
from .prompts import load_prompt
from .sandbox import DockerSandbox, SandboxConfig
from .settings import abspath, load_server_config, validate_server_config
from .support import LLM_SUPPORT_TYPES, SupportGenerator, grade_text_llm
from .tasks import load_tasks, task_ids_from_phases

# 例文と期待値（指定したキーだけ照合する）
FEATURE_CASES = [
    ("swap関数のコードを全部書いてください", {"h1": 1, "h3": 0, "h4": 0}),
    ("完成したコードではなく、考え方のヒントだけ教えてください", {"h1": 0, "h4": 1}),
    ("swap(x, y) を呼んでも x と y が入れ替わりません。関数の中で値を変えても値渡しだから戻らないのだと思うのですが合っていますか",
     {"h2": 1, "h3": 1}),
    ("main.c:12: warning: passing argument 1 of 'swap' makes pointer from integer without a cast と出ます", {"h2": 1, "h1": 0}),
    ("ポインタってなに？", {"h1": 0, "h2": 0, "h3": 0, "h4": 0}),
    ("今日の天気はどうですか", {"off_task": 1}),
]


def _p(msg: str) -> None:
    print(msg, flush=True)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="OpenAI 実接続の確認")
    ap.add_argument("--config", default="server_config.json")
    ap.add_argument("--repeat", type=int, default=2, help="支援生成を型ごとに何回試すか")
    ap.add_argument("--skip-support", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_server_config(abspath(a.config))
    errs = validate_server_config(cfg)
    if errs:
        sys.exit("設定エラー:\n  " + "\n  ".join(errs))
    llm = OpenAIClient(os.environ["OPENAI_API_KEY"], cfg["llm"], cfg["openai_base_url"])
    report: dict = {"t": time.time(), "config_llm": cfg["llm"], "ping": {}, "features": [], "support": {}, "grade": None}
    ok_all = True

    _p("== 1. 疎通")
    for role, rc in cfg["llm"].items():
        json_mode = role != "chat"
        msgs = [{"role": "user", "content": '{"ok": 1} という JSON だけを返してください。' if json_mode else "1+1は？数字だけ答えて。"}]
        r = llm.complete(role, msgs, json_mode=json_mode, timeout_s=max(30, rc.get("timeout_s", 30)))
        report["ping"][role] = {"ok": r.ok, "latency_ms": r.latency_ms, "model": r.model, "error": r.error, "text": r.text[:200]}
        ok_all &= r.ok
        _p(f"  {role:9s} {'OK ' if r.ok else 'NG '} {r.latency_ms:6d} ms  model={r.model}  {r.error[:200]}")
    if not all(v["ok"] for v in report["ping"].values()):
        _p("疎通に失敗した役割があります。モデル名・params（reasoning_effort 等）・response_format を見直してください。")

    tasks = load_tasks(abspath(cfg["tasks_dir"]), task_ids_from_phases(cfg["phases"]))
    task = next(t for t in tasks if t.phase == "main")
    pf = load_prompt(abspath(cfg["prompts"]["features"]))

    _p("\n== 2. 質問特徴抽出（例文）")
    for text, expect in FEATURE_CASES:
        res, out = extract_features(llm, pf, task, "", text, float(cfg["llm"]["features"]["timeout_s"]))
        feats = out.get("features") or {}
        miss = {k: (feats.get(k), v) for k, v in expect.items() if feats.get(k) != v}
        report["features"].append({"text": text, "expect": expect, "ok": out["ok"], "features": feats,
                                   "mismatch": miss, "latency_ms": res.latency_ms, "error": out.get("error")})
        mark = "NG" if not out["ok"] else ("一致" if not miss else "不一致")
        ok_all &= out["ok"]
        _p(f"  [{mark}] {res.latency_ms:5d} ms  {text[:40]}  {('ずれ: ' + str(miss)) if miss else ''}{out.get('error', '')}")

    if not a.skip_support:
        _p("\n== 3. 支援問題の生成→検証（AI回答＝課題の参照解として）")
        sb = DockerSandbox(SandboxConfig(**cfg["sandbox"]))
        if not sb.available():
            sys.exit("サンドボックスのイメージがありません: docker build -t aisup-sandbox:1 sandbox/")
        prompts = {st: load_prompt(os.path.join(abspath(cfg["prompts"]["support_dir"]), f"{st}.md")) for st in LLM_SUPPORT_TYPES}
        gen = SupportGenerator(llm, prompts, sb, cfg)
        response = "次のように書けます。\n\n```c\n" + task.reference_solution + "```\n"
        for st in LLM_SUPPORT_TYPES:
            rows = []
            for k in range(a.repeat):
                t0 = time.monotonic()
                g = gen.generate(st, f"check{k}", task, "r1", response)
                el = time.monotonic() - t0
                rows.append({"ok": g.ok, "reason": g.reason, "elapsed_s": round(el, 2),
                             "attempts": [{"reason": x.reason, "ms": x.elapsed_ms} for x in g.attempts],
                             "public": g.public, "key": g.key})
            report["support"][st] = rows
            okn = sum(r["ok"] for r in rows)
            times = [r["elapsed_s"] for r in rows]
            _p(f"  {st:16s} 成功 {okn}/{len(rows)}  所要 中央値 {statistics.median(times):.1f}s 最大 {max(times):.1f}s  "
               f"失敗理由 {[x['reason'] for r in rows for x in r['attempts'] if x['reason']]}")
        se = next((r for r in report["support"].get("self_explain", []) if r["ok"]), None)
        if se:
            _p("\n== 4. 記述採点")
            res, out = grade_text_llm(llm, load_prompt(abspath(cfg["prompts"]["grade"])), se["public"], se["key"],
                                      "ポインタが指す先の値を入れ替えているので、呼び出し元の変数が変わる。",
                                      float(cfg["llm"]["grade"]["timeout_s"]))
            report["grade"] = {"latency_ms": res.latency_ms, **out}
            _p(f"  {res.latency_ms} ms  {out}")

    os.makedirs(abspath("data"), exist_ok=True)
    path = abspath(f"data/llm_check_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    _p(f"\n詳細: {path}")
    if not ok_all:
        sys.exit(1)


if __name__ == "__main__":
    main()
