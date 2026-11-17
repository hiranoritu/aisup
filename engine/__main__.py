"""ログ再生 CLI。

    python -m engine events.jsonl --out out/ [--config config.json]
"""
from __future__ import annotations

import argparse

from .config import load_config, make_config
from .core import replay
from .events import load_jsonl
from .export import export


def main() -> None:
    ap = argparse.ArgumentParser(description="イベントログを再生して判定結果を出力する")
    ap.add_argument("events")
    ap.add_argument("--out", default="out")
    ap.add_argument("--config")
    a = ap.parse_args()
    cfg = load_config(a.config) if a.config else make_config()
    eng = replay(load_jsonl(a.events), cfg)
    paths = export(eng, a.out)
    print(f"cycles={len(eng.cycles)} decisions={len(eng.decisions)} anomalies={len(eng.anomalies)}")
    for k, p in paths.items():
        print(f"{k}: {p}")


if __name__ == "__main__":
    main()
