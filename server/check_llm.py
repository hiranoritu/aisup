"""OpenAI 実接続の確認（本番前・モデル変更時・プロンプト変更時に実行する）。

    set -a; source .env; set +a
    python -m server.check_llm [--config server_config.json] [--repeat 10]
                               [--feature-models gpt-6.1-terra,gpt-6-luna] [--feature-repeat 2]

1. 役割ごとの疎通（モデル名・パラメータが受け付けられるか、応答時間、実際に応答したモデル名）
2. 質問特徴抽出：例文（境界になりやすいものを含む約20問）に対する h1〜h4 等が期待どおりか。
   --feature-models で複数のモデルを比べる（期待値との一致率・モデル間の一致率・所要時間）。
   人手ラベルとの一致確認の代わりではない。
3. 支援問題の生成→サンドボックス検証：型ごとに --repeat 回。AI 回答は実際に近い4つの形
   （main のない断片・途中までのコード・複数ブロック・コードのない説明）を順に使う。成功率・実施不能の理由・所要時間。
4. 記述採点：同じ自己説明の問題に対し、短い回答・十分な回答・明らかに不十分な回答を採点する。
結果は data/llm_check_<時刻>.json に保存する。API キーは出力・保存のどちらにも出さない（sk- で始まる文字列は伏せる）。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from typing import Any

from .features import extract_features
from .llm import OpenAIClient
from .prompts import load_prompt
from .sandbox import DockerSandbox, SandboxConfig
from .settings import abspath, load_server_config, validate_server_config
from .support import LLM_SUPPORT_TYPES, SupportGenerator, grade_text_llm
from .tasks import load_tasks, task_ids_from_phases

FEATURE_KEYS = ("h1", "h2", "h3", "h4", "is_followup", "asks_about_answer", "off_task")

# 直前の AI 回答（確認質問か新規質問かの判定用）
PREV_SWAP = ("swap の引数をポインタにして、呼び出し側でアドレスを渡します。\n```c\nvoid swap(int *a, int *b) {\n"
             "    int tmp = *a;\n    *a = *b;\n    *b = tmp;\n}\n```\n呼び出しは swap(&x, &y); です。")

# (質問文, 期待値（指定したキーだけ照合。空なら記録のみ）, 直前のAI回答, 分類)
FEATURE_CASES: list[tuple[str, dict, str, str]] = [
    # 完成コードの要求と支援の形の指定（h1 / h4）
    ("swap関数のコードを全部書いてください", {"h1": 1, "h3": 0, "h4": 0}, "", "完成コード"),
    ("修正したコードをください", {"h1": 1, "h4": 0}, "", "完成コード"),
    ("完成したコードではなく、考え方のヒントだけ教えてください", {"h1": 0, "h4": 1}, "", "否定"),
    ("ヒントではなく、完成したコードを全部見せてください", {"h1": 1, "h4": 0}, "", "否定"),
    ("答えは言わずに、どこが間違っているかだけ教えて", {"h1": 0, "h4": 1}, "", "否定"),
    ("コードは書かないで、手順だけ箇条書きで教えてください", {"h1": 0, "h4": 1}, "", "否定"),
    ("自分で考えたいので、正解は言わずに質問で導いてください", {"h1": 0, "h4": 1}, "", "否定"),
    ("int *p = &x; の行の意味だけ説明してください", {"h1": 0, "h2": 1, "h4": 1}, "", "支援の形"),
    ("まずヒントをください。それでもわからなければ完成コードもください", {"h1": 1, "h4": 1}, "", "コードとヒントの両方"),
    ("ヒントと完成コードの両方をください", {"h1": 1, "h4": 1}, "", "コードとヒントの両方"),
    # 仮説（h3）
    ("swap(x, y) を呼んでも x と y が入れ替わりません。関数の中で値を変えても値渡しだから戻らないのだと思うのですが合っていますか",
     {"h2": 1, "h3": 1, "h1": 0}, "", "仮説"),
    ("値渡しだから変わらないのだと思います。とりあえず完成コードを書いてください", {"h1": 1, "h3": 1, "h4": 0}, "",
     "考え＋完成コード"),
    ("printfで%dを使っているのに大きな変な数字が出ます。アドレスを表示してしまっているのかなと思っています",
     {"h2": 1, "h3": 1, "h1": 0}, "", "仮説"),
    # エラー文だけ
    ("main.c:12: warning: passing argument 1 of 'swap' makes pointer from integer without a cast と出ます",
     {"h2": 1, "h1": 0, "h3": 0}, "", "エラー文"),
    ("main.c:5:5: error: expected ';' before 'return'", {"h2": 1, "h1": 0, "h3": 0, "h4": 0}, "", "エラー文だけ"),
    ("Segmentation fault (core dumped)", {"h1": 0, "h3": 0, "h4": 0}, "", "エラー文だけ"),
    # 確認質問か新規質問か
    ("さっきの回答の *a = *b; はなぜ必要なんですか", {"is_followup": 1, "asks_about_answer": 1, "h1": 0, "h4": 0}, PREV_SWAP,
     "確認質問"),
    ("続けて", {"is_followup": 1, "asks_about_answer": 0}, PREV_SWAP, "確認質問"),
    ("次の課題で最大値と最小値を両方返す関数を作りたいです。どこから考えればいいですか", {"is_followup": 0}, PREV_SWAP,
     "新規質問"),
    ("じゃあ配列を関数に渡すときも同じですか", {}, PREV_SWAP, "曖昧（確認か新規か）"),
    # その他
    ("ポインタってなに？", {"h1": 0, "h2": 0, "h3": 0, "h4": 0}, "", "漠然"),
    ("今日の天気はどうですか", {"off_task": 1}, "", "課題外"),
]

# 実際の AI 回答に近い形（main_swap 用。課題が違う場合も「形」の確認として使う）
RESPONSE_SHAPES: dict[str, str] = {
    "fragment_no_main": (
        "swap の引数をポインタにします。\n\n```c\nvoid swap(int *a, int *b) {\n    int tmp = *a;\n    *a = *b;\n"
        "    *b = tmp;\n}\n```\n\nmain からは `swap(&x, &y);` のようにアドレスを渡して呼び出してください。"),
    "partial_code": (
        "途中まで書いてみます。続きは自分で考えてみてください。\n\n```c\nvoid swap(int *a, int *b) {\n"
        "    int tmp = *a;\n    // ここで *a と *b を入れ替える\n}\n\nint main(void) {\n    int x, y;\n"
        "    scanf(\"%d %d\", &x, &y);\n    swap(&x, &y);\n    printf(\"%d %d\\n\", x, y);\n    return 0;\n}\n```\n"),
    "multi_block": (
        "まず関数です。\n\n```c\nvoid swap(int *a, int *b) {\n    int tmp = *a;\n    *a = *b;\n    *b = tmp;\n}\n```\n\n"
        "呼び出し側はこうします。\n\n```c\nswap(&x, &y);\n```\n\n全体ではこうなります。\n\n```c\n#include <stdio.h>\n\n"
        "void swap(int *a, int *b) {\n    int tmp = *a;\n    *a = *b;\n    *b = tmp;\n}\n\nint main(void) {\n"
        "    int x, y;\n    scanf(\"%d %d\", &x, &y);\n    swap(&x, &y);\n    printf(\"%d %d\\n\", x, y);\n"
        "    return 0;\n}\n```\n\n入力例 `3 5` なら出力は\n\n```\n5 3\n```\n"),
    "explanation_only": (
        "C の関数は引数の値をコピーして受け取る（値渡し）ので、swap の中で引数を入れ替えても main の x と y は変わりません。"
        "main の変数そのものを変えたいときは、変数のアドレス（&x）を渡し、関数側ではポインタで受け取って、"
        "*a のように間接参照して書き換えます。一時変数を1つ使うと入れ替えられます。"),
}

# 記述採点に使う回答（問題は自己説明。swap のポインタ引数と間接参照についての問いを想定）
GRADE_ANSWERS: list[tuple[str, str]] = [
    ("short", "ポインタが指す先の値を入れ替えているので、呼び出し元の変数が変わる。"),
    ("sufficient",
     "main では swap(&x, &y) のように & で x と y のアドレスを渡し、swap はそれを int *a, int *b というポインタ引数で受け取る。"
     "関数の中では *a, *b と間接参照することで、a と b が指している main の x と y そのものを読み書きしている。"
     "tmp に *a の値を退避してから *a に *b を入れ、*b に tmp を入れるので、x と y の値が入れ替わる。"
     "値を直接渡す（値渡し）だと関数はコピーを入れ替えるだけで main の変数は変わらないが、"
     "アドレスを渡しているので main の変数を変更できる。"),
    ("insufficient", "よくわからないけど、動いたのでたぶん大丈夫だと思う。"),
]

_SECRET = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")


def scrub(obj: Any, key: str) -> Any:
    """API キー（と sk- で始まる文字列）を伏せる。"""
    if isinstance(obj, str):
        if key:
            obj = obj.replace(key, "[API_KEY]")
        return _SECRET.sub("sk-[伏せ字]", obj)
    if isinstance(obj, dict):
        return {k: scrub(v, key) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, key) for v in obj]
    return obj


def _ms_stats(xs: list[float]) -> str:
    return "—" if not xs else f"中央値 {statistics.median(xs):,.0f} ms / 最大 {max(xs):,.0f} ms"


def run_features(llm, pf, task, timeout_s: float, repeat: int, out) -> list[dict]:
    rows = []
    for text, expect, prev, cat in FEATURE_CASES:
        for k in range(repeat):
            res, o = extract_features(llm, pf, task, prev, text, timeout_s)
            feats = o.get("features") or {}
            miss = {kk: (feats.get(kk), v) for kk, v in expect.items() if feats.get(kk) != v}
            rows.append({"text": text, "category": cat, "repeat": k, "expect": expect, "ok": o["ok"],
                         "features": {kk: feats.get(kk) for kk in (*FEATURE_KEYS, "followup_confidence")},
                         "spans": feats.get("spans"), "mismatch": miss, "latency_ms": res.latency_ms,
                         "model": res.model, "error": o.get("error")})
            mark = "失敗" if not o["ok"] else ("一致" if not miss else ("記録" if not expect else "不一致"))
            got = " ".join(f"{kk}={feats.get(kk)}" for kk in ("h1", "h2", "h3", "h4") if feats)
            extra = f" followup={feats.get('is_followup')}({feats.get('followup_confidence')})" if prev else ""
            out(f"  [{mark}] {res.latency_ms:5d} ms  {text[:34]:34s}  {got}{extra}"
                f"{'  ずれ(得た値, 期待): ' + str(miss) if miss else ''}{o.get('error', '') or ''}")
    return rows


def summarize_features(rows: list[dict]) -> dict:
    judged = [r for r in rows if r["expect"]]
    keys = [(r, k) for r in judged for k in r["expect"]]
    lat = [r["latency_ms"] for r in rows if r["ok"]]
    # 同じ例文を繰り返したときの安定性（全反復で同じ特徴か）
    by_text: dict[str, list[dict]] = {}
    for r in rows:
        by_text.setdefault(r["text"], []).append(r["features"])
    stable = sum(1 for fs in by_text.values() if all({k: f.get(k) for k in FEATURE_KEYS} ==
                                                      {k: fs[0].get(k) for k in FEATURE_KEYS} for f in fs))
    return {"calls": len(rows), "failed": sum(not r["ok"] for r in rows),
            "case_agreement": sum(not r["mismatch"] and r["ok"] for r in judged) / max(1, len(judged)),
            "key_agreement": sum(r["ok"] and k not in r["mismatch"] for r, k in keys) / max(1, len(keys)),
            "stable_cases": f"{stable}/{len(by_text)}",
            "latency_median_ms": statistics.median(lat) if lat else None, "latency_max_ms": max(lat) if lat else None}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="OpenAI 実接続の確認")
    ap.add_argument("--config", default="server_config.json")
    ap.add_argument("--repeat", type=int, default=2, help="支援生成を型ごとに何回試すか（実際に近い回答の形を順に使う）")
    ap.add_argument("--feature-models", default="", help="比べる特徴抽出モデル（カンマ区切り。省略時は設定のモデルだけ）")
    ap.add_argument("--feature-repeat", type=int, default=1, help="例文ごとに何回抽出するか（安定性の確認）")
    ap.add_argument("--skip-support", action="store_true")
    ap.add_argument("--skip-features", action="store_true")
    ap.add_argument("--skip-grade", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_server_config(abspath(a.config))
    errs = validate_server_config(cfg)
    if errs:
        sys.exit("設定エラー:\n  " + "\n  ".join(errs))
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        sys.exit("OPENAI_API_KEY が設定されていません（set -a; source .env; set +a）")

    def out(msg: str) -> None:
        print(scrub(msg, api_key), flush=True)

    llm = OpenAIClient(api_key, cfg["llm"], cfg["openai_base_url"])
    report: dict = {"t": time.time(), "config_llm": cfg["llm"], "ping": {}, "features": {}, "support": {}, "grade": None}
    ok_all = True

    out("== 1. 疎通")
    for role, rc in cfg["llm"].items():
        json_mode = role != "chat"
        msgs = [{"role": "user", "content": '{"ok": 1} という JSON だけを返してください。' if json_mode else "1+1は？数字だけ答えて。"}]
        r = llm.complete(role, msgs, json_mode=json_mode, timeout_s=max(30, rc.get("timeout_s", 30)))
        report["ping"][role] = {"ok": r.ok, "latency_ms": r.latency_ms, "model": r.model, "error": r.error, "text": r.text[:200]}
        ok_all &= r.ok
        out(f"  {role:9s} {'OK ' if r.ok else 'NG '} {r.latency_ms:6d} ms  model={r.model}  {r.error[:200]}")

    tasks = load_tasks(abspath(cfg["tasks_dir"]), task_ids_from_phases(cfg["phases"]))
    task = next((t for t in tasks if t.task_id == "main_swap"), next(t for t in tasks if t.phase == "main"))
    pf = load_prompt(abspath(cfg["prompts"]["features"]))
    report["features_prompt_version"] = pf.version

    if not a.skip_features:
        models = [m.strip() for m in a.feature_models.split(",") if m.strip()] or [cfg["llm"]["features"]["model"]]
        for model in models:
            fcfg = copy.deepcopy(cfg["llm"])
            fcfg["features"]["model"] = model
            fl = OpenAIClient(api_key, fcfg, cfg["openai_base_url"])
            out(f"\n== 2. 質問特徴抽出（{model}・プロンプト {pf.version}・{len(FEATURE_CASES)} 問 × {a.feature_repeat} 回）")
            rows = run_features(fl, pf, task, float(cfg["llm"]["features"]["timeout_s"]), a.feature_repeat, out)
            report["features"][model] = {"rows": rows, "summary": summarize_features(rows)}
            ok_all &= all(r["ok"] for r in rows)
        out("\n   モデル            期待値との一致（問／特徴）  反復で同じ  失敗  所要時間")
        for model, v in report["features"].items():
            s = v["summary"]
            out(f"   {model:16s}  {s['case_agreement']:.0%} ／ {s['key_agreement']:.0%}            {s['stable_cases']:>6s}  "
                f"{s['failed']:3d}   {_ms_stats([r['latency_ms'] for r in v['rows'] if r['ok']])}")
        if len(report["features"]) >= 2:
            names = list(report["features"])
            base = {(r["text"], r["repeat"]): r["features"] for r in report["features"][names[0]]["rows"]}
            for other in names[1:]:
                cmp = [(base[(r["text"], r["repeat"])], r["features"]) for r in report["features"][other]["rows"]
                       if (r["text"], r["repeat"]) in base]
                per_key = {k: sum(x.get(k) == y.get(k) for x, y in cmp) / max(1, len(cmp)) for k in FEATURE_KEYS}
                report["features"][other]["agreement_with_" + names[0]] = per_key
                out(f"   {other} と {names[0]} の一致（特徴ごと）: " + "  ".join(f"{k} {v:.0%}" for k, v in per_key.items()))

    sb = None
    gen = None
    if not (a.skip_support and a.skip_grade):
        sb = DockerSandbox(SandboxConfig(**cfg["sandbox"]))
        if not sb.available():
            sys.exit("サンドボックスのイメージがありません: docker build -t aisup-sandbox:1 sandbox/")
        prompts = {st: load_prompt(os.path.join(abspath(cfg["prompts"]["support_dir"]), f"{st}.md")) for st in LLM_SUPPORT_TYPES}
        gen = SupportGenerator(llm, prompts, sb, cfg)

    shapes = list(RESPONSE_SHAPES)
    if not a.skip_support:
        out(f"\n== 3. 支援問題の生成→検証（型ごとに {a.repeat} 回。AI 回答の形: {', '.join(shapes)} を順に使う）")
        for st in LLM_SUPPORT_TYPES:
            rows = []
            for k in range(a.repeat):
                shape = shapes[k % len(shapes)]
                t0 = time.monotonic()
                g = gen.generate(st, f"check-{st}-{k}", task, "r1", RESPONSE_SHAPES[shape])
                el = time.monotonic() - t0
                rows.append({"shape": shape, "ok": g.ok, "reason": g.reason, "elapsed_s": round(el, 2),
                             "attempts": [{"reason": x.reason, "ms": x.elapsed_ms,
                                           "model": x.llm.model if x.llm else None} for x in g.attempts],
                             "public": g.public, "key": g.key})
            okn = sum(r["ok"] for r in rows)
            times = [r["elapsed_s"] for r in rows]
            final = Counter(r["reason"] for r in rows if not r["ok"])
            retried = Counter(x["reason"] for r in rows for x in r["attempts"] if x["reason"])
            per_shape = {s: f"{sum(r['ok'] for r in rows if r['shape'] == s)}/{sum(r['shape'] == s for r in rows)}"
                         for s in shapes}
            report["support"][st] = {"rows": rows, "success": okn, "n": len(rows), "final_reasons": dict(final),
                                     "attempt_failures": dict(retried), "per_shape": per_shape,
                                     "elapsed_median_s": statistics.median(times), "elapsed_max_s": max(times)}
            out(f"  {st:16s} 成功 {okn}/{len(rows)}  所要 中央値 {statistics.median(times):.1f}s 最大 {max(times):.1f}s")
            out(f"  {'':16s} 形ごとの成功 {per_shape}")
            if final:
                out(f"  {'':16s} 実施不能の理由 {dict(final)}")
            if retried:
                out(f"  {'':16s} 再生成になった失敗（成功したものを含む） {dict(retried)}")

    if not a.skip_grade:
        out("\n== 4. 記述採点")
        se = next((r for r in report["support"].get("self_explain", {}).get("rows", []) if r["ok"]), None)
        if se is None:
            g = gen.generate("self_explain", "check-grade", task, "r1", RESPONSE_SHAPES["fragment_no_main"])
            se = {"public": g.public, "key": g.key} if g.ok else None
        if se is None:
            out("  自己説明の問題を生成できなかったため省略")
        else:
            out(f"  問題: {se['public']['question']}")
            out(f"  観点: {se['key']['rubric']}")
            pg = load_prompt(abspath(cfg["prompts"]["grade"]))
            report["grade"] = {"question": se["public"]["question"], "rubric": se["key"]["rubric"],
                               "model_answer": se["key"].get("model_answer"), "prompt_version": pg.version, "answers": []}
            for name, ans in GRADE_ANSWERS:
                for k in range(2):
                    res, o = grade_text_llm(llm, pg, se["public"], se["key"], ans, float(cfg["llm"]["grade"]["timeout_s"]))
                    report["grade"]["answers"].append({"name": name, "answer": ans, "repeat": k,
                                                       "latency_ms": res.latency_ms, **o})
                    out(f"  [{name:12s}#{k}] {res.latency_ms:5d} ms  正解={o.get('correct')}  "
                        f"満たした {len(o.get('met') or [])}/{len(se['key']['rubric'])}  {o.get('error', '') or ''}")

    os.makedirs(abspath("data"), exist_ok=True)
    path = abspath(f"data/llm_check_{int(time.time())}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(scrub(report, api_key), f, ensure_ascii=False, indent=2)
    out(f"\n詳細: {path}")
    if not ok_all:
        sys.exit(1)


if __name__ == "__main__":
    main()
