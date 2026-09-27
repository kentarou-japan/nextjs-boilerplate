# 02. データ仕様と出所一覧

確認日: 2026-09-27。本環境から取引所・データベンダー・中央銀行サイトへの通信はネットワークポリシーで遮断されていたため、先物の実データは取得できていない。

## 1. データ区分とラベル

すべての成果物（JSON・CSV・HTML）に以下のいずれかを付与している。

| ラベル | 意味 | 今回の有無 |
|---|---|---|
| `REAL_FUTURES` | 限月別の先物清算値・出来高・建玉 | **なし** |
| `PROXY_PRELIMINARY` | 現物・指数・為替スポットによる代替（予備検証） | あり |
| `SYNTHETIC_TEST_ONLY` | 合成データ（動作確認専用、成績に意味なし） | あり |

## 2. 実データ投入用 CSV 仕様（`data/user/` に置く → `trendbot` の dataset=`real`）

### futures_prices.csv（1 行 = 1 市場 × 1 限月 × 1 取引日）
| 列 | 必須 | 型 | 内容 |
|---|---|---|---|
| date | ○ | YYYY-MM-DD | 取引所の取引日（夜間セッションは取引所の帰属日） |
| market | ○ | str | `instruments.yaml` のキー（例 ES, NK225M） |
| contract | ○ | str | `<market><月コード><西暦>`（例 ESZ2025, JGB10H2026） |
| settle | ○ | float | 清算値（建値単位。マイナス可の市場あり） |
| open, high, low | | float | 任意 |
| volume, open_interest | | float | 限月別。出来高による乗り換え判定・流動性上限に使用（1 日ラグ） |
| status | | ok/halt/limit | 取引停止日は **行を作って** status=halt。休場日は行を作らない |

### contracts.csv
market, contract, last_trade_date（必須）, first_notice_date（任意）。取引所公表値。

### fx.csv
date, ccy, jpy_per_unit（1 外貨あたり円）, fixing（参照レート名。例 WMR 16:00 London）。

### rates.csv
date, ccy, rate_annual_pct, tenor, source。JPY は TONA・無担保コール O/N・国庫短期証券などから一つを選び固定。

投入後の検査: `trendbot quality --dataset real`（重複・欠損・異常値・古い価格・取引停止・データ年齢を検出）。

## 3. 代替データ（取得済み、`config/datasets.yaml` で GitHub コミット SHA 固定、sha256 を `data/raw/manifest.json` に記録）

| 名称 | 一次出所 | 期間 | 頻度 | 用途 | 主な偏り |
|---|---|---|---|---|---|
| fx_daily | FRB H.10（FRED 経由、datopian 再配布） | 1971–2026-09 | 日次 | 6J/6E/6B/6A/6C 代替、円換算 | 金利差（キャリー）なし。**README の表記規約と実データが不一致**（全通貨が外貨/USD）→ 値域検査で検証 |
| wti_daily | EIA Cushing WTI 現物 | 1986–2026-09 | 日次 | CL 代替 | ロールイールドなし。2020-04-20 に −36.98 |
| natgas_daily | EIA Henry Hub 現物 | 1997–2026-09 | 日次 | NG 代替 | 先物コンタンゴのロール損失を含まない。現物の天候スパイク（例 2026-01）を含む |
| arch_sp500_daily | S&P500 指数（arch 同梱、元 Yahoo） | 1999–2018 | 日次 | ES 代替 | 配当−金利なし。**2019 年以降なし** |
| sp500_monthly | Shiller（月平均） | 1871–2026-08 | 月次 | ES 代替、ベンチマーク B | 平均値系列 |
| gold_monthly | World Bank Pink Sheet（月平均） | 1960–2026-08 | 月次 | GC 代替（1975–） | 平均値系列 |
| ust10y_monthly | FRB H.15 10 年利回り（月平均） | 1953–2026-07 | 月次 | ZN 代替（デュレーション 6.5 仮定の価格化）、B の債券 | クーポン・キャリー・CTD 無視 |
| imf_commodities_monthly | IMF 一次産品価格（月平均） | 1980–2017-06 | 月次 | HG/ZC/ZS 代替 | 2017 年で終了、平均値系列 |
| arch_ff_monthly | Kenneth French Data Library（RF, Mkt-RF） | 1926–2018-11 | 月次 | USD 短期金利（参考） | 2018-11 で終了 |

代替データに存在しない市場: NK225M, FESX, FGBL, JGB10, GILT（月次含め）、日次では ZN, GC, HG, ZC, ZS も無い。

## 4. 不足データと取得候補（費用・ライセンス要確認）

| 必要データ | 候補（例） | 確認事項 |
|---|---|---|
| 限月別清算値・出来高・建玉（全 18 市場、2000 年以降） | 取引所の公式データ販売（CME DataMine、JPX データ、Eurex/Deutsche Börse、ICE）、商用ベンダー（例: Refinitiv/LSEG、Bloomberg、Norgate、CSI Data、Databento 等） | 費用、再配布可否、限月別か連続系列のみか、清算値の定義、夜間セッションの帰属日 |
| 限月カレンダー（最終取引日・第一通知日） | 取引所公表資料 | 過去の規則変更 |
| 手数料・取引所手数料・証拠金（時系列） | 利用予定ブローカーの見積り、CME/JPX/Eurex の証拠金公表値 | ブローカー上乗せ証拠金、円建て担保の掛け目 |
| JPY 短期金利 | 日本銀行（無担保コール O/N）、TONA | 系列の選択と固定 |
| 為替の参照レート | WM/Reuters 16:00 London 等 | ライセンス |
| 既存運用商品（参考比較） | 購入可能な公募投信・ETF の基準価額 | 販売可否、信頼できる価格データ |

## 5. データ品質検査（`src/trendbot/data/quality.py`）

| 検査 | 判定 | 判断時の扱い |
|---|---|---|
| (date, contract) 重複 | FAIL | その市場を凍結 |
| 非正価格（マイナス不可市場） | FAIL | 凍結 |
| 欠損（取引所暦が開場なのに行なし） | WARN / 判断日なら FAIL | 凍結 |
| 休場（暦が休場） | 問題なし | — |
| 取引停止・値幅制限（status） | INFO | 当日の約定不可（持ち越し）。休場とは区別 |
| 同値連続（古い価格） | WARN / 判断日付近なら FAIL | 凍結 |
| 異常値（|ΔP| > 12 × 頑健σ、過去窓のみ） | WARN / 判断日付近なら FAIL | 凍結 |
| データ年齢（判断日に当日データなし） | FAIL | 凍結 |
| 為替の非正・異常 | FAIL/WARN | 全体停止（為替欠損時） |
| 凍結市場が 4 以上 | — | 全体停止 |

代替データへの実行結果は `reports/<dataset>/data_quality_*.csv`。
