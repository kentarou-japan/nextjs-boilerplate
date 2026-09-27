# trend_bot — 複数資産トレンド追随 BOT（研究・バックテスト・模擬運用）

評価通貨: 円。対象: 株価指数・国債・通貨・商品の先物 18 市場（候補）。**本番発注は無効**（実装なしのスタブ＋二重ロック）。

> 現状の成績はすべて **代替データによる予備検証** または **合成データ（動作確認専用）** であり、先物 BOT の運用実績でも予測でもありません。結論は `docs/07_validation_report.md` §10（実資金での採用は見送りを提案）。

## ドキュメント

| ファイル | 内容 |
|---|---|
| `docs/00_design.md` | 環境確認の結果、設計、実装計画、主要な設計判断 |
| `docs/01_strategy_spec.md` | 戦略仕様（数式・使用データ・観測/判断/発注時刻・約定仮定・リスク上限） |
| `docs/02_data_spec.md` | CSV 仕様、代替データの出所一覧（確認日・ライセンス）、不足データと取得候補、品質検査 |
| `docs/03_instruments.md` | 18 市場の契約情報（**全て一次資料未確認＝本番阻害条件**）、乗り換え方針、研究用コスト仮定 |
| `docs/04_research_basis.md` | 研究で支持される内容と今回の独自仮説の区別 |
| `docs/05_trials_log.md` | 試行・判断・不具合修正の履歴（機械記録は `reports/trials_log.csv`） |
| `docs/06_operations_runbook.md` | 模擬運用の実行手順、停止・復旧・再開の手順 |
| `docs/07_validation_report.md` | 比較検証レポート（A/B/C/D、WF、PBO、感度、除外、ストレス、容量、最終評価期間） |
| `docs/08_limitations_and_next_steps.md` | 既知の制約・未検証事項・次段階への Go/No-Go 条件・利用者の判断が必要な事項 |

## 再現手順

```bash
cd trend_bot
python3 -m pip install -e ".[dev]"      # 依存の版は requirements.lock.txt（Python 3.11）
trendbot fetch-data                      # 代替データ（GitHub コミット SHA 固定）を data/raw/ へ。sha256 を manifest.json に記録
python3 -m pytest -q                     # 57 テスト（約 3 分）

# データ品質
trendbot quality --dataset proxy_daily   # → reports/proxy_daily/data_quality_*.csv

# 単発バックテスト（実行ごとに runs/<時刻>_<dataset>/manifest.json にコード版・設定ハッシュ・データハッシュを記録）
trendbot backtest --dataset proxy_daily --start 2000-01-03 --end 2020-12-31

# 評価一式（開発期間のみ。最終評価期間はロック）
trendbot evaluate --dataset proxy_daily
trendbot evaluate --dataset proxy_monthly
trendbot evaluate --dataset synthetic --quick     # 合成ランダムウォーク上で優位性が出ないことの確認

# 最終評価期間の評価（1 回だけ行う。実行ごとに reports/holdout_log.json に追記される）
trendbot evaluate --dataset proxy_daily --unlock-holdout

# 模擬運用（合成データ上。config/paper.yaml）
trendbot paper init
trendbot paper run --start 2024-01-02 --end 2024-06-28
trendbot paper dashboard                          # → state/paper/dashboard.html
trendbot blockers                                 # 本番阻害条件（現在 42 件）
```

決定性: 乱数は設定の seed から市場名ごとに派生（合成データ）。同じデータ・設定・コードで同じ結果になる。評価の並列実行（`--workers`）は結果に影響しない。

## 実データを入れるとき

`data/user/` に `futures_prices.csv`, `contracts.csv`, `fx.csv`, `rates.csv`（仕様は `docs/02_data_spec.md` §2）を置き、`evaluation/common.py` の dataset `real` を使う。契約仕様の確認結果は `config/instruments.yaml` の `verification` に記録する。秘密情報（API キー等）はコード・設定ファイルに書かず環境変数で渡す設計（ログは資格情報らしいキーを自動マスク）。

## 構成

```
config/        strategy.yaml, strategy_monthly_proxy.yaml, instruments.yaml, assumptions.yaml,
               search_space.yaml, datasets.yaml, paper.yaml
src/trendbot/  data/ (fetch, schema, loaders, quality, synthetic, calendars), contracts.py, continuous.py,
               signals.py, riskmodel.py, portfolio.py, risk.py, execution.py, accounting.py, strategy.py,
               backtest.py, metrics.py, evaluation/ (common, suite, pbo, benchmarks), paper/ (broker, state,
               runner), report/html.py, manifest.py, logutil.py, cli.py
tests/         test_futures_core.py, test_risk_quality.py, test_paper.py
reports/       評価レポート（HTML/CSV/JSON）、試行記録、最終評価記録、テスト結果、模擬運用デモ
```

既存の Next.js アプリ（リポジトリ直下）には変更を加えていない。
