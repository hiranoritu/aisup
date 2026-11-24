"""RQ1 の人手ラベル付け用の資料（server/labeling.py）。"""
import csv
import json

import pytest

from server.labeling import NA, cohen_kappa, fleiss_kappa, macro_f1, main, make, score
from engine import make_config

from server_helpers import PROG_A, F, FakeSandbox, Script, make_app, start
from test_server_v12 import paste_items, raw


def _run_participant(app, script, group, tag):
    s = start(app, group)
    for k in range(4):
        q = f"{tag}-q{k}"
        script.features[q] = F(h3=1) if k % 2 else F(h2=1)
        s.check_synced(s.next_seq - 1)
        r = s.submit_prompt(q)
        if (r.get("state") or {}).get("support"):          # 支援が出たら閉じる（打ち切りの確認用）
            sup = r["state"]["support"]
            s.support_end(sup["opportunity_id"], "skipped")
            if sup["support_type"] == "clarify_question":
                s.clarify_submit(sup["opportunity_id"], None, "", "skipped")
        base = s.next_seq
        s.client_events(paste_items(s, base) + [raw(s, base + 2, "CODE_EDIT", {
            "offset": 0, "delete_len": 0, "insert_text": "// 自分で追記\n", "kind": "typed"})], s.page_id)
        s.run_code(s.engine.doc.text, f"{k} 5\n")
    s.force_next_phase()
    return s


@pytest.fixture()
def exported(tmp_path):
    script = Script()
    script.support = lambda sysmsg, msgs: json.dumps({
        "program": PROG_A, "stdin": "", "question": "何が出力されますか", "expected_output": "3",
        "distractors": ["0", "1", "&a"], "hint": "p は何を指す？"})
    app = make_app(tmp_path / "data", script, sandbox=FakeSandbox({"": "3\n"}))
    sa = _run_participant(app, script, "adaptive", "a")
    su = _run_participant(app, script, "uniform", "u")
    lt = app.create_participant(None, "adaptive", note="loadtest")
    zpath = tmp_path / "export.zip"
    zpath.write_bytes(app.export_zip())
    return zpath, (sa, su), lt["participant_id"]


def test_make_hides_identity_and_system_output(tmp_path, exported):
    zpath, (sa, su), lt_pid = exported
    out = tmp_path / "lab"
    info = make(str(zpath), str(out), 7, ["main"], make_config(), "tasks")
    assert info["items"] >= 6 and info["participants"] == 2
    rdir = out / "for_raters"
    texts = [p.read_text(encoding="utf-8") for p in (rdir / "items").iterdir()]
    assert len(texts) == info["items"]
    blob = "\n".join(texts)
    index = (rdir / "index.html").read_text(encoding="utf-8")
    for secret in (sa.pid, su.pid, "adaptive", "uniform", "Passive", "Pending", "cycle_", "opportunity"):
        assert secret not in blob                       # 各項目にシステムの判定・群・ID を出さない
    for secret in (sa.pid, su.pid, "adaptive", "uniform", "Pending", "cycle_"):
        assert secret not in index
    assert all("質問" in t and "AI 回答" in t for t in texts)
    assert sum("<span class='k'>実行</span>" in t for t in texts) >= 4
    # 各項目には自分の質問だけが出る（次の質問は出ない）
    assert sum("a-q1" in t for t in texts) == 1
    with open(out / "_key" / "key.csv", encoding="utf-8-sig") as f:
        key = list(csv.DictReader(f))
    assert {r["participant_id"] for r in key} == {sa.pid, su.pid}       # 負荷試験の参加者は除く
    assert all(r["phase"] == "main" and r["rule_version"] == make_config()["rule_version"] for r in key)
    assert {r["group"] for r in key} == {"adaptive", "uniform"}
    # 支援が出たサイクルは提示の時点で打ち切り、印を付ける
    trunc = [r for r in key if r["truncated_at_support"] == "True"]
    assert all(r["intervened"] == "True" for r in trunc)
    sheet = (rdir / "label_sheet.csv").read_text(encoding="utf-8-sig").splitlines()
    assert sheet[0] == "item_id,HS,RU,note" and len(sheet) == info["items"] + 1


def test_truncated_item_does_not_show_actions_after_support(tmp_path, exported):
    zpath, _, _ = exported
    out = tmp_path / "lab"
    make(str(zpath), str(out), 7, ["main"], make_config(), "tasks")
    with open(out / "_key" / "key.csv", encoding="utf-8-sig") as f:
        key = list(csv.DictReader(f))
    trunc = [r for r in key if r["truncated_at_support"] == "True"]
    assert trunc                                   # 一律群の3問目以降は動作予測の支援が出る
    t = (out / "for_raters" / "items" / f"{trunc[0]['item_id']}.html").read_text(encoding="utf-8")
    assert "評価の対象外" in t and "<span class='k'>実行</span>" not in t


def test_order_is_seeded(tmp_path, exported):
    zpath, _, _ = exported
    keys = []
    for k, seed in enumerate((1, 1, 2)):
        out = tmp_path / f"lab{k}"
        make(str(zpath), str(out), seed, ["main"], make_config(), "tasks")
        keys.append((out / "_key" / "key.csv").read_text(encoding="utf-8-sig"))
    assert keys[0] == keys[1] and keys[0] != keys[2]


def test_cli_refuses_to_overwrite(tmp_path, exported):
    zpath, _, _ = exported
    out = tmp_path / "lab"
    main(["make", "--zip", str(zpath), "--out", str(out), "--seed", "3"])
    with pytest.raises(SystemExit):
        main(["make", "--zip", str(zpath), "--out", str(out), "--seed", "3"])


def _write(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "HS", "RU", "note"])
        w.writerows(rows)


def test_score_reports_agreement_and_f1(tmp_path, exported):
    zpath, _, _ = exported
    out = tmp_path / "lab"
    make(str(zpath), str(out), 7, ["main"], make_config(), "tasks")
    with open(out / "_key" / "key.csv", encoding="utf-8-sig") as f:
        key = list(csv.DictReader(f))
    # 評価者 A はシステムの判定と同じ（Unknown は判断不能）、B は1件だけ HS を変える
    def lab(v):
        return v if v in ("Passive", "Active", "Constructive") else NA
    a = [[r["item_id"], lab(r["sys_hs"]), lab(r["sys_ru"]), ""] for r in key]
    b = [list(x) for x in a]
    b[0][1] = "Constructive" if b[0][1] != "Constructive" else "Active"
    _write(tmp_path / "rater_a.csv", a)
    _write(tmp_path / "rater_b.csv", b)
    res = score(str(out), [str(tmp_path / "rater_a.csv"), str(tmp_path / "rater_b.csv")])
    assert res["raters"] == ["rater_a", "rater_b"]
    assert res["inter_rater"]["HS"]["percent_agreement"] == pytest.approx((len(key) - 1) / len(key))
    assert res["inter_rater"]["RU"]["percent_agreement"] == 1.0
    assert res["gold"] == "unanimous_only" and res["HS_all"]["n"] <= len(key) - 1
    assert res["HS_all"]["accuracy"] == 1.0                 # 全員一致の項目はシステムと同じ
    # 合議ファイルを与えるとそれを正解にする
    res2 = score(str(out), [str(tmp_path / "rater_a.csv")], gold_path=str(tmp_path / "rater_b.csv"))
    assert res2["gold"] == "consensus_file" and res2["HS_all"]["accuracy"] < 1.0
    assert res2["decision_latency_s"]["n"] >= 1


def test_score_rejects_unknown_label(tmp_path, exported):
    zpath, _, _ = exported
    out = tmp_path / "lab"
    make(str(zpath), str(out), 7, ["main"], make_config(), "tasks")
    _write(tmp_path / "bad.csv", [["I001", "passive", "", ""]])
    with pytest.raises(ValueError):
        score(str(out), [str(tmp_path / "bad.csv")])


def test_statistics():
    assert cohen_kappa(list("aabb"), list("aabb")) == 1.0
    assert cohen_kappa(list("ab"), list("ba")) == pytest.approx(-1.0)
    assert fleiss_kappa([["a", "a"], ["b", "b"]], "ab") == 1.0
    assert macro_f1(["a", "b"], ["a", "a"], "ab") == pytest.approx((2 / 3 + 0) / 2)


def test_disagree_then_score_with_consensus(tmp_path, exported):
    zpath, _, _ = exported
    out = tmp_path / "lab"
    make(str(zpath), str(out), 7, ["main"], make_config(), "tasks")
    with open(out / "_key" / "key.csv", encoding="utf-8-sig") as f:
        key = list(csv.DictReader(f))
    ids = [r["item_id"] for r in key]
    a = [[i, "Active", "Passive", ""] for i in ids]
    b = [list(x) for x in a]
    b[0][1], b[0][3] = "Constructive", "仮説がある"
    b[1][2] = "判定不能"                                   # 日本語の別名も受け付ける
    _write(tmp_path / "rater_a.csv", a)
    _write(tmp_path / "rater_b.csv", b)
    res = main(["disagree", "--dir", str(out), "--labels", str(tmp_path / "rater_a.csv"), str(tmp_path / "rater_b.csv")])
    cdir = out / "consensus"
    html_ = (cdir / "disagreements.html").read_text(encoding="utf-8")
    assert ids[0] in html_ and ids[1] in html_ and ids[2] not in html_ and "仮説がある" in html_
    with open(cdir / "disagreements.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert [r["item_id"] for r in rows] == ids[:2] and rows[1]["RU_rater_b"] == NA
    with open(cdir / "gold_draft.csv", encoding="utf-8-sig") as f:
        draft = list(csv.reader(f))
    assert draft[1] == [ids[0], "", "Passive", ""] and draft[2] == [ids[1], "Active", "", ""]
    assert draft[3] == [ids[2], "Active", "Passive", ""]
    ir = json.loads((cdir / "inter_rater_before_consensus.json").read_text(encoding="utf-8"))
    assert ir["disagreements"] == 2 and ir["inter_rater"]["HS"]["cohen_kappa"] is not None
    # 合議で空欄を埋めて正解として渡す
    draft[1][1], draft[2][2] = "Constructive", NA
    with open(tmp_path / "gold.csv", "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(draft)
    sc = score(str(out), [str(tmp_path / "rater_a.csv"), str(tmp_path / "rater_b.csv")], str(tmp_path / "gold.csv"))
    assert sc["gold"] == "consensus_file" and sc["gold_missing"] == {"HS": 0, "RU": 0}
    assert sc["RU_all"]["n_gold_undeterminable"] == 1 and sc["RU_all"]["n"] == len(ids) - 1
    assert "RU_not_truncated" in sc and sc["truncated_items"] >= 1
    assert sc["RU_not_truncated"]["n"] <= sc["RU_all"]["n"] - sc["truncated_items"] + 1


def test_guide_and_sheet_offer_undeterminable(tmp_path, exported):
    zpath, _, _ = exported
    out = tmp_path / "lab"
    make(str(zpath), str(out), 7, ["main"], make_config(), "tasks")
    guide = (out / "for_raters" / "guide.md").read_text(encoding="utf-8")
    assert "Undeterminable" in guide and "判定不能" in guide and "合議" in guide and "κ" in guide
    assert "Undeterminable" in (out / "for_raters" / "index.html").read_text(encoding="utf-8")
