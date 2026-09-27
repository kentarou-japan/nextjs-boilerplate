# 06. 模擬運用の実行手順と、停止・復旧・再開の手順

本番発注は無効（`live_trading.enabled: false`、`LiveBrokerStub` は常に `LiveTradingDisabled` を送出）。以下はすべて模擬ブローカー（`SimBroker`、`state/paper/broker.db`）に対する手順。

## 1. 準備

```bash
cd trend_bot
pip install -e ".[dev]"            # 依存は requirements.lock.txt の版に固定
trendbot fetch-data                # 代替データ取得（コミットSHA固定、sha256記録）
python -m pytest -q                # 57 テスト
trendbot blockers                  # 本番阻害条件の一覧（0 件になるまで本番接続しない）
```

## 2. 模擬運用の開始と日次運転

```bash
trendbot paper init                                   # 状態DB作成・模擬口座へ入金（config/paper.yaml の initial_capital）
trendbot paper run --start 2024-01-02 --end 2024-06-28 # 指定期間を1営業日ずつ処理
trendbot paper status                                 # 最終処理日・停止・建玉・未約定注文
trendbot paper dashboard                              # state/paper/dashboard.html を生成
```

1 サイクル（`PaperRunner.run_day`）の順序:
1. （模擬のみ）取引所セッション処理: 前日注文を当日清算値で約定、値洗い、利息
2. ブローカー接続・約定取得（切断なら **状態を一切変えず** 停止記録のみ）
3. 内部台帳: 為替換算 → 利息 → 値洗い → 約定反映
4. 注文状態の照会、建玉・残高の照合、重複注文検査
5. 判断日データの品質検査（市場別凍結／全体停止）、証拠金検査
6. 停止方針に従い未約定注文を取消 → 判断（バックテストと同じコード）→ 注文を **先に DB に記録（PENDING_SUBMIT）してから** 送信
7. 同じ日付の再実行は何もしない（`skipped_already_processed`）。注文 ID は判断内容から決まるため、再送してもブローカーが重複として拒否する

障害注入（訓練用）:
```bash
trendbot paper run --start ... --end ... --inject disconnected@2024-03-05 --auto-reconnect
trendbot paper run --start ... --end ... --inject timeout_on_submit@2024-03-05
trendbot paper run --start ... --end ... --inject position_drift@2024-03-05
```

## 3. 停止条件と停止中の扱い

| 停止理由 | 範囲 | 新規注文 | 未約定注文 | 既存建玉 | 解除 |
|---|---|---|---|---|---|
| 市場データ異常・欠損・古い価格（data_quality） | 市場 | その市場は発注しない | その市場分を取消 | 維持 | 品質回復で自動 |
| 多数市場または為替のデータ異常（data_quality_global） | 全体 | なし | リスク増加分を取消 | 維持 | 手動 |
| 重複注文（duplicate_order） | 全体 | なし | リスク増加分を取消 | 維持 | 手動（照合OK必須） |
| 注文状態不明（unknown_order_status） | 全体 | なし。**再送しない** | 照会のみ | 維持 | 手動（照合OK必須） |
| 内部記録と口座の不一致（reconciliation_mismatch） | 全体 | なし | リスク増加分を取消 | 維持・手動調査 | 手動（照合OK必須） |
| API 接続断（api_disconnect） | 全体 | 送れない | 再接続後に照会 | 維持 | 手動（照合OK必須） |
| 証拠金不足（margin_shortfall） | 全体 | リスク削減のみ（既定は承認待ち） | リスク増加分を取消 | 削減計画 | 手動 |

**全決済を自動では行わない理由**: データや接続の障害時は価格・約定の把握自体が不確かで、機械的な全決済は誤った価格・過大なコスト・意図しない反対建玉を招き得る。各市場は平時からボラ・想定元本・証拠金の上限内にあり、停止中に建玉を「増やさない」ことを最優先とする。証拠金不足だけは建玉を減らす必要があるため、リスク削減注文を自動生成し、既定では人の承認後に送信する（`paper approve`）。

## 4. 停止時の手順（オペレーター）

1. `trendbot paper status` と `dashboard.html` の「停止履歴」「イベント」で理由と詳細を確認
2. 原因別の対処
   - **unknown_order_status**: ブローカー側で当該 `client_order_id` の状態を確認。システムは次回サイクル・再起動時に自動照会し、ブローカーに存在すれば状態を同期、存在しなければ `NOT_SENT` にする（再送はしない。翌日の判断で必要なら新しい注文が出る）
   - **reconciliation_mismatch**: 口座明細（建玉・現金）と `bot.db` を突き合わせ、原因（手動取引、約定漏れ、ブローカー側訂正など）を特定。内部記録を修正する場合は記録を残す
   - **duplicate_order**: ブローカー側の余分な注文を取消し、原因を調査
   - **api_disconnect**: 接続回復を確認。切断中の日は処理していないので、回復後のサイクルで期間全体の値洗いと約定がまとめて反映される
   - **margin_shortfall**: 承認待ちの削減注文を確認し `trendbot paper approve --operator <名前>`。承認は当日判断分のみ有効（翌日は失効）
   - **data_quality**: データ提供元を確認。市場別停止はデータ回復で自動解除
3. 再開: `trendbot paper resume --operator <名前>`（特定の停止のみ: `--halt-ids 3,4`）
   - 再開時に自動で照合を再実行し、不一致・状態不明が残っていれば **解除を拒否** する
4. 解除・承認はすべて `halts` / `events` テーブルに操作者名付きで残る

## 5. 再起動後の復元と照合

プロセスが停止・再起動した場合:
```bash
trendbot paper reconcile      # 起動時照合（run の開始時にも自動実行）
```
- `PENDING_SUBMIT`（DB 記録後、送信前後に停止）・`UNKNOWN` の注文をブローカーに照会し、状態を確定
- 内部建玉 ＋ 未反映の約定 と ブローカー建玉を比較。不一致なら全体停止
- 状態はトランザクション単位で保存されるため、途中停止でも「前日の状態」か「当日処理後の状態」のどちらかになる

テストで確認済みのケース（`tests/test_paper.py`）: 再起動後に中断なしの運用と NAV が一致、送信直前の停止で注文が二重に出ない、状態不明注文の解決、建玉ずれで停止し解除拒否、切断中は状態不変で回復後に追いつく、データ欠損で当該市場のみ凍結・自動解除、証拠金不足で削減のみ・承認後送信、本番ブローカーは常に拒否。

## 6. 本番接続を検討する場合の前提（今回は対象外）

`trendbot blockers` が 0 件であること、実データでの再検証、ブローカー API アダプタの実装とレビュー、秘密情報は環境変数/シークレットストア経由（コード・設定ファイルに書かない）、二重ロック（設定＋環境変数 `TRENDBOT_LIVE_TRADING`）、少額での並行稼働。詳細は `08_limitations_and_next_steps.md`。
