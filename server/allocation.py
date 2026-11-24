"""ブロック無作為化の割付表を作る（仕様 v1.2 §15.5）。実験前に作って凍結し、ハッシュを記録する。

    python -m server.allocation --blocks 10 --seed 20261124 --out data/allocation.csv

ブロックサイズ 4（適応2・一律2）。各ブロック内の順序を乱数で並べ替える。
乱数の種と生成したファイルのハッシュを実験記録に残すこと（同じ種から同じ表が再現できる）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys


def make_table(blocks: int, seed: int, block: tuple[str, ...] = ("adaptive", "adaptive", "uniform", "uniform")) -> list[tuple[int, int, str]]:
    rng = random.Random(seed)
    rows = []
    slot = 1
    for b in range(1, blocks + 1):
        order = list(block)
        rng.shuffle(order)
        for g in order:
            rows.append((slot, b, g))
            slot += 1
    return rows


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="ブロック無作為化の割付表を作る")
    ap.add_argument("--blocks", type=int, required=True, help="ブロック数（1ブロック4名）")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out", default="data/allocation.csv")
    ap.add_argument("--force", action="store_true", help="既存の表を上書きする（凍結後は使わない）")
    a = ap.parse_args(argv)
    if os.path.exists(a.out) and not a.force:
        sys.exit(f"{a.out} は既にあります（凍結済みの表を上書きしないため中止）")
    rows = make_table(a.blocks, a.seed)
    text = "slot,block,group\n" + "".join(f"{s},{b},{g}\n" for s, b, g in rows)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"{a.out}: {len(rows)} 枠  seed={a.seed}  sha256={hashlib.sha256(text.encode()).hexdigest()[:12]}")


if __name__ == "__main__":
    main()
