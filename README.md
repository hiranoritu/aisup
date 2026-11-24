# 生成AI利用 C プログラミング学習支援システム（仕様 v1.2 準拠・開発版）

ログから HS×RU の状態を判定して介入を決める**判定エンジン**（`engine/`）と、遠隔・完全非同期で実験を行う**学習環境**（`server/`, `web/`）。
仕様の正は `docs/spec_v1.2.md`（v1.1 は履歴）。Python 3.10 以上。サーバは標準ライブラリのみで、依存は `tree-sitter` / `tree-sitter-c`（S 特徴）と pytest（版は `requirements.txt` で固定）。

```
docs/spec_v1.2.md        仕様書（正）
engine/                  判定エンジン（LLM・ネットワーク・時計・乱数に依存しない）
  config.py              規則・閾値（rule_version と config_hash を全判定に記録）
  core.py                Engine：サイクル分割・判定窓・クールダウン・介入判断・v1.2 の補助特徴
  ru.py / hs.py / policy.py / structure.py / document.py / clex.py / codeblocks.py / export.py / events.py
server/                  サーバ（標準ライブラリのみ）
  app.py                 HTTP・管理・エクスポート・バックアップ・定期処理（python -m server）
  session.py             参加者ごと：連番つき受信→イベント保存→エンジン投入、実験の進行、課題の提出、支援
  store.py               追記専用イベントストア（SQLite WAL。UPDATE/DELETE はトリガで拒否）
  sandbox.py             Docker サンドボックス（同時実行数の待ち行列）と §6-2 の検証
  support.py             支援問題の生成・検証・採点（§6, §7, §13-3）
  llm.py / fake_llm.py   OpenAI Chat Completions／開発用の偽 LLM
  features.py / prompts.py / render.py / tasks.py / settings.py
  allocation.py          ブロック無作為化の割付表（python -m server.allocation）
  check_llm.py           OpenAI 実接続の確認（python -m server.check_llm）
  loadtest.py            負荷試験（python -m server.loadtest）
  support_load.py        支援生成の同時実行試験（python -m server.support_load --n 10）
  labeling.py            RQ1 の人手ラベル付け用の資料と集計（python -m server.labeling make / score）
web/                     学習者画面（index.html, net.js, phases.js, app.js, highlight.js, materials.js）・管理画面
sandbox/                 実行用 Docker イメージ（gcc＋ASan/UBSan）
tasks/                   課題（導入1・主課題3・後続課題2）。採点用の公開・非公開入力つき
materials/               教材（Markdown。画面上部の教材バー）
instruments/             同意書・事前/事後テスト・質問紙（**見本**。研究者が差し替える）
prompts/                 学習者用AI・特徴抽出・支援生成・記述採点のプロンプト（版＝内容のハッシュを記録）
Dockerfile, docker-compose.yml, deploy/   本番構成（HTTPS・構築スクリプト・バックアップ）
tests/                   受け入れ条件（§10）、エンジン、サーバ、実サンドボックスのテスト
```

## 開発

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
docker build -t aisup-sandbox:1 sandbox/            # 実行サンドボックス（初回・runner.py 変更時）
.venv/bin/python -m pytest                          # Docker がなければ実サンドボックスのテストは skip
python -m engine examples/sample_events.jsonl --out out/   # ログ再生 → CSV/JSON
```

手元での動作確認（API キー不要。偽 LLM は課題の参照解を返し、記録上 model="fake"）：

```bash
python -m server.allocation --blocks 3 --seed 1 --out /tmp/aisup/allocation.csv
echo '{"llm_provider":"fake","allocation_file":"/tmp/aisup/allocation.csv"}' > /tmp/aisup/dev.json
ADMIN_TOKEN=devadmin123 python -m server --config /tmp/aisup/dev.json --data /tmp/aisup/data --port 8765
# 管理画面 http://127.0.0.1:8765/admin で参加コードを発行し、表示された URL を開く
```

OpenAI の確認（モデル変更時も実行）：`OPENAI_API_KEY=... python -m server.check_llm`

## 実験の流れ（§15.1）と設定

`server_config.json`（コミット対象。研究で固定する設定。秘密情報は入れない）の `phases` の順に自動で進む：
同意 → 環境確認 → 練習（intro）→ 事前テスト → 主課題（支援あり）→ 後続課題（支援停止）→ 事後テスト → アンケート → 終了。

- フェーズの時間上限は実時間（中断中も減る）。上限で自動的に次へ（`PHASE_END reason=timeout`）。
  練習10分・事前テスト10分・主課題45分・後続課題25分・事後テスト10分・アンケート10分（2026-11-24 決定。予備実験の後に固定する。§15.3）。
- 課題フェーズの課題は順番に進み、**「提出して採点」で合格するまで次の課題へ進めない**。課題ごとの時間上限はない。フェーズの時間上限が来たら未完了でも次のフェーズへ（全員が後続課題と事後テストに到達する）。
- 採点は課題の公開入力（入出力例）と非公開入力で行い、期待出力は参照解をサンドボックスで実行した結果。非公開入力の内容は画面に出さない。
- 最初に開いてから24時間で無効。30分を超える中断は記録したうえで再開を認める（§13-10）。
- 学習者用AI：`gpt-6.2-sol`、特徴抽出：`gpt-6-luna`、支援生成・記述採点：`gpt-6.1-terra`（§13-6）。日付付きの版がないため応答の `model` を毎回記録。
  予備実験では特徴抽出を `gpt-6.1-terra` でも裏で行い `PROMPT_FEATURES_SHADOW` に記録する（`llm.features_shadow`。判定には使わず、費用は学習者の利用上限に数えない）。
  書き出しの `summary/support_by_group.csv`（群×支援の種類ごとに選ばれた・提示できた・実施不能の件数）と `summary/support_infeasible_by_group.csv`（群×実施不能の理由×種類の件数）。負荷試験の参加者は除く。
  書き出しの `summary/features_compare.csv` に質問ごとの両モデルの特徴を並べる。予備実験後に人手ラベルと両モデルを比べて本実験のモデルを確定し、本実験では `features_shadow` を外す（2026-11-24 決定）。
- 参加者ごとの LLM 上限（`llm_limits`）：質問80回か約$2で実験者に通知、質問240回か約$6で学習者用 AI への質問を止める
  （画面に `block_message` を表示し、`LIMIT_REACHED action=block` を記録して実験者にも通知。課題・実行・提出は続けられる）。
  費用は応答の `usage` と `llm_prices`（**料金は OpenAI の公式の価格表で確認してから使う**）から見積もる。表にないモデルが応答したら、表で最も高い料金で見積もって実験者に一度通知する。
- 外部AI利用の疑い（§15.2）は記録のみで介入判断に使わない（`engine/config.py` の `policy.contamination_policy`。§13-9 案A）。

## 本番の構築（Oracle Cloud Always Free・東京・Arm。§15.7）

1. **OCI**：ホームリージョンを東京にしてアカウントを作る。Ampere A1（例：2 OCPU・6GB、Ubuntu 22.04/24.04 aarch64）を作成。「out of host capacity」なら別の AD か時間をおいて再試行。VCN のセキュリティ・リストで TCP 80/443 の受信を許可。
2. **DNS**：独自ドメインの A レコードをインスタンスの公開IPに向ける。
3. **サーバ**：リポジトリを `/opt/aisup` に置き、`deploy/env.example` を `deploy/.env` にコピーして値を入れ、`sudo bash deploy/setup_server.sh`。
   Docker・ファイアウォール・HTTPS（Caddy が証明書を自動取得）・毎日のバックアップ（cron 3:30）まで設定される。
4. **割付表**：`docker compose --env-file deploy/.env exec app python -m server.allocation --blocks 10 --seed 20041207 --out data/allocation.csv`。
   種は **20041207**（2026-11-24 決定）、10 ブロック・40 枠（適応20・一律20）。できた表の SHA-256 は
   `0ab7b1eaa0d5df033d3b0c9542f7a453713f797a8527acf423690635a21cfd88`（記録上の `allocation_file_hash` は先頭12桁 `0ab7b1eaa0d5`）。
   macOS と Linux（arm64, Python 3.12）で同じ表になることを確認済み。表示されたハッシュがこれと違えば使わない。以後変えない。
5. **死活監視**：外部の監視サービスに `https://<ドメイン>/healthz` を登録し、停止を通知させる。`ALERT_WEBHOOK_URL`（Slack/Discord 等）を設定すると、利用上限・内部エラー・期限切れ・完了をサーバから通知する。
6. **サーバ外へのバックアップ**：研究室の PC で `deploy/pull_backup.sh ubuntu@<ドメイン> ~/aisup_backups` を毎日実行する（cron / launchd）。取り込んだ最新のバックアップを開いてイベント数を表示する。参加者が全フェーズに到達した時点でもサーバ内にバックアップを作る。
7. **負荷試験**（本番データと分けて行う）：`python -m server.loadtest --url https://<ドメイン> --admin-token ... --n 10 --minutes 5`。
8. **回収（reclaim）への備え**：メモリ使用率を予備実験前に1週間観察する。

**国内VPSへの移行**（無料枠の条件変更・回収・容量不足・性能不足のとき）：新しいサーバで手順 2〜3 を行い、`data/`（`events.sqlite3`・`allocation.csv`・`backups/`）を旧サーバまたはバックアップから複製して `docker compose up -d`。サーバ起動時にイベントを再生して全参加者の状態が戻る。予備実験の前に一度試す。

**セキュリティの注意**：アプリのコンテナはサンドボックスを起動するため Docker ソケットを使う（ホストの管理者権限に近い）。このサーバにはアプリ以外を置かない。学習者のコードは別の使い捨てコンテナ（ネットワークなし・読み取り専用・nobody・資源制限）で動く。参加コードはアクセスログから除き、Referrer も送らない。

## サーバ・画面の約束

```
学習者が送信
 → PROMPT_DRAFT_SUBMIT → LLM で質問特徴 → PROMPT_FEATURES（失敗時も ok:false で保存）
 → engine.ingest(PROMPT_FEATURES) が Decision を返す（確認質問なら None）→ DECISION を保存
     pre_send      : AI に送る前に質問の具体化（P-A）→ PROMPT_SENT
     post_response : AI 回答を読み取り専用で表示 → 支援問題を生成・検証 → SUPPORT_SHOWN…END／SUPPORT_INFEASIBLE
     None          : そのまま送信・表示して編集可能
```

- **連番（v1.2 §2）**：画面のイベントは `client_seq`・`client_t`（ページ読込からの ms）・`page_id` を持ち、約1秒ごとにまとめて送る。サーバは連番の順にだけ適用し（`event_id = c<連番>`）、重複は捨て、欠落が30秒埋まらなければ `CLIENT_GAP` を記録して先へ進む。画面は適用済み（`acked`）になるまで localStorage に保持して再送する。
- **操作の同期**：実行・質問・提出・支援の操作は `after_seq`（その時点の最後の連番）と `page_id` を付け、未適用のイベントがあればサーバが 409 `not_synced` を返す（画面は送り切ってから再試行）。別のタブで開き直すと古いタブからの送信は 409 `other_page`。
- `SUPPORT_SHOWN` で「介入あり」になり判定窓から外れ、次の適格機会がクールダウン。`SUPPORT_INFEASIBLE` ではどちらも起きない。支援中の RUN・AI_RESPONSE・編集は RU に入らない。
- `CODE_EDIT` の由来はサーバが決める：直前の `COPY_FROM_AI` と一致→`ai`、エディタ内のコピー（`COPY_IN_EDITOR`）と一致→`editor`（元の文字の由来を引き継ぐ）、それ以外→`external`（画面で遮断されるはずのものが届いた印 `unblocked_external`）。

## 実装時に置いた解釈（仕様に明記がなく、予備実験で見直す点）

判定エンジン：

1. **規則の三値評価**：上位規則が「不明」のときは下位に進まず Unknown。例：E が不明なら D≥0.10 でも Active にしない。
2. **T**：（実行時のコード, 標準入力）の組の種類数。生の実行回数は `T_runs`。課題の「提出」は T に数えない（システムの採点で、学習者自身のテストではないため）。
3. **E の「修正」**（v1.2-dev.2）：コードの中身が変わった編集だけ（置き換え前後の文字列が同じ編集は数えない。`config.py` の `E_edit_rule`。`"any"` で v1.2-dev.1 の規則）。①は h3=1 の質問の後にそうした編集があり、その後に実行があれば1。「仮説に対応する変更か」は見ていない。
4. **I の「新規作成」**：関数単位で対応付いたらサイクル開始時にその関数がなかったこと。断片なら対象範囲の80%以上がそのサイクル中の入力。
5. **C-A の追加条件**：D か S で Active、E=0 確定、同じ判定範囲で I=1 が未観察。課題の「重要概念」はまだ使っていない。
6. **P-A の追加条件**：h2=0 かつ D か S で Active（Q だけ・改名だけは除く）。
7. **判定窓の範囲**：同じフェーズ（`main`）内。課題をまたいでも続く（`policy.window_scope: "task"` で課題内）。
8. **記録の欠落**（v1.2）：CLIENT_GAP・編集の適用失敗・スナップショット不一致・再開時の不一致のあるサイクルは C・I・E を Unknown（I も含める）。
9. **外部AI利用の疑い**（v1.2 §15.2。基準値は `config.py`）：サイクル内の PASTE_BLOCKED（エディタ・チャット欄・小エディタ・テストのコード欄のどれでも）が1回以上、または「10秒以上の離脱から戻って60秒以内に150文字以上の打鍵」。離脱は「タブ非表示またはウィンドウのフォーカス喪失」、長さは端末の単調時刻で測る（離脱中にページを読み直した離脱は長さ不明として数える）。

サーバ・画面：

10. **支援生成の時間上限**（§6-4）：15秒は1回の生成ごと（再生成すると待ち時間は最大で約2倍）。
11. **記述式の採点**（§13-3 案A、2026-11-24 決定）：学習者には正誤を表示せず、「満たせた観点」と「まだ触れていない観点」を示す（まだ触れていない観点があれば1回書き足せる）。正誤（全観点を満たしたか）・満たした観点の数 `met_count`・観点の数 `rubric_n` は SUPPORT_ANSWER に記録し、各問題の観点（ルーブリック）と模範解答は SUPPORT_SHOWN の `key` に記録する（学習者には送らない）。模範解答は見せない。
12. **P-A**：困っている点の選択と一文は記録するが、AI に追記するのは一文だけ。採点しない。時間切れは原文のまま送る。
13. **終了時のフィードバック**：動作予測は実行結果と正解、問題の分解は正解と解説、自力応用は入力ごとの期待出力との比較。
14. **AI 応答を待つ間**はエディタ・実行をロックする。
15. **学習者用AIの文脈**：システムプロンプトに課題文、会話履歴は課題ごと（最大20件）。エディタのコードは自動では渡さない。
16. **貼り付けの許可**（§15.2、2026-11-24 決定）：このページ内でコピーした文字列（AI 回答・エディタ・出力欄など）だけ貼り付けられる。外部からの貼り付け・ドロップは遮断して PASTE_BLOCKED（文字数と `via`：editor / chat / support / form）。エディタ（`editor`）・チャット欄（`chat`）・支援のコード欄（`support_code`）・支援の記述式回答欄（`support_text`）・P-A の一文（`clarify_sentence`）・事前/事後テストの全回答欄（`test_text`／`test_code`）・質問紙のコード欄（`questionnaire_code`）に同じ制限をかける（記録の `via` は「欄の種類:paste|drop」）。標準入力欄と質問紙の自由記述は制限しない。このページ内の別の場所（AI 回答など）からのドラッグ＆ドロップは、外部ではないので PASTE_BLOCKED にしないが、由来を追えないので受け付けず「コピーして貼り付けてください」と表示する。
17. **サンドボックス**：LeakSanitizer は無効（ptrace 不可）。CPU 2秒・壁時計3秒・メモリ256MB・プロセス64・出力64KB・同時実行2（待ち行列）。
18. **選択肢の並び**は opportunity_id と選択肢のハッシュで決める（乱数を使わない）。
19. **編集の差分**：打鍵と貼り付けは選択範囲とデータから正確に作る。取り消し・IME などは前後一致の差分（同じ文字が並ぶ箇所で位置が1文字ずれうる。内容は常に一致）。
20. **再開**：ページを開くたびに（2回目以降）SESSION_RESUME を記録する（中断時間はサーバが最後のイベントからの経過で測る）。
21. **環境確認**：スマートフォン・タブレット（iPadOS を含む）とサンドボックス接続失敗は先へ進めない。画面幅1000px未満・通信が遅い・非対応ブラウザは警告のみ。
22. **テスト・質問紙**：回答は自動保存（変化した項目だけ記録）。「提出」で次へ。必須項目は質問紙で指定（見本では負担・必要性・外部AI利用の自己申告）。
23. **教材**：支援の表示中も開ける（2026-11-24 決定）。開閉は MATERIAL_VIEW（支援中なら `during_support` と `opportunity_id`）。ページを閉じる・読み直すときに開いていたら閉じたと記録する。
24. **Tree-sitter 版 S**（v1.2-dev.2）：文字列・文字リテラルは中身を見ず `STR`/`CHR` の1記号。ポインタ型へのキャストと `sizeof(int *)`（`abstract_pointer_declarator`）は `PTR_DECL`（構造要素）。Tree-sitter 本体と C 文法の版を `config.py` の `structure_analyzer_versions` に書き、違う版が入っていればサーバ・エンジンが起動しない。判定結果の `S_analyzer` と SESSION_START に版を記録する。
26. **質問特徴の定義**（`prompts/prompt_features.md`、features-v3）：h4 は完成コード・答えそのもの以外の支援の形の指定だけ（完成コードの要求は h1=1・h4=0）。h2 は自分の状況の具体的な一点（コードの箇所・エラーメッセージ・出力と期待の違い・特定の構文や概念）で、目標の言い直しや「どこから」「どうすれば」だけは h2=0。ラベル付けの手引きにも同じ定義と境界例を載せる（2026-11-24 決定）。
25. **RQ1 の人手ラベル付け**（`server/labeling.py`、§9）：単位は判定エンジンのサイクル（既定は主課題）。評価者には分類に使えるイベントだけ（質問の原文・追加の質問・AI 回答・編集〔打鍵はまとめ、貼り付けは由来つき〕・実行・コンパイル・提出の合否）を見せ、参加者・群・システムの判定・支援の内容・タブ離脱などは見せない。支援が提示されたサイクルは提示の時点で打ち切る（`key.csv` の `truncated_at_support`。集計では全体と打ち切りなしを別に出す）。項目の並びは種つきで混ぜる。集計（`score`）は評価者間の一致（一致率・Cohen/Fleiss の κ）、正解（合議ファイル、なければ全員一致の項目）とシステムの HS/RU の Macro-F1・正解率・混同行列・Unknown 率、9状態の一致率と Pending 率、判定遅延（質問の送信→介入判断）と状態が決まるまでの時間（サイクルの長さ）。評価者が「判定不能」（`Undeterminable`）とした項目は F1 から除き件数を出す。RU は打ち切った項目を除いた結果（`RU_not_truncated`）も出す。
   **正解は合議で決める**（2026-11-24 決定）：2名が独立に記入 → `disagree` で κ（合議前）と不一致だけの一覧 `consensus/disagreements.html`・一致した値を記入済みの `gold_draft.csv` を作る → 合議で空欄を埋めて `score --gold` に渡す。

## 2026-11-24 の決定（仕様 v1.2 実装時）

- エディタ内のコピー→貼り付けは許可（外部からの貼り付けだけ遮断）。
- 課題は「提出して自動採点に合格するまで次に進めない」。課題ごとの時間上限は設けない。主課題・後続課題のフェーズごとに時間上限を設け、上限が来たら未完了でも次のフェーズへ。
- 外部AI利用の疑いフラグは上記 9 の既定値で実装し、`config.py` で調整する。記録のみ（介入判断に使わない）。
- 判定規則（rule_version を v1.2-dev.2 に）：E の「修正」は中身の変わった編集だけ。Tree-sitter 版 S の3点を修正（上記 24）。
- 教材は支援中も開ける（開閉を記録）。フェーズの時間上限（上記）。LLM の停止上限 240回・$6 を追加。
- 学習者用 AI はエディタのコードを自動では見ない。会話履歴は課題ごとに20件。チャット欄も外部からの貼り付けを遮断する。
- 割付表は 10 ブロック（40 コード）。乱数の種は研究者が決める。§13-1 は案A のまま（スレッド返信 UI は作らない）。
- 本実験前の凍結は予備実験の後。main への統合はブラウザでの通し確認の後。

## 未実装・未検証

- [ ] **実ブラウザでの確認**（v1.2 の画面）：同意〜終了の流れ、貼り付けの遮断、タブ離脱、再開、日本語入力、支援の各型。
- [ ] **OpenAI 実接続**：API キーを設定して `python -m server.check_llm`。特徴抽出・支援生成・採点のプロンプトの試行と、15秒に収まるか。
- [ ] **本番サーバでの負荷試験**（Oracle Ampere 2コア・実際の LLM）。手元（Mac・偽LLM・同時実行2）では10名×2分で実行の95%点 220ms・エラー0。
  支援生成の同時10件（2026-11-24、Mac・実際の LLM・サンドボックス同時実行2）：自己説明 20/20 成功・中央値 7.7秒・最大 9.7秒、動作予測 20/20・最大 5.5秒、5種類混在 20/20・最大 10.2秒（自力応用）。15秒超えなし。本番サーバでも `docker compose ... exec app python -m server.support_load --n 10` で確かめる。
- [ ] **研究者が用意するもの**：同意書（倫理審査で承認された文面）、事前・事後テスト、質問紙（`instruments/`）、課題・教材の最終確認、終了画面の文言（`completion_message`）。
- [ ] **質問特徴抽出の人手ラベルとの一致確認**（`prompts/prompt_features.md`）。
- [x] **人手ラベル付け用の出力**（RQ1。上記 25）。記入の手引き（`for_raters/guide.md`）の HS・RU の定義の部分は下書きなので研究者が確定させる。
- [ ] **割付表の作成**：本番サーバで種 20041207・`--blocks 10` で作り、ハッシュが上の値と一致することを確かめる（予備実験は別の表・別のデータディレクトリで）。
- [ ] **本実験前の凍結**（予備実験の後）：`rule_version`（現在 `v1.2-dev.2`）、`server_config` のハッシュ、プロンプト・教材・同意書・テストの版、割付表のハッシュ、サンドボックスイメージを記録して固定する。
