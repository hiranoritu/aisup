"""コンテナ内で動く実行器。標準入力で JSON を受け取り、標準出力に JSON を返す。

入力: {"src": str, "stdins": [str, ...], "cflags": [...], "limits": {...}}
出力: {"compile": {"ok", "exit_code", "diagnostics"},
       "runs": [{"stdin", "stdout", "stderr", "exit_code", "timed_out", "truncated", "signal", "sanitizer"}]}

CPU 時間・ファイルサイズ（=出力上限）・プロセス数は setrlimit、壁時計は timeout。
RLIMIT_AS は ASan の仮想メモリ予約と両立しないので使わず、メモリはコンテナの --memory で制限する。
"""
import json
import os
import resource
import signal
import subprocess
import sys

WORK = "/tmp/w"


def _limits(cpu_s, fsize, nproc):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))  # 先に SIGXCPU を受けさせる
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))  # 出力上限を兼ねる
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def _read_capped(data: bytes, cap: int):
    if len(data) > cap:
        return data[:cap].decode("utf-8", "replace"), True
    return data.decode("utf-8", "replace"), False


def main():
    req = json.load(sys.stdin)
    lim = req.get("limits", {})
    os.makedirs(WORK, exist_ok=True)
    src_path = os.path.join(WORK, "main.c")
    exe = os.path.join(WORK, "main")
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(req["src"])
    cflags = req.get("cflags") or []
    try:
        cp = subprocess.run(["gcc", *cflags, "-o", exe, src_path, "-lm"], capture_output=True,
                            timeout=lim.get("compile_timeout_s", 20))
        diag, _ = _read_capped(cp.stderr, 20000)
        comp = {"ok": cp.returncode == 0, "exit_code": cp.returncode,
                "diagnostics": diag.replace(src_path, "main.c")}
    except subprocess.TimeoutExpired:
        comp = {"ok": False, "exit_code": None, "diagnostics": "compile timeout"}
    out = {"compile": comp, "runs": []}
    if not comp["ok"]:
        print(json.dumps(out))
        return
    cap = lim.get("output_bytes", 65536)
    env = {"PATH": "/usr/bin:/bin",
           # LeakSanitizer は ptrace を要するため無効（メモリリークは判定対象外）
           "ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0:exitcode=86",
           "UBSAN_OPTIONS": "print_stacktrace=0:halt_on_error=1:exitcode=87"}
    out_path, err_path = os.path.join(WORK, "out"), os.path.join(WORK, "err")
    for stdin in req.get("stdins", [""]):
        r = {"stdin": stdin, "timed_out": False, "signal": None, "limit": None}
        # 出力はファイルに書かせ、RLIMIT_FSIZE で上限を超えたら SIGXFSZ で止める（メモリに溜めない）
        with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
            try:
                p = subprocess.run([exe], input=stdin.encode("utf-8"), stdout=fo, stderr=fe, env=env, cwd=WORK,
                                   timeout=lim.get("wall_s", 3),
                                   preexec_fn=_limits(lim.get("cpu_s", 2), cap + 1, lim.get("nproc", 16)))
                code = p.returncode
            except subprocess.TimeoutExpired:
                code = None
                r["timed_out"] = True
                r["limit"] = "wall"
        with open(out_path, "rb") as f:
            r["stdout"], t1 = _read_capped(f.read(cap + 1), cap)
        with open(err_path, "rb") as f:
            r["stderr"], t2 = _read_capped(f.read(cap + 1), cap)
        r["truncated"] = t1 or t2
        if code is not None and code < 0:
            r["signal"] = -code
            if -code == signal.SIGXCPU:
                r["limit"] = "cpu"        # RLIMIT_CPU 超過
                r["timed_out"] = True
            elif -code == signal.SIGKILL:
                r["limit"] = "killed"     # CPU ハードリミット到達かメモリ（cgroup OOM）。区別できない
            elif -code == signal.SIGXFSZ:
                r["limit"] = "output"
                r["truncated"] = True
        r["exit_code"] = code
        r["sanitizer"] = ("AddressSanitizer" in r["stderr"] or "runtime error:" in r["stderr"]
                          or code in (86, 87))
        out["runs"].append(r)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
