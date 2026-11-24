# 生成AI利用 C プログラミング学習支援システム（仕様 v1.1 準拠・開発版）

ログから HS×RU の状態を判定して介入を決める**判定エンジン**（`engine/`）と、学習環境（エディタ・AIチャット・実行・支援UI）を提供する**サーバと画面**（`server/`, `web/`）。
Python 3.10 以上。依存は `tree-sitter` / `tree-sitter-c`（S 特徴）と pytest のみで、版は `requirements.txt` で固定している。

```
docs/spec_v1.1.md        仕様書（正）
engine/
  events.py              イベントスキーマ（JSON Lines）
  config.py              規則・閾値（rule_version と config_hash を全判定に記録）
  clex.py                C の字句解析（壊れたコードでも例外を出さない）
  document.py            エディタ内容の再構成と文字ごとの由来追跡（typed / ai / external / unknown）
  codeblocks.py          AI 回答からの C コード抽出
  structure.py           S 特徴（本番は Tree-sitter C、開発用の近似版 lexer_approx もあり）
  hs.py                  LLM 特徴の検証と HS 規則
  ru.py                  D/S/C/T/E/I/Q と RU 規則（三値論理）
  policy.py              9状態→支援、一律群
  core.py                Engine：サイクル分割・判定窓・クールダウン・介入判断
  export.py              CSV/JSON 出力
server/                  サーバ（Python 標準ライブラリのみ）
  app.py                 HTTP・管理機能・エクスポート（python -m server）
  session.py             参加者ごとのセッション：イベント保存→エンジン投入→介入フロー・支援UI
  store.py               追記専用イベントストア（SQLite。UPDATE/DELETE はトリガで拒否）
  llm.py / fake_llm.py   OpenAI Chat Completions 呼び出し／開発用の偽 LLM
  features.py            質問特徴の抽出（PROMPT_FEATURES）
  support.py             支援問題の生成・サンドボックス検証・採点（§6, §7, §13-3）
  sandbox.py             Docker サンドボックスの呼び出しと §6-2 の検証
  settings.py            サーバ設定（LLM・支援UI・サンドボックス。ハッシュを SESSION_START に記録）
  tasks.py / render.py   課題メタデータ／AI 回答のコードブロック分割（block_id は engine と同じ採番）
sandbox/                 実行用 Docker イメージ（Dockerfile, runner.py）
web/                     学習者画面（index.html, app.js）と管理画面（admin.html, admin.js）
tasks/                   課題メタデータ（**仮の課題**。placeholder: true）
prompts/                 学習者用AI・特徴抽出・支援生成・記述採点のプロンプト（版＝内容のハッシュを記録）
tests/                   受け入れ条件（§10）、特徴量、サーバ、実サンドボックスのテスト
examples/sample_events.jsonl
```

## 使い方

```bash
pip install -r requirements.txt
docker build -t aisup-sandbox:1 sandbox/                 # 実行サンドボックス（初回・runner.py 変更時）
python -m pytest                                          # テスト（Docker がなければ実サンドボックスのテストは skip）
python -m engine examples/sample_events.jsonl --out out/  # ログ再生 → CSV/JSON
```

### サーバの起動

```bash
cp server_config.example.json server_config.json   # モデル名を記入（§13-6）
export OPENAI_API_KEY=...   ADMIN_TOKEN=...         # 秘密情報は環境変数だけに置く
python -m server --config server_config.json --data data/ --host 0.0.0.0 --port 8000
```

- 管理画面 `http://<host>:8000/admin`：参加者登録（群は指定または無作為）、学習者URLのコピー、課題の開始・切替・終了（`task_sequence` の順）、判定の確認、ログと判定結果の zip ダウンロード。
- 学習者画面 `http://<host>:8000/?t=<トークン>`。
- API キーなしで動作確認するときは `{"llm_provider": "fake"}` の設定で起動する（偽 LLM は課題の参照解を返す。記録上 model="fake"）。
- データは `data/events.sqlite3`（追記専用）。再起動すると保存済みイベントを再生して全参加者の状態を復元する。エクスポートの判定結果も保存ログの再生から作る（LLM は呼ばない）。
- LAN 内の http（localhost 以外）ではブラウザのクリップボード API が使えないため、「コピー」ボタンは代替方式で動く。可能なら https で配信する。

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

### サーバ・画面で置いた解釈（予備実験で見直す点）

8. **支援生成の時間上限**（§6-4）：15秒は**1回の生成ごと**（LLM＋検証）。再生成した場合、学習者の待ち時間は最大で約2倍になる。
9. **記述式の正誤**（§13-3 案A）：観点をすべて満たしたら正解。終了時は満たした／足りない観点とフィードバックを見せ、模範解答は見せない（案B `self_assess` では観点と模範解答を見せて自己評価）。
10. **P-A**：困っている点の選択と一文は記録するが、AI に追記するのは**一文だけ**（選択肢は送らない）。P-A は採点しない。時間切れは原文のまま送る（`SUPPORT_END reason=timeout`）。
11. **終了時のフィードバック**：動作予測は実際の実行結果と正解の選択肢、問題の分解は正解の選択肢と解説、自力応用は入力ごとの期待出力との比較。
12. **AI 応答を待つ間**はエディタ・実行をロックする（送信からAI回答表示まで）。
13. **学習者用AIの文脈**：システムプロンプトに課題文を入れ、会話履歴は課題ごと（最大20件）。エディタのコードは自動では渡さない。
14. **貼り付けの由来判定**：改行コード（CRLF/LF）と末尾の空白の違いは無視して、直前の COPY_FROM_AI と一致を判定する。由来はサーバが決め、画面からの申告は使わない。
15. **サンドボックス**：LeakSanitizer は無効（コンテナ内で ptrace を使えないため。リークは検証対象外）。CPU 2秒・壁時計3秒・メモリ256MB・プロセス64・出力64KB・ネットワークなし・読み取り専用（/tmp のみ書込可）。
16. **選択肢の並び**は乱数を使わず opportunity_id と選択肢文字列のハッシュで決める（再現可能）。
17. 学習者画面で編集を差分にするとき、打鍵と貼り付けは選択範囲とデータから正確に作る。それ以外（取り消し・IME など）は前後一致の差分で代用するので、同じ文字が並ぶ箇所では位置が1文字ずれることがある（内容の再構成は常に一致）。

## 未実装・未検証（次に Claude Code で作るもの）

- [x] **サンドボックス**：Docker（`sandbox/`）。`tests/test_sandbox.py` で制限と §6-2 の検証を確認済み。
- [x] **支援問題の生成と検証**（仕様 §6）：`server/support.py`。生成プロンプトは `prompts/support/`（**未検証の草案**）。
- [x] **サーバ**：イベント保存（追記専用）、OpenAI 中継、参加者ごとの Engine、管理画面、エクスポート。
- [x] **Web 画面**：エディタ（打鍵・貼り付け・コピーのフック）、AI チャット、実行欄、支援オーバーレイ。**実ブラウザでの動作確認はまだ**（API とサーバ側の流れはテスト済み）。
- [ ] **実ブラウザでの確認**：日本語入力（IME）、取り消し、コピー→貼り付けの由来、支援オーバーレイの各型、タイマー、課題切替。
- [ ] **OpenAI 実接続**：モデル名の決定（§13-6）と、特徴抽出・支援生成・採点プロンプトの試行。
- [ ] **質問特徴抽出の人手ラベルとの一致確認**（`prompts/prompt_features.md`）。
- [ ] **課題の差し替え**：`tasks/*.json` は仮。重要概念・変形の種・参照解を本番の課題にする。
- [ ] §13-1 案B（スレッド返信UIで新規／確認を決める方式）は未実装（エンジン側の切替が必要）。
- [ ] 本実験前の凍結：`engine/config.py` の `rule_version`、`server_config` のハッシュ、プロンプトの版、サンドボックスイメージを記録して固定する。
- [ ] Tree-sitter 版 S の未決事項（上記）。
