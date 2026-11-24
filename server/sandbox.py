"""C コードの隔離実行（仕様 §1, §6, §10, §14）。

1回の呼び出しで1コンテナを起動し、コンパイルと（複数の標準入力での）実行を行う。
コンテナ：ネットワークなし・読み取り専用ルート・/tmp のみ書込可（容量制限）・全 capability 削除・
nobody 実行・CPU/メモリ/プロセス数制限。コンテナ内の制限は sandbox/runner.py。
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

CFLAGS = ["-std=c11", "-O0", "-g", "-Wall", "-Wextra", "-fsanitize=address,undefined",
          "-fno-omit-frame-pointer"]


@dataclass
class SandboxConfig:
    image: str = "aisup-sandbox:1"
    docker: str = "docker"
    memory: str = "256m"
    cpus: str = "1"
    pids: int = 64
    tmpfs_size: str = "32m"
    wall_s: float = 3.0
    cpu_s: int = 2
    output_bytes: int = 65536
    compile_timeout_s: float = 20.0
    host_timeout_s: float = 60.0   # docker 起動を含む全体の上限
    max_concurrent: int = 2        # 同時に動かすコンテナ数（CPU コア数まで。超えた分は待ち行列）
    queue_timeout_s: float = 60.0  # 待ち行列でこれ以上待ったら諦める


@dataclass
class RunOutcome:
    ok: bool                     # サンドボックス自体が動いたか（コンパイル失敗でも True）
    compile: dict[str, Any]
    runs: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""              # サンドボックス障害の理由
    image: str = ""
    wait_ms: int = 0             # 待ち行列で待った時間
    run_ms: int = 0              # コンテナの起動から終了まで

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "compile": self.compile, "runs": self.runs, "error": self.error,
                "image": self.image, "wait_ms": self.wait_ms, "run_ms": self.run_ms}


class DockerSandbox:
    def __init__(self, cfg: Optional[SandboxConfig] = None) -> None:
        self.cfg = cfg or SandboxConfig()
        self._slots = threading.BoundedSemaphore(self.cfg.max_concurrent)
        self._lock = threading.Lock()
        self.waiting = 0      # 待ち行列の長さ（管理画面・負荷試験用）
        self.running = 0

    def available(self) -> bool:
        try:
            p = subprocess.run([self.cfg.docker, "image", "inspect", self.cfg.image], capture_output=True,
                               timeout=10)
            return p.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def run(self, src: str, stdins: list[str]) -> RunOutcome:
        """同時実行数を max_concurrent までに抑えて実行する（仕様 v1.2 §15.6）。"""
        t0 = time.monotonic()
        with self._lock:
            self.waiting += 1
        got = self._slots.acquire(timeout=self.cfg.queue_timeout_s)
        with self._lock:
            self.waiting -= 1
            if got:
                self.running += 1
        wait_ms = int((time.monotonic() - t0) * 1000)
        if not got:
            return RunOutcome(False, {}, error="queue_timeout", image=self.cfg.image, wait_ms=wait_ms)
        try:
            t1 = time.monotonic()
            out = self._run(src, stdins)
            out.wait_ms = wait_ms
            out.run_ms = int((time.monotonic() - t1) * 1000)
            return out
        finally:
            with self._lock:
                self.running -= 1
            self._slots.release()

    def _run(self, src: str, stdins: list[str]) -> RunOutcome:
        c = self.cfg
        name = "aisup-" + uuid.uuid4().hex[:12]
        cmd = [c.docker, "run", "--rm", "-i", "--name", name,
               "--network", "none", "--read-only",
               "--tmpfs", f"/tmp:rw,exec,nosuid,size={c.tmpfs_size}",
               "--memory", c.memory, "--memory-swap", c.memory, "--cpus", c.cpus,
               "--pids-limit", str(c.pids), "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--user", "nobody", c.image]
        req = {"src": src, "stdins": stdins, "cflags": CFLAGS,
               "limits": {"wall_s": c.wall_s, "cpu_s": c.cpu_s, "output_bytes": c.output_bytes,
                          "compile_timeout_s": c.compile_timeout_s}}
        try:
            p = subprocess.run(cmd, input=json.dumps(req).encode("utf-8"), capture_output=True,
                               timeout=c.host_timeout_s)
        except subprocess.TimeoutExpired:
            subprocess.run([c.docker, "kill", name], capture_output=True)
            return RunOutcome(False, {}, error="host_timeout", image=c.image)
        except OSError as e:
            return RunOutcome(False, {}, error=f"docker_unavailable:{e}", image=c.image)
        try:
            out = json.loads(p.stdout.decode("utf-8"))
        except ValueError:
            # コンテナ自体が OOM 等で落ちた
            err = p.stderr.decode("utf-8", "replace")[-500:]
            return RunOutcome(False, {}, error=f"runner_failed(exit={p.returncode}):{err}", image=c.image)
        return RunOutcome(True, out["compile"], out["runs"], image=c.image)


# ---------- 支援問題の検証（仕様 §6-2） ----------
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{4,}|\(nil\)")


def output_has_address(text: str) -> bool:
    return bool(_ADDR_RE.search(text))


def src_prints_address(src: str) -> bool:
    return "%p" in src


@dataclass
class Verified:
    ok: bool
    reason: str                       # ok のとき ""、失敗時は理由コード
    outputs: dict[str, str] = field(default_factory=dict)   # stdin → 実行結果（正答の根拠）
    outcome: Optional[dict] = None


def verify_program(sb: DockerSandbox, src: str, stdins: list[str], repeats: int = 3) -> Verified:
    """-Wall -Wextra でコンパイル成功、サニタイザ無エラー、同一入力で repeats 回の出力完全一致、
    制限内終了、アドレス値を含まない、の全条件を満たすか。正答は実行結果そのもの。"""
    if src_prints_address(src):
        return Verified(False, "prints_address")
    expanded = [s for s in stdins for _ in range(repeats)]
    res = sb.run(src, expanded)
    if not res.ok:
        return Verified(False, "sandbox_error:" + res.error, outcome=res.to_dict())
    if not res.compile.get("ok"):
        return Verified(False, "compile_failed", outcome=res.to_dict())
    outputs: dict[str, str] = {}
    for r in res.runs:
        if r.get("timed_out"):
            return Verified(False, "timeout", outcome=res.to_dict())   # 壁時計・CPU 時間超過
        if r.get("sanitizer"):
            return Verified(False, "sanitizer_error", outcome=res.to_dict())
        if r.get("truncated"):
            return Verified(False, "output_too_large", outcome=res.to_dict())
        if r.get("exit_code") != 0:
            return Verified(False, "nonzero_exit", outcome=res.to_dict())
        if output_has_address(r.get("stdout", "")):
            return Verified(False, "output_has_address", outcome=res.to_dict())
        prev = outputs.get(r["stdin"])
        if prev is not None and prev != r["stdout"]:
            return Verified(False, "nondeterministic_output", outcome=res.to_dict())
        outputs[r["stdin"]] = r["stdout"]
    return Verified(True, "", outputs, res.to_dict())
