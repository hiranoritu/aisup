# HS×RU 判定エンジン（仕様 v1.1 準拠・開発版）

生成AI利用プログラミング学習支援システムのうち、**ログから状態を判定して介入を決める中心部分**。
Python 3.10 以上、標準ライブラリのみで動く（テストは pytest）。

```
docs/spec_v1.1.md        仕様書（正）
engine/
  events.py              イベントスキーマ（JSON Lines）
  config.py              規則・閾値（rule_version と config_hash を全判定に記録）
  clex.py                C の字句解析（壊れたコードでも例外を出さない）
  document.py            エディタ内容の再構成と文字ごとの由来追跡（typed / ai / external / unknown）
  codeblocks.py          AI 回答からの C コード抽出
  structure.py           S 特徴（Tree-sitter 版は未検証、開発用の近似版あり）
  hs.py                  LLM 特徴の検証と HS 規則
  ru.py                  D/S/C/T/E/I/Q と RU 規則（三値論理）
  policy.py              9状態→支援、一律群
  core.py                Engine：サイクル分割・判定窓・クールダウン・介入判断
  export.py              CSV/JSON 出力
prompts/prompt_features.md   質問特徴抽出プロンプト（草案）
tests/                   受け入れ条件（§10）と特徴量の単体テスト
examples/sample_events.jsonl
```

## 使い方

```bash
python -m pytest                                   # テスト
python -m engine examples/sample_events.jsonl --out out/   # ログ再生 → CSV/JSON
```

本番でもサーバは同じ `Engine` を参加者ごとに1つ持ち、保存したイベントを順に `ingest()` に渡す。
サーバ再起動時はログを先頭から再生して状態を復元する（`replay()`）。エンジンは LLM・サンドボックス・時計に触れないので、同じログと同じ設定からは必ず同じ判断が出る。

## サーバ・フロントとの約束（介入のタイミング）

```
学習者が送信
 → PROMPT_DRAFT_SUBMIT を保存
 → LLM で質問特徴を抽出 → PROMPT_FEATURES を保存（失敗時も ok:false で保存）
 → engine.ingest(PROMPT_FEATURES) が Decision を返す（確認質問なら None）
     timing == "pre_send"      : AI に送る前に質問の具体化（P-A）を出す → PROMPT_SENT
     timing == "post_response" : AI 回答を読み取り専用で表示 → 支援問題を生成・検証
                                  → 成功なら SUPPORT_SHOWN…SUPPORT_END、失敗なら SUPPORT_INFEASIBLE
     support_type == None      : そのまま送信・表示して編集可能にする
```

- `SUPPORT_SHOWN` が来た時点でそのサイクルは「介入あり」になり、判定窓から外れ、次の適格機会がクールダウンになる。`SUPPORT_INFEASIBLE` ではどちらも起きない。
- 支援中（`SUPPORT_SHOWN`〜`SUPPORT_END`）の RUN・AI_RESPONSE・編集は RU の計算に入らない。支援UI内の実行は `SUPPORT_RUN` で記録する。
- `CODE_EDIT` は `offset / delete_len / insert_text / kind` を必ず送る。AI 回答ペインからコピーした文字列の貼り付けは `kind:"paste", source:{type:"ai", response_id, block_id}`。`block_id` は回答内のコードフェンスの出現順（0始まり）で、`codeblocks.py` と同じ採番にする。
- `CODE_SNAPSHOT` を定期的に送る。再構成した内容と食い違えば、そのサイクルの由来系特徴（C・I・E）は Unknown になり、`anomalies` に記録される。

## 実装時に置いた解釈（仕様に明記がなく、予備実験で見直す点）

1. **規則の三値評価**：上位規則が「不明」のときは下位に進まず Unknown にする。例：E が不明なら Constructive の可能性を否定できないので、D≥0.10 でも Active にしない。Unknown 率が上がる方向の解釈。
2. **T**：（実行時のコード, 標準入力）の組の種類数。同じコード・同じ入力の再実行は1回。生の実行回数は `T_runs`。
3. **E ①**：h3=1 の質問（新規・確認とも）の後に編集があり、その後に実行があれば1。「仮説に対応する変更か」は見ていない。
4. **I の「新規作成」**：関数単位で対応付いた場合はサイクル開始時にその関数がなかったこと。断片の場合は対象範囲の80%以上がそのサイクル中に入力されたこと。
5. **C-A の追加条件**：D か S で Active、E=0 が確定、同じ判定範囲で I=1 が一度も観察されていない。課題ごとの「重要概念」はまだ使っていない（課題メタデータ待ち）。
6. **P-A の追加条件**：h2=0 かつ D か S で Active（確認質問 Q だけの Active や、改名だけの疑いがあるものは除く）。
7. **判定窓の範囲**：同じフェーズ（`main`）内のサイクル。課題をまたいでも窓は続く。`policy.window_scope: "task"` で課題内に限定できる。

閾値と上記の切り替えはすべて `config.py` にある。

## 未実装・未検証（次に Claude Code で作るもの）

- [ ] **Tree-sitter C**：`tree_sitter`, `tree_sitter_c` を導入し、`structure_analyzer: "tree_sitter_c"` に切り替えて `TreeSitterAnalyzer` をテストする。現在のテストは開発用近似 `lexer_approx` で通している。本実験では近似を使わない。
- [ ] **サンドボックス**：gcc（`-Wall -Wextra -fsanitize=address,undefined`）、ネットワーク遮断、CPU時間・メモリ・プロセス数・出力サイズ制限。
- [ ] **支援問題の生成と検証**（仕様 §6）：LLM 生成 → 3回実行で出力一致 → 正答は実行結果 → 失敗は1回再生成 → 実施不能。
- [ ] **Web 画面**：エディタ（打鍵・貼り付け・コピーのフック）、AI チャット、実行欄、支援オーバーレイ。
- [ ] **サーバ**：イベント保存（追記専用）、LLM 中継（API キーはサーバ）、参加者ごとの Engine 管理、管理画面。
- [ ] **質問特徴抽出**：`prompts/prompt_features.md` を使った LLM 呼び出しと人手ラベルとの一致確認。
- [ ] 仕様 §13 の未確定事項（新規/確認質問の判定方式、記述式のフィードバック方式、コピー可否、スキップ可否、モデル、実行環境）。
