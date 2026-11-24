"""check_llm の補助（API キーを伏せる・例文の網羅・プロンプトの版）。実際の API は呼ばない。"""
from server.check_llm import FEATURE_CASES, RESPONSE_SHAPES, scrub
from server.prompts import load_prompt
from engine.codeblocks import extract_c_blocks


def test_scrub_hides_api_key_everywhere():
    key = "sk-proj-ABCdef1234567890xyz"
    rep = {"error": f"Incorrect API key provided: {key}", "rows": [{"t": "x sk-other_KEY_123456 y"}], "n": 3}
    out = scrub(rep, key)
    blob = str(out)
    assert key not in blob and "sk-other_KEY_123456" not in blob and out["n"] == 3


def test_feature_cases_cover_boundaries():
    cats = {c for _, _, _, c in FEATURE_CASES}
    assert len(FEATURE_CASES) >= 20
    for need in ("コードとヒントの両方", "エラー文だけ", "考え＋完成コード", "回答の適用範囲", "否定"):
        assert any(c.startswith(need) for c in cats)
    swap = next(e for t, e, _, _ in FEATURE_CASES if t == "swap関数のコードを全部書いてください")
    assert swap["h1"] == 1 and swap["h4"] == 0


def test_response_shapes():
    n = {k: len(extract_c_blocks("r", v)) for k, v in RESPONSE_SHAPES.items()}
    assert n["explanation_only"] == 0 and n["multi_block"] >= 3 and n["fragment_no_main"] == 1
    assert "main" not in RESPONSE_SHAPES["fragment_no_main"].split("```c")[1].split("```")[0]


def test_features_prompt_has_version_label_and_h4_definition():
    p = load_prompt("prompts/prompt_features.md")
    assert p.version.startswith("features-v5:")
    assert "具体的な一点" in p.system and "どこから考えればいいですか」→ h2=0" in p.system
    assert "「ポインタってなに？」→ h2=0" in p.system and "「ポインタの * と & は何が違うの？」→ h2=1" in p.system
    assert "適用範囲" in p.system and "「別の答えを出して」" in p.system
    assert "h1=1, h4=0" in p.system and "以外" in p.system


def test_support_burst_runs_concurrently_with_fakes():
    import json as _json
    from server.llm import FuncLLM
    from server.prompts import load_prompt
    from server.settings import make_server_config
    from server.support import SupportGenerator
    from server.support_load import burst
    from server.tasks import load_task
    from server_helpers import PROG_A, FakeSandbox
    cfg = make_server_config({"llm_provider": "fake"})
    sup = _json.dumps({"program": PROG_A, "stdin": "", "question": "出力は？", "expected_output": "3",
                       "distractors": ["0", "1", "&a"], "hint": "h"})
    llm = FuncLLM(lambda role, msgs: sup, "scripted")
    prompts = {"trace_predict": load_prompt("prompts/support/trace_predict.md")}
    gen = SupportGenerator(llm, prompts, FakeSandbox({"": "3\n"}), cfg)
    rows = burst(gen, load_task("tasks/main_swap.json"), ["trace_predict"], 6, "t")
    assert len(rows) == 6 and all(r["ok"] for r in rows) and all(r["elapsed_s"] >= 0 for r in rows)
