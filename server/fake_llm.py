"""開発・動作確認用の偽 LLM（llm_provider: "fake"）。本実験では使わない。

決定的な応答を返す。課題の参照解を「AI回答」として返すので、貼り付け・実行・支援の流れを API キーなしで試せる。
記録上は model="fake" になり、本番ログと区別できる。
"""
from __future__ import annotations

import json
import re

from .llm import FuncLLM
from .tasks import Task


def _task_for(messages, tasks: list[Task]) -> Task:
    blob = "\n".join(m["content"] for m in messages)
    for t in tasks:
        if t.title in blob:
            return t
    return tasks[0]


def _features(text: str) -> dict:
    def hit(*words):
        return [w for w in words if w in text]
    spans = {}
    h1 = hit("書いて", "コードをください", "答えを", "全部")
    h2 = hit("エラー", "行目", "出力", "動かない", "warning", "error", "変わらない")
    h3 = hit("思う", "考え", "のでは", "だから")
    h4 = hit("ヒント", "だけ教えて", "説明して")
    for k, v in (("h1", h1), ("h2", h2), ("h3", h3), ("h4", h4)):
        if v:
            spans[k] = v
    follow = hit("さっき", "この回答", "今のコード", "なぜそう", "本当に")
    return {"h1": int(bool(h1)), "h2": int(bool(h2)), "h3": int(bool(h3)), "h4": int(bool(h4)),
            "is_followup": int(bool(follow)), "followup_confidence": 0.9,
            "asks_about_answer": int(bool(hit("なぜそう", "本当に", "正しい"))), "off_task": 0, "spans": spans}


def make_fake_llm(tasks: list[Task]) -> FuncLLM:
    def fn(role, messages):
        sysmsg = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = messages[-1]["content"]
        task = _task_for(messages, tasks)
        if role == "chat":
            return ("（開発用の偽AIの応答です）次のように書けます。\n\n```c\n" + task.reference_solution.rstrip()
                    + "\n```\n\nポインタを使うと、関数の中から呼び出し元の変数を書き換えられます。")
        if role in ("features", "features_shadow"):
            m = re.search(r"学習者の質問: (.*)$", user, re.S)
            return json.dumps(_features(m.group(1) if m else user), ensure_ascii=False)
        if role == "grade":
            n = len(re.findall(r"^\d+: ", user, re.M))
            ans = user.split("学習者の回答:", 1)[-1].strip()
            met = 1 if len(ans) >= 10 else 0
            return json.dumps({"criteria": [{"index": i, "met": met, "evidence": ans[:20]} for i in range(n)],
                               "feedback": "" if met else "どの変数のアドレスが渡されているかに注目してみましょう。"},
                              ensure_ascii=False)
        if role == "support":
            stdin = task.sample_inputs[0] if task.sample_inputs else ""
            code_lines = re.findall(r"^\s*\d+\| (.*)$", user, re.M)
            if "選択問題を作ります" in sysmsg:
                return json.dumps({"question": "この課題を解く手順として正しいものはどれですか。",
                                   "choices": ["入力を読む → 関数で処理する → 結果を表示する",
                                               "結果を表示する → 入力を読む → 関数で処理する",
                                               "関数で処理する → 結果を表示する",
                                               "ファイルを開く → 入力を読む → 結果を表示する"],
                                   "answer_index": 0, "hint": "main の中で最初に何をしているかを見ましょう。",
                                   "explanation": "値を読み込んでから処理し、最後に表示します。"}, ensure_ascii=False)
            if "役割を説明させる" in sysmsg:
                idx = [i + 1 for i, line in enumerate(code_lines) if "*" in line and "/*" not in line][:2] or [1]
                return json.dumps({"lines": idx, "question": "強調した行が何をしているか説明してください。",
                                   "rubric": ["ポインタが指す先の値を読み書きしていることに触れている",
                                              "呼び出し元の変数が変わることに触れている"],
                                   "model_answer": "*a は a が指す先の変数を表し、呼び出し元の変数を書き換える。",
                                   "hint": "* が付いた変数は何を表すでしょうか。"}, ensure_ascii=False)
            if "条件変更の課題" in sysmsg:
                return json.dumps({"task": "（開発用）参照解と同じ動作になるようにプログラムを完成させてください。",
                                   "starter_code": task.starter_code, "reference_solution": task.reference_solution,
                                   "tests": task.sample_inputs[:2] or [""],
                                   "hint": "課題文の出力例をよく見ましょう。"}, ensure_ascii=False)
            return json.dumps({"program": task.reference_solution, "stdin": stdin,
                               "question": f"入力 {stdin.strip()!r} を与えて実行すると何が出力されますか。",
                               "expected_output": "", "distractors": ["0", "1 2", "3 5", "-1"],
                               "hint": "関数の中で * を付けて代入している行に注目しましょう。"}, ensure_ascii=False)
        raise ValueError(f"unknown role {role}")
    return FuncLLM(fn, "fake")
