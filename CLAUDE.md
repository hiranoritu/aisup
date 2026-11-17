# 開発メモ（Claude Code 用）

- 仕様の正は `docs/spec_v1.1.md`。仕様と実装が食い違ったら、勝手に直さず質問する。
- 研究用の判定ロジックなので、**閾値・規則・状態の定義を変える変更は必ず `config.py` の値か `rule_version` の変更として行い**、黙ってコード内に埋め込まない。
- Unknown を Passive や 0 に置き換えない。欠損は None のまま運び、理由を `unknown_reasons` に残す。
- `engine/` は LLM・ネットワーク・時計・乱数に依存させない（再生可能性）。外部呼び出しの結果はイベントとして保存してから渡す。
- 変更したら `python -m pytest` を通す。受け入れ条件は `tests/test_acceptance.py`。
- 次の作業と既知の解釈は `README.md` の「実装時に置いた解釈」「未実装・未検証」を参照。
