# 03. 投資対象（候補 18 市場）と契約情報

生成元: `config/instruments.yaml`（契約）・`config/assumptions.yaml`（研究用仮定）。本表は 2026-09-27 に YAML から生成。

**全市場の契約仕様は一次資料で未確認（verification.status=unverified）**。取引所サイトへの通信が遮断されていたため。これらはすべて本番接続の阻害条件（`trendbot blockers`）。手数料・証拠金・出来高は研究用仮定であり事実ではない。

## 選定の考え方

- 株価指数（米・日・欧）、国債（米・独・日・英）、通貨（円・ユーロ・ポンド・豪ドル・加ドル、いずれも CME の対米ドル）、商品（金・銅・WTI・天然ガス・コーン・大豆）の 18 市場。
- 銘柄数を増やすことは目的にせず、Brent（WTI と高相関）、NQ（ES と高相関）、ミニ限月は候補から外した。経済的に似たリスクは sector（例: energy = CL+NG）と資産分類の両方でリスク上限を設けて管理する。
- JGB10 は 1 枚の想定元本が約 1.4 億円と大きく、1 億円運用では整数化で建たない可能性が高い（容量分析で確認）。ミニ JGB 等の代替は仕様未確認。
- NG は季節性の強い期間構造を持ち、現物代替での結果は先物と大きく乖離し得る。

## 契約情報（未確認値）

| 市場 | 名称 | 取引所 | 分類/sector | 建値 | 乗数 | 呼値 | 限月 | 最終取引日規則 | 第一通知日規則 | 受渡 | 暦(TZ) | 負価格 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ES | E-mini S&P 500 | CME | equity/equity_us | USD | 50 | 0.25 | HMUZ | third_friday | — | cash | CMES (America/Chicago) | — |
| NK225M | 日経225mini | OSE | equity/equity_jp | JPY | 100 | 5 | HMUZ | business_day_before_second_friday | — | cash | XTKS (Asia/Tokyo) | — |
| FESX | EURO STOXX 50 Index Futures | EUREX | equity/equity_eu | EUR | 10 | 1 | HMUZ | third_friday | — | cash | XEUR (Europe/Berlin) | — |
| ZN | 10-Year U.S. Treasury Note | CBOT | rates/rates_us | USD | 1000 | 0.015625 | HMUZ | seventh_bd_before_last_bd | last_bd_of_prior_month | physical | CMES (America/Chicago) | — |
| FGBL | Euro-Bund Futures | EUREX | rates/rates_eu | EUR | 1000 | 0.01 | HMUZ | eurex_bond_two_days_before_tenth | — | physical | XEUR (Europe/Berlin) | — |
| JGB10 | 長期国債先物（10年） | OSE | rates/rates_jp | JPY | 1e+06 | 0.01 | HMUZ | jgb_fifth_bd_before_twentieth | — | physical | XTKS (Asia/Tokyo) | — |
| GILT | Long Gilt Futures | ICE_EU | rates/rates_uk | GBP | 1000 | 0.01 | HMUZ | second_bd_before_last_bd | two_bd_before_first_day | physical | XLON (Europe/London) | — |
| 6J | Japanese Yen Futures | CME | fx/fx_jpy | USD | 1.25e+07 | 5e-07 | HMUZ | cme_fx_two_bd_before_third_wednesday | — | physical | CMES (America/Chicago) | — |
| 6E | Euro FX Futures | CME | fx/fx_eur | USD | 125000 | 5e-05 | HMUZ | cme_fx_two_bd_before_third_wednesday | — | physical | CMES (America/Chicago) | — |
| 6B | British Pound Futures | CME | fx/fx_gbp | USD | 62500 | 0.0001 | HMUZ | cme_fx_two_bd_before_third_wednesday | — | physical | CMES (America/Chicago) | — |
| 6A | Australian Dollar Futures | CME | fx/fx_aud | USD | 100000 | 5e-05 | HMUZ | cme_fx_two_bd_before_third_wednesday | — | physical | CMES (America/Chicago) | — |
| 6C | Canadian Dollar Futures | CME | fx/fx_cad | USD | 100000 | 5e-05 | HMUZ | cme_fx_one_bd_before_third_wednesday | — | physical | CMES (America/Chicago) | — |
| GC | COMEX Gold | COMEX | commodity/metals_precious | USD | 100 | 0.1 | GJMQVZ | third_last_bd | last_bd_of_prior_month | physical | CMES (America/Chicago) | — |
| HG | COMEX Copper | COMEX | commodity/metals_industrial | USD | 25000 | 0.0005 | HKNUZ | third_last_bd | last_bd_of_prior_month | physical | CMES (America/Chicago) | — |
| CL | NYMEX WTI Light Sweet Crude Oil | NYMEX | commodity/energy | USD | 1000 | 0.01 | FGHJKMNQUVXZ | cl_three_bd_before_25th_prior_month | — | physical | CMES (America/Chicago) | 可 |
| NG | NYMEX Henry Hub Natural Gas | NYMEX | commodity/energy | USD | 10000 | 0.001 | FGHJKMNQUVXZ | ng_three_bd_before_first_day | — | physical | CMES (America/Chicago) | — |
| ZC | CBOT Corn | CBOT | commodity/agri_grains | USD | 50 | 0.25 | HKNUZ | bd_before_fifteenth | last_bd_of_prior_month | physical | CMES (America/Chicago) | — |
| ZS | CBOT Soybeans | CBOT | commodity/agri_oilseeds | USD | 50 | 0.25 | FHKNQUX | bd_before_fifteenth | last_bd_of_prior_month | physical | CMES (America/Chicago) | — |

## 取引時間（未確認の記述）

| 市場 | 取引時間 | 清算値の目安時刻（現地） | 一次資料 URL（未アクセス） |
|---|---|---|---|
| ES | 日〜金 17:00-16:00 CT（16:00-17:00 は日次休止） | 15:00 | https://www.cmegroup.com/markets/equities/sp/e-mini-sandp500.contractSpecs.html |
| NK225M | 日中 8:45-15:45、夜間 17:00-翌6:00 JST（2024年11月変更後と認識、未確認） | 15:45 | https://www.jpx.co.jp/derivatives/products/domestic/225mini/ |
| FESX | 01:10-22:00 CET（未確認） | 17:30 | https://www.eurex.com/ex-en/markets/idx/stx/euro-stoxx-50-derivatives |
| ZN | 日〜金 17:00-16:00 CT | 14:00 | https://www.cmegroup.com/markets/interest-rates/us-treasury/10-year-us-treasury-note.contractSpecs.html |
| FGBL | 01:10-22:00 CET（未確認） | 17:15 | https://www.eurex.com/ex-en/markets/int/fix/government-bonds/Euro-Bund-Futures-137298 |
| JGB10 | 日中・夜間（2024年の時間変更後の詳細は未確認） | 15:00 | https://www.jpx.co.jp/derivatives/products/jgb/jgb-futures/ |
| GILT | 08:00-18:00 London（未確認） | 16:15 | https://www.ice.com/products/37650330/Long-Gilt-Future |
| 6J | 日〜金 17:00-16:00 CT | 14:00 | https://www.cmegroup.com/markets/fx/g10/japanese-yen.contractSpecs.html |
| 6E | 日〜金 17:00-16:00 CT | 14:00 | https://www.cmegroup.com/markets/fx/g10/euro-fx.contractSpecs.html |
| 6B | 日〜金 17:00-16:00 CT | 14:00 | https://www.cmegroup.com/markets/fx/g10/british-pound.contractSpecs.html |
| 6A | 日〜金 17:00-16:00 CT | 14:00 | https://www.cmegroup.com/markets/fx/g10/australian-dollar.contractSpecs.html |
| 6C | 日〜金 17:00-16:00 CT | 14:00 | https://www.cmegroup.com/markets/fx/g10/canadian-dollar.contractSpecs.html |
| GC | 日〜金 17:00-16:00 CT | 12:30 | https://www.cmegroup.com/markets/metals/precious/gold.contractSpecs.html |
| HG | 日〜金 17:00-16:00 CT | 12:00 | https://www.cmegroup.com/markets/metals/base/copper.contractSpecs.html |
| CL | 日〜金 17:00-16:00 CT | 13:30 | https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.contractSpecs.html |
| NG | 日〜金 17:00-16:00 CT | 14:30 | https://www.cmegroup.com/markets/energy/natural-gas/natural-gas.contractSpecs.html |
| ZC | 日〜金 19:00-7:45, 8:30-13:20 CT（未確認） | 13:15 | https://www.cmegroup.com/markets/agriculture/grains/corn.contractSpecs.html |
| ZS | 日〜金 19:00-7:45, 8:30-13:20 CT（未確認） | 13:15 | https://www.cmegroup.com/markets/agriculture/oilseeds/soybean.contractSpecs.html |

## 乗り換え方針

- 既定: 基準日（第一通知日と最終取引日の早い方）の **5 営業日前** に次限月へ（`roll.business_days_before`）。現物受渡の市場で通知日・受渡に入らない。
- 代替: 出来高基準（前日の出来高が次限月で上回ったら切替、戻らない、かつ暦上の期限までに必ず切替）。
- 乗り換えは旧限月の決済と新限月の建てを同日に発注し、両脚に手数料・スプレッド・インパクトを計上。
- 休場日は取引所暦（exchange_calendars: CMES, XEUR, XLON, XTKS）で判定。OSE 専用暦は無く東証暦で代用（未確認）。

## 研究用仮定（コスト・証拠金・流動性）— 事実ではない

| 市場 | 手数料/枚/片道（建値通貨） | ADV（枚） | 当初証拠金（想定元本比） | スプレッド（ティック） |
|---|---|---|---|---|
| ES | 2.5 | 1,000,000 | 0.07 | 1.0 |
| NK225M | 100 | 500,000 | 0.07 | 1.0 |
| FESX | 1.0 | 500,000 | 0.07 | 1.0 |
| ZN | 2.5 | 1,000,000 | 0.025 | 1.0 |
| FGBL | 1.0 | 500,000 | 0.025 | 1.0 |
| JGB10 | 1500 | 20,000 | 0.025 | 1.0 |
| GILT | 1.0 | 100,000 | 0.025 | 1.0 |
| 6J | 2.5 | 100,000 | 0.04 | 1.0 |
| 6E | 2.5 | 150,000 | 0.04 | 1.0 |
| 6B | 2.5 | 60,000 | 0.04 | 1.0 |
| 6A | 2.5 | 60,000 | 0.04 | 1.0 |
| 6C | 2.5 | 50,000 | 0.04 | 1.0 |
| GC | 2.5 | 150,000 | 0.09 | 1.0 |
| HG | 2.5 | 60,000 | 0.09 | 1.0 |
| CL | 2.5 | 250,000 | 0.09 | 1.0 |
| NG | 2.5 | 150,000 | 0.15 | 1.0 |
| ZC | 2.5 | 200,000 | 0.09 | 1.0 |
| ZS | 2.5 | 120,000 | 0.09 | 1.0 |

インパクト = 0.1 × 日次価格ボラ × √(枚数/ADV)。外貨の円転コスト 2bp。ストレステストでコスト×2・×3、ADV×0.25、証拠金×2・×4 を検証。
