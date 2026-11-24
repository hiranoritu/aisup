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
    for need in ("コードとヒントの両方", "エラー文だけ", "考え＋完成コード", "曖昧（確認か新規か）", "否定"):
        assert need in cats
    swap = next(e for t, e, _, _ in FEATURE_CASES if t == "swap関数のコードを全部書いてください")
    assert swap["h1"] == 1 and swap["h4"] == 0


def test_response_shapes():
    n = {k: len(extract_c_blocks("r", v)) for k, v in RESPONSE_SHAPES.items()}
    assert n["explanation_only"] == 0 and n["multi_block"] >= 3 and n["fragment_no_main"] == 1
    assert "main" not in RESPONSE_SHAPES["fragment_no_main"].split("```c")[1].split("```")[0]


def test_features_prompt_has_version_label_and_h4_definition():
    p = load_prompt("prompts/prompt_features.md")
    assert p.version.startswith("features-v2:")
    assert "h1=1, h4=0" in p.system and "以外" in p.system
