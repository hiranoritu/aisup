"""支援生成の同時実行試験（仕様 v1.2 §10 負荷試験の一部）：N 名が同時に支援を受けるときの生成時間。

    set -a; source .env; set +a
    python -m server.support_load --n 10 --rounds 2 [--types self_explain,trace_predict,mixed]

本番と同じ設定（server_config.json）・実際の LLM・Docker サンドボックス（同時実行数と待ち行列も本番と同じ）で、
N 件の生成を同時に始め、1件ごとの所要時間（待ち行列を含む）・成否・理由を集計する。
上限（support.generation_timeout_s、既定15秒）を超えた生成は、サーバでは generation_timeout として再生成または実施不能になる。
HTTP は通さない（生成の時間は LLM とサンドボックスでほぼ決まるため）。本番サーバの上で実行すると本番の CPU で測れる：
    docker compose --env-file deploy/.env exec app python -m server.support_load --n 10
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from collections import Counter

from .check_llm import RESPONSE_SHAPES, scrub
from .llm import OpenAIClient
from .prompts import load_prompt
from .sandbox import DockerSandbox, SandboxConfig
from .settings import abspath, load_server_config, validate_server_config
from .support import LLM_SUPPORT_TYPES, SupportGenerator
from .tasks import load_tasks, task_ids_from_phases

CODE_SHAPES = ["fragment_no_main", "partial_code", "multi_block"]   # コードのない回答は生成前に実施不能になるので除く


def burst(gen, task, types: list[str], n: int, label: str) -> list[dict]:
    """n 件の生成を同時に始める。"""
    start = threading.Barrier(n)
    rows: list[dict] = [{} for _ in range(n)]

    def one(i: int) -> None:
        st = types[i % len(types)]
        shape = CODE_SHAPES[i % len(CODE_SHAPES)]
        start.wait()
        t0 = time.monotonic()
        g = gen.generate(st, f"load-{label}-{i}", task, "r1", RESPONSE_SHAPES[shape])
        rows[i] = {"i": i, "support_type": st, "shape": shape, "ok": g.ok, "reason": g.reason,
                   "elapsed_s": round(time.monotonic() - t0, 2),
                   "attempts": [{"reason": a.reason, "ms": a.elapsed_ms,
                                 "llm_ms": a.llm.latency_ms if a.llm else None} for a in g.attempts]}

    ths = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return rows


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="支援生成の同時実行試験")
    ap.add_argument("--config", default="server_config.json")
    ap.add_argument("--n", type=int, default=10, help="同時に生成する件数（同時に支援を受ける参加者数）")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--types", default="self_explain,trace_predict,mixed",
                    help="試す組み合わせ（種類名、または mixed＝5種類を混ぜる）")
    a = ap.parse_args(argv)
    cfg = load_server_config(abspath(a.config))
    errs = validate_server_config(cfg)
    if errs:
        sys.exit("設定エラー:\n  " + "\n  ".join(errs))
    key = os.environ["OPENAI_API_KEY"]
    llm = OpenAIClient(key, cfg["llm"], cfg["openai_base_url"])
    sb = DockerSandbox(SandboxConfig(**cfg["sandbox"]))
    if not sb.available():
        sys.exit("サンドボックスのイメージがありません")
    prompts = {st: load_prompt(os.path.join(abspath(cfg["prompts"]["support_dir"]), f"{st}.md")) for st in LLM_SUPPORT_TYPES}
    gen = SupportGenerator(llm, prompts, sb, cfg)
    tasks = load_tasks(abspath(cfg["tasks_dir"]), task_ids_from_phases(cfg["phases"]))
    task = next(t for t in tasks if t.task_id == "main_swap")
    limit = float(cfg["support"]["generation_timeout_s"])
    print(f"同時 {a.n} 件 × {a.rounds} 回  上限 {limit:.0f} 秒  サンドボックス同時実行 {cfg['sandbox']['max_concurrent']}"
          f"  再生成 {'あり' if cfg['support']['regenerate_on_failure'] else 'なし'}", flush=True)
    report = {"t": time.time(), "n": a.n, "limit_s": limit, "sandbox": cfg["sandbox"], "results": {}}
    for combo in [x.strip() for x in a.types.split(",") if x.strip()]:
        types = list(LLM_SUPPORT_TYPES) if combo == "mixed" else [combo]
        rows = []
        for r in range(a.rounds):
            rows += burst(gen, task, types, a.n, f"{combo}-{r}")
        report["results"][combo] = rows
        el = [x["elapsed_s"] for x in rows]
        first = [x["attempts"][0]["ms"] / 1000 for x in rows if x["attempts"]]
        over = sum(1 for x in first if x > limit)
        print(f"\n== {combo}（{len(rows)} 件）", flush=True)
        print(f"  成功 {sum(x['ok'] for x in rows)}/{len(rows)}   所要（再生成を含む）中央値 {statistics.median(el):.1f}s "
              f"最大 {max(el):.1f}s")
        print(f"  1回目の生成  中央値 {statistics.median(first):.1f}s 最大 {max(first):.1f}s  上限超え {over} 件")
        reasons = Counter(at["reason"] for x in rows for at in x["attempts"] if at["reason"])
        if reasons:
            print(f"  失敗した生成の理由（再生成で成功したものを含む） {dict(reasons)}")
        if combo == "mixed":
            for st in types:
                xs = [x["elapsed_s"] for x in rows if x["support_type"] == st]
                print(f"    {st:16s} 中央値 {statistics.median(xs):.1f}s 最大 {max(xs):.1f}s  "
                      f"成功 {sum(x['ok'] for x in rows if x['support_type'] == st)}/{len(xs)}")
    os.makedirs(abspath("data"), exist_ok=True)
    path = abspath(f"data/support_load_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scrub(report, key), f, ensure_ascii=False, indent=2)
    print(f"\n詳細: {path}")


if __name__ == "__main__":
    main()
