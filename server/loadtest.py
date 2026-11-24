"""負荷試験（仕様 v1.2 §10, §16-10）：N 名が同時にコンパイル・実行・AI 質問・提出を行う。

    python -m server.loadtest --url https://aisup.example.jp --admin-token ... --n 10 --minutes 5

参加者は管理APIで手動割付（群を交互）で発行するので、割付表は消費しない。発行した参加者は note="loadtest" が付く。
本番データと混ぜないため、負荷試験は本番の前に別のデータディレクトリで行うこと。
LLM を実際に呼ぶと費用がかかる（--prompt-every で質問の頻度を調整）。
結果：エンドポイントごとの応答時間（中央値・95%点・最大）とエラー数。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict

SWAP = """#include <stdio.h>

void swap(int *a, int *b) {
    int t = *a;
    *a = *b;
    *b = t;
}

int main(void) {
    int x, y;
    scanf("%d %d", &x, &y);
    swap(&x, &y);
    printf("%d %d\\n", x, y);
    return 0;
}
"""


class Client:
    def __init__(self, base: str, token: str, stats, errors, lock) -> None:
        self.base, self.token = base.rstrip("/"), token
        self.stats, self.errors, self.lock = stats, errors, lock
        self.seq = 0
        self.page = f"load-{random.random():.12f}"

    def req(self, name: str, path: str, body=None, timeout=120):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data,
                                   headers={"Content-Type": "application/json", "X-Token": self.token})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                out = json.loads(resp.read() or b"{}")
                code = resp.status
        except urllib.error.HTTPError as e:
            code, out = e.code, {"error": e.read()[:200].decode("utf-8", "replace")}
        except OSError as e:
            code, out = 0, {"error": str(e)}
        ms = (time.monotonic() - t0) * 1000
        with self.lock:
            self.stats[name].append(ms)
            if code != 200:
                self.errors[name].append(f"{code} {out.get('error')}")
        return code, out

    def events(self, items):
        evs = []
        for typ, pl in items:
            self.seq += 1
            evs.append({"client_seq": self.seq, "type": typ, "payload": pl, "client_t": self.seq * 100, "page_id": self.page})
        return self.req("events", "/api/events", {"page_id": self.page, "events": evs})

    def act(self, name, path, body):
        return self.req(name, path, {**body, "after_seq": self.seq, "page_id": self.page})


def participant(base, admin_token, idx, minutes, prompt_every, stats, errors, lock):
    r = urllib.request.Request(base.rstrip("/") + "/api/admin/participants",
                               data=json.dumps({"group": "adaptive" if idx % 2 else "uniform", "note": "loadtest"}).encode(),
                               headers={"Content-Type": "application/json", "X-Admin-Token": admin_token})
    with urllib.request.urlopen(r, timeout=30) as resp:
        token = json.loads(resp.read())["token"]
    c = Client(base, token, stats, errors, lock)
    info = {"user_agent": "loadtest", "mobile": False, "viewport_w": 1400, "supported_browser": True, "net_ms": 50}
    c.req("hello", "/api/hello", {"page_id": c.page, "info": info})
    c.req("consent", "/api/consent", {"agree": True})
    c.req("envcheck", "/api/envcheck", {"info": info})
    _, st = c.req("state", "/api/state")
    code = st.get("code", "")
    end = time.monotonic() + minutes * 60
    k = 0
    while time.monotonic() < end:
        k += 1
        # 打鍵（1文字ずつ20文字分を1回で送る）
        c.events([("CODE_EDIT", {"offset": len(code), "delete_len": 0, "insert_text": "/", "kind": "typed"})] +
                 [("CODE_EDIT", {"offset": len(code) + 1 + i, "delete_len": 0, "insert_text": "x", "kind": "typed"}) for i in range(19)])
        code += "/" + "x" * 19
        c.events([("CODE_EDIT", {"offset": len(code) - 20, "delete_len": 20, "insert_text": "", "kind": "typed"})])
        code = code[:-20]
        c.act("run", "/api/run", {"code": code, "stdin": "3 5\n"})
        if k % prompt_every == 0:
            _, r = c.act("prompt", "/api/prompt", {"text": f"ポインタの使い方を教えてください（{k}）"})
            sup = (r.get("state") or {}).get("support")
            if sup:
                c.act("support_end", "/api/support/end", {"opportunity_id": sup["opportunity_id"], "reason": "skipped"})
                if sup["support_type"] == "clarify_question":
                    c.act("clarify", "/api/clarify", {"opportunity_id": sup["opportunity_id"], "mode": "skipped"})
        if k % 3 == 0:
            c.act("submit", "/api/task/submit", {"code": SWAP})
        time.sleep(random.uniform(2, 5))


def main(argv=None):
    ap = argparse.ArgumentParser(description="負荷試験")
    ap.add_argument("--url", required=True)
    ap.add_argument("--admin-token", required=True)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--prompt-every", type=int, default=3, help="何回の操作ごとに AI に質問するか")
    a = ap.parse_args(argv)
    stats, errors, lock = defaultdict(list), defaultdict(list), threading.Lock()
    ths = [threading.Thread(target=participant, args=(a.url, a.admin_token, i, a.minutes, a.prompt_every, stats, errors, lock))
           for i in range(a.n)]
    t0 = time.monotonic()
    for t in ths:
        t.start()
        time.sleep(0.3)
    for t in ths:
        t.join()
    print(f"参加者 {a.n} 名・{time.monotonic() - t0:.0f} 秒")
    print(f"{'操作':10s} {'回数':>5s} {'中央値ms':>9s} {'95%点ms':>9s} {'最大ms':>9s} {'エラー':>6s}")
    for name in sorted(stats):
        xs = sorted(stats[name])
        p95 = xs[min(len(xs) - 1, int(len(xs) * 0.95))]
        print(f"{name:10s} {len(xs):5d} {statistics.median(xs):9.0f} {p95:9.0f} {max(xs):9.0f} {len(errors[name]):6d}")
    for name, es in errors.items():
        if es:
            print(f"  {name} のエラー例: {es[:3]}")


if __name__ == "__main__":
    main()
