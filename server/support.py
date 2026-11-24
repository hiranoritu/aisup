"""支援問題の生成・検証・採点（仕様 §6, §7, §13-3）。

生成：LLM が問題案（JSON）を作る → 形式を検証 → 実行を伴う問題はサンドボックスで検証し、
      正答は LLM の予想ではなく実際の実行結果にする → 失敗したら1回だけ再生成 → それでも失敗なら実施不能。
各試行の LLM 出力・検証結果はすべて返し、サーバがイベントとして保存する。
選択肢の並びは乱数を使わず、opportunity_id と選択肢文字列のハッシュで決める（再現可能）。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from engine.codeblocks import extract_c_blocks

from .llm import LLMClient, LLMResult, parse_json_text
from .prompts import Prompt
from .sandbox import DockerSandbox, output_has_address, src_prints_address, verify_program
from .tasks import Task

LLM_SUPPORT_TYPES = ("decompose", "self_explain", "predict_verify", "trace_predict", "apply_variation")
NEEDS_AI_CODE = ("self_explain", "predict_verify", "trace_predict", "apply_variation")


class GenFail(Exception):
    def __init__(self, reason: str, detail: Any = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Attempt:
    llm: Optional[LLMResult]
    reason: str                      # "" なら成功
    verification: Optional[dict] = None
    elapsed_ms: int = 0


@dataclass
class GenResult:
    ok: bool
    reason: str
    public: dict[str, Any] = field(default_factory=dict)    # 学習者に送る
    key: dict[str, Any] = field(default_factory=dict)       # 正答・ヒント（サーバだけが持つ）
    attempts: list[Attempt] = field(default_factory=list)
    source_block: Optional[str] = None                      # 使った AI コード "response_id:block_id"


def numbered(code: str) -> str:
    return "\n".join(f"{i + 1:>3}| {line}" for i, line in enumerate(code.splitlines()))


def pick_ai_code(response_id: str, response_text: str) -> Optional[tuple[str, str]]:
    """AI 回答中の C コードのうち最も長いもの（block_id, code）。"""
    blocks = extract_c_blocks(response_id, response_text)
    if not blocks:
        return None
    b = max(blocks, key=lambda b: (len(b.code), -int(b.block_id)))
    return b.block_id, b.code


def stable_order(seed: str, items: list[str]) -> list[int]:
    return sorted(range(len(items)), key=lambda i: hashlib.sha256(f"{seed}\x00{items[i]}".encode()).hexdigest())


def norm_output(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").strip("\n").split("\n"))


class SupportGenerator:
    def __init__(self, llm: LLMClient, prompts: dict[str, Prompt], sandbox: DockerSandbox, cfg: dict) -> None:
        self.llm = llm
        self.prompts = prompts
        self.sb = sandbox
        self.cfg = cfg   # server_config

    # ------------------------------------------------------------------ 生成
    def generate(self, support_type: str, opportunity_id: str, task: Task,
                 response_id: Optional[str], response_text: str) -> GenResult:
        if support_type not in LLM_SUPPORT_TYPES:
            raise ValueError(support_type)
        code_pick = pick_ai_code(response_id or "", response_text) if response_text else None
        if support_type in NEEDS_AI_CODE and code_pick is None:
            return GenResult(False, "no_code_in_response")
        block_id, ai_code = code_pick if code_pick else (None, "")
        scfg = self.cfg["support"]
        tries = 1 + int(scfg["regenerate_on_failure"])
        attempts: list[Attempt] = []
        for _ in range(tries):
            t0 = time.monotonic()
            budget = float(scfg["generation_timeout_s"])
            res: Optional[LLMResult] = None
            try:
                msgs = self.prompts[support_type].messages(
                    task_title=task.title, task_description=task.description, concepts="、".join(task.concepts),
                    variation_seeds="／".join(task.variation_seeds) or "（なし）",
                    ai_code=numbered(ai_code) if ai_code else "（コードなし）")
                timeout = min(budget, float(self.cfg["llm"]["support"].get("timeout_s", budget)))
                res = self.llm.complete("support", msgs, json_mode=True, timeout_s=timeout)
                if not res.ok:
                    raise GenFail("llm_failed", res.error)
                try:
                    obj = parse_json_text(res.text)
                except ValueError:
                    raise GenFail("json_parse_error")
                if not isinstance(obj, dict):
                    raise GenFail("json_not_object")
                public, key, verification = getattr(self, "_build_" + support_type)(obj, opportunity_id, ai_code)
                elapsed = time.monotonic() - t0
                if elapsed > budget:
                    raise GenFail("generation_timeout", {"elapsed_s": round(elapsed, 2)})
                attempts.append(Attempt(res, "", verification, int(elapsed * 1000)))
                public["support_type"] = support_type
                return GenResult(True, "", public, key, attempts,
                                 f"{response_id}:{block_id}" if block_id is not None else None)
            except GenFail as e:
                ver = e.detail if isinstance(e.detail, dict) else ({"detail": e.detail} if e.detail else None)
                attempts.append(Attempt(res, e.reason, ver, int((time.monotonic() - t0) * 1000)))
        return GenResult(False, attempts[-1].reason, attempts=attempts)

    # 型ごとの検証。戻り値 (public, key, verification)
    def _build_decompose(self, o, opp, _code):
        q, choices, ai, hint = o.get("question"), o.get("choices"), o.get("answer_index"), o.get("hint", "")
        if not isinstance(q, str) or not q.strip():
            raise GenFail("invalid:question")
        if not isinstance(choices, list) or not 3 <= len(choices) <= 4 or not all(isinstance(c, str) and c.strip() for c in choices):
            raise GenFail("invalid:choices")
        if len(set(choices)) != len(choices):
            raise GenFail("invalid:duplicate_choices")
        if not isinstance(ai, int) or not 0 <= ai < len(choices):
            raise GenFail("invalid:answer_index")
        order = stable_order(opp, choices)
        shown = [choices[i] for i in order]
        return ({"kind": "choice", "question": q, "choices": shown},
                {"answer_index": order.index(ai), "hint": str(hint), "explanation": str(o.get("explanation", ""))},
                None)

    def _build_self_explain(self, o, _opp, code):
        lines, q, rubric = o.get("lines"), o.get("question"), o.get("rubric")
        n = len(code.splitlines())
        if not isinstance(lines, list) or not 1 <= len(lines) <= 3 or not all(isinstance(x, int) and 1 <= x <= n for x in lines):
            raise GenFail("invalid:lines")
        if not all(code.splitlines()[x - 1].strip() for x in lines):
            raise GenFail("invalid:blank_line_selected")
        if not isinstance(q, str) or not q.strip():
            raise GenFail("invalid:question")
        if not isinstance(rubric, list) or not 2 <= len(rubric) <= 3 or not all(isinstance(r, str) and r.strip() for r in rubric):
            raise GenFail("invalid:rubric")
        return ({"kind": "text", "question": q, "code": code, "highlight_lines": sorted(set(lines))},
                {"rubric": rubric, "model_answer": str(o.get("model_answer", "")), "hint": str(o.get("hint", ""))},
                None)

    def _build_predict(self, o, opp):
        prog, stdin, q, distractors = o.get("program"), o.get("stdin", ""), o.get("question"), o.get("distractors")
        if not isinstance(prog, str) or "main" not in prog:
            raise GenFail("invalid:program")
        if not isinstance(stdin, str):
            raise GenFail("invalid:stdin")
        if not isinstance(q, str) or not q.strip():
            raise GenFail("invalid:question")
        if not isinstance(distractors, list) or not all(isinstance(d, str) for d in distractors):
            raise GenFail("invalid:distractors")
        if src_prints_address(prog):
            raise GenFail("prints_address")
        v = verify_program(self.sb, prog, [stdin], int(self.cfg["support"]["verify_repeats"]))
        ver = {"ok": v.ok, "reason": v.reason, "outputs": v.outputs, "outcome": v.outcome}
        if not v.ok:
            raise GenFail("verify:" + v.reason, ver)
        actual = v.outputs[stdin]
        if not norm_output(actual):
            raise GenFail("empty_output", ver)
        if len(actual.splitlines()) > 5:
            raise GenFail("output_too_long", ver)
        # 実際の出力と一致する・重複する・アドレスを含む誤答は捨てる
        seen = {norm_output(actual)}
        wrong = []
        for d in distractors:
            nd = norm_output(d)
            if nd and nd not in seen and not output_has_address(d):
                seen.add(nd)
                wrong.append(nd)
        if len(wrong) < 2:
            raise GenFail("insufficient_distractors", ver)
        choices = [norm_output(actual)] + wrong[:3]
        order = stable_order(opp, choices)
        shown = [choices[i] for i in order]
        ver["llm_expected_output"] = o.get("expected_output")
        ver["llm_expected_matches_actual"] = norm_output(str(o.get("expected_output", ""))) == norm_output(actual)
        return ({"kind": "choice", "question": q, "code": prog, "stdin": stdin, "choices": shown},
                {"answer_index": order.index(0), "actual_output": actual, "hint": str(o.get("hint", ""))},
                ver)

    def _build_predict_verify(self, o, opp, _code):
        return self._build_predict(o, opp)

    def _build_trace_predict(self, o, opp, _code):
        return self._build_predict(o, opp)

    def _build_apply_variation(self, o, _opp, _code):
        task, starter, ref, tests = o.get("task"), o.get("starter_code"), o.get("reference_solution"), o.get("tests")
        if not isinstance(task, str) or not task.strip():
            raise GenFail("invalid:task")
        if not isinstance(starter, str) or not isinstance(ref, str) or "main" not in ref:
            raise GenFail("invalid:code")
        if not isinstance(tests, list) or not 1 <= len(tests) <= 3 or not all(isinstance(t, str) for t in tests):
            raise GenFail("invalid:tests")
        v = verify_program(self.sb, ref, list(dict.fromkeys(tests)), int(self.cfg["support"]["verify_repeats"]))
        ver = {"ok": v.ok, "reason": v.reason, "outputs": v.outputs, "outcome": v.outcome}
        if not v.ok:
            raise GenFail("verify:" + v.reason, ver)
        st = self.sb.run(starter, [])
        ver["starter_compile"] = st.compile
        if not st.ok or not st.compile.get("ok"):
            raise GenFail("starter_does_not_compile", ver)
        if all(norm_output(v.outputs[t]) == "" for t in v.outputs):
            raise GenFail("empty_output", ver)
        return ({"kind": "code", "question": task, "starter_code": starter, "tests": list(v.outputs)},
                {"expected": v.outputs, "reference_solution": ref, "hint": str(o.get("hint", ""))},
                ver)


# ---------------------------------------------------------------------- 質問の具体化（P-A）
def build_clarify(original_text: str, last_compile: Optional[dict], last_run: Optional[dict]) -> tuple[dict, dict]:
    """直近の COMPILE/RUN のエラーから選択肢を作る。LLM は使わない。原文は書き換えない。"""
    choices: list[str] = []
    if last_compile and not last_compile.get("ok"):
        for line in (last_compile.get("diagnostics") or "").splitlines():
            if "error:" in line:
                choices.append("コンパイルエラー: " + line.split("error:", 1)[1].strip())
            if len(choices) >= 2:
                break
    if last_run:
        if last_run.get("sanitizer"):
            choices.append("実行時エラー（メモリやポインタの誤り）が出た")
        elif last_run.get("timed_out") or last_run.get("limit"):
            choices.append("実行が終わらない・制限を超えた")
        elif last_run.get("exit_code") not in (0, None):
            choices.append(f"実行が異常終了した（終了コード {last_run.get('exit_code')}）")
    choices.append("期待した出力にならない")
    choices.append("エラーではなく、わからない点がある")
    public = {"kind": "clarify", "support_type": "clarify_question", "original_text": original_text,
              "choices": choices,
              "question": "今いちばん知りたいことを一文で書いてください。送る前に、何について困っているかを選んでください。"}
    return public, {}


# ---------------------------------------------------------------------- 採点
def grade_choice(key: dict, answer: Any) -> dict:
    ok = isinstance(answer, int) and answer == key["answer_index"]
    return {"correct": ok}


def grade_code(sb: DockerSandbox, key: dict, code: str) -> dict:
    stdins = list(key["expected"])
    res = sb.run(code, stdins)
    if not res.ok:
        return {"correct": None, "error": "sandbox_error:" + res.error}
    if not res.compile.get("ok"):
        return {"correct": False, "compile": res.compile, "results": []}
    results = []
    for r in res.runs:
        exp = key["expected"][r["stdin"]]
        results.append({"stdin": r["stdin"], "expected": exp, "actual": r.get("stdout", ""),
                        "pass": (not r.get("sanitizer") and not r.get("timed_out")
                                 and norm_output(r.get("stdout", "")) == norm_output(exp)),
                        "sanitizer": r.get("sanitizer"), "timed_out": r.get("timed_out")})
    return {"correct": all(x["pass"] for x in results), "compile": res.compile, "results": results}


def grade_text_llm(llm: LLMClient, prompt: Prompt, public: dict, key: dict, answer: str,
                   timeout_s: float) -> tuple[LLMResult, dict]:
    rubric = key["rubric"]
    msgs = prompt.messages(question=public["question"], code=public.get("code", ""),
                           rubric="\n".join(f"{i}: {r}" for i, r in enumerate(rubric)), answer=answer)
    res = llm.complete("grade", msgs, json_mode=True, timeout_s=timeout_s)
    if not res.ok:
        return res, {"correct": None, "error": "llm_failed"}
    try:
        obj = parse_json_text(res.text)
        crit = {int(c["index"]): int(c["met"]) for c in obj["criteria"]}
        if set(crit) != set(range(len(rubric))) or not set(crit.values()) <= {0, 1}:
            raise ValueError("criteria mismatch")
    except (ValueError, KeyError, TypeError) as e:
        return res, {"correct": None, "error": f"grade_parse_error:{e}"}
    met = [rubric[i] for i in range(len(rubric)) if crit[i] == 1]
    unmet = [rubric[i] for i in range(len(rubric)) if crit[i] == 0]
    return res, {"correct": not unmet, "met": met, "unmet": unmet, "feedback": str(obj.get("feedback", ""))}
