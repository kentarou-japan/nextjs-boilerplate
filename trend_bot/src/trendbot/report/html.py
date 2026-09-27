"""Static HTML reports: evaluation report per dataset and the paper-trading dashboard.

Plain HTML + inline PNG charts (matplotlib). Every chart has its numbers in a table as well.
The data label banner (PROXY / SYNTHETIC) is always at the top.
"""
from __future__ import annotations

import base64
import html
import io
import json
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# Use a Japanese-capable font when one is installed (IPAGothic / Noto CJK); fall back silently otherwise.
for _name in ("IPAGothic", "IPAexGothic", "Noto Sans CJK JP", "Hiragino Sans"):
    if any(f.name == _name for f in font_manager.fontManager.ttflist):
        matplotlib.rcParams["font.family"] = _name
        break

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
INK, INK2, MUTED, SURFACE = "#0b0b0b", "#52514e", "#898781", "#fcfcfb"
STATUS = {"OK": "#0ca30c", "WARN": "#fab219", "FAIL": "#d03b3b"}

LABEL_TEXT = {
    "PROXY_PRELIMINARY": "代替データによる予備検証（現物・指数・為替スポット）。先物BOTの運用実績ではありません。",
    "SYNTHETIC_TEST_ONLY": "合成データ（動作確認専用）。成績には一切の意味がありません。",
    "REAL_FUTURES": "実際の先物限月別データ",
}

CSS = """
:root{--bg:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e4e3df}
body{background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;
margin:0 auto;max-width:1180px;padding:16px;line-height:1.5}
h1{font-size:22px;margin:8px 0} h2{font-size:18px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:15px;margin:18px 0 6px;color:var(--ink2)}
.banner{background:#fff4d6;border:1px solid #eda100;padding:10px 12px;border-radius:6px;font-weight:600}
.banner.synth{background:#fde8e8;border-color:#d03b3b}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0;overflow-x:auto}
table{border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left} th{color:var(--ink2);font-weight:600}
img{max-width:100%;height:auto} .muted{color:var(--muted);font-size:12px}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:12px}
ul{margin:4px 0 4px 18px;padding:0}
"""


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return '<img alt="chart" src="data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode() + '">'


def _ax(title: str, figsize=(10, 3.0)):
    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(axis="y", color="#e4e3df", linewidth=0.6)
    return fig, ax


def line_chart(series: dict[str, pd.Series], title: str, pct: bool = False, zero: bool = False) -> str:
    fig, ax = _ax(title)
    for i, (name, s) in enumerate(series.items()):
        ax.plot(s.index, s.values * (100 if pct else 1), color=SERIES[i % len(SERIES)], linewidth=1.6, label=name)
    if zero:
        ax.axhline(0, color=MUTED, linewidth=0.8)
    if pct:
        lo, hi = ax.get_ylim()
        dec = 0 if hi - lo >= 8 else (1 if hi - lo >= 0.8 else 2)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.{dec}f}%"))
    else:
        lo, hi = ax.get_ylim()
        if max(abs(lo), abs(hi)) >= 1e5:
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    if len(series) > 1:
        ax.legend(frameon=False, fontsize=8, labelcolor=INK2, ncol=min(4, len(series)))
    return _png(fig)


def fmt(v, kind: str = "auto") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, (int,)) and kind == "auto":
        return f"{v:,}"
    if isinstance(v, float):
        if kind == "pct":
            return f"{v * 100:.2f}%"
        if abs(v) >= 1e5:
            return f"{v:,.0f}"
        return f"{v:.3f}"
    return html.escape(str(v))


PCT_KEYS = {"cagr", "ann_mean_excess", "ann_vol", "max_drawdown", "worst_month", "worst_year", "es95_bar", "es95_month",
            "costs_pct_nav_per_year", "margin_usage_mean", "margin_usage_max", "exante_vol_mean", "return", "max_dd",
            "test_return_selected", "test_return_base", "cagr_int", "cagr_frac", "tracking_error_int_vs_frac",
            "ann_vol_int", "year_return", "pct_positive_years"}


def table(df: pd.DataFrame, pct_cols=None, max_rows: int = 400) -> str:
    pct_cols = set(pct_cols or []) | PCT_KEYS
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = []
    for _, r in df.head(max_rows).iterrows():
        rows.append("<tr>" + "".join(f"<td>{fmt(r[c], 'pct' if c in pct_cols else 'auto')}</td>" for c in df.columns) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def kv_table(d: dict) -> str:
    rows = "".join(f"<tr><td>{html.escape(str(k))}</td><td>{fmt(v, 'pct' if k in PCT_KEYS else 'auto')}</td></tr>"
                   for k, v in d.items() if not isinstance(v, (dict, list)))
    return f"<table><tbody>{rows}</tbody></table>"


def banner(label: str) -> str:
    cls = "banner synth" if label == "SYNTHETIC_TEST_ONLY" else "banner"
    return f'<div class="{cls}">データ区分: {label} — {LABEL_TEXT.get(label, "")}</div>'


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f"<body>{body}</body></html>")


# ------------------------------------------------------------------------------------------ evaluation report
def evaluation_report(report_dir: Path) -> Path:
    s = json.loads((report_dir / "summary.json").read_text())
    daily = pd.read_csv(report_dir / "base_dev_daily.csv", index_col=0, parse_dates=True)
    parts = [f"<h1>評価レポート: {html.escape(s['dataset'])}</h1>", banner(s["data_label"]),
             f"<p class='muted'>開発期間 {s['dev_period'][0]} 〜 {s['dev_period'][1]} / 最終評価期間 {s['holdout_start']} 〜 / "
             f"頻度 {s['frequency']} / 市場 {', '.join(s['universe'])} / データハッシュ {s['data_hash'][:16]}</p>"]
    nav = daily["nav"] / daily["nav"].iloc[0]
    dd = nav / nav.cummax() - 1
    parts.append("<h2>1. 基準戦略 D（開発期間）</h2>")
    parts.append('<div class="card">' + line_chart({"NAV（初期=1）": nav}, "資産推移（開発期間・超過収益ベース、現金利息なし）") + "</div>")
    parts.append('<div class="card">' + line_chart({"ドローダウン": dd}, "ドローダウン", pct=True, zero=True) + "</div>")
    parts.append('<div class="card">' + kv_table(s["base_dev"]) + "</div>")
    yr = pd.DataFrame([{"year": k, "return": v} for k, v in s["yearly_returns_dev"].items()])
    parts.append("<h3>年次リターン（開発期間）</h3><div class='card'>" + table(yr) + "</div>")
    att = pd.DataFrame(s["attribution_dev"])
    parts.append("<h3>損益寄与（初期資金比 %、開発期間）</h3><div class='card'>" +
                 table(att[["item", "type", "gross_pnl_pct", "cost_pct", "net_pct"]].round(3)) + "</div>")
    if "stat_exante_vol" in daily:
        parts.append('<div class="card">' + line_chart({"事前推定ボラ": daily["stat_exante_vol"].dropna(),
                                                         "証拠金使用率": daily["stat_margin_usage"].dropna()},
                                                        "リスク（NAV比）", pct=True) + "</div>")
    parts.append('<div class="card">' + line_chart({"累積コスト（NAV比）": (-daily["costs"]).cumsum() / daily["nav"].iloc[0]},
                                                    "売買コスト累計", pct=True) + "</div>")

    wf = s["walk_forward"]
    parts.append("<h2>2. ウォークフォワードと過剰適合（PBO）</h2>")
    parts.append(f"<p>判定: <b>{html.escape(wf['decision'])}</b> / OOS Sharpe 改善 {fmt(wf['sharpe_improvement'])} / "
                 f"PBO {fmt(wf['pbo'].get('pbo'))}（CSCV, S={wf['pbo'].get('S')}, 候補 {wf['pbo'].get('N')} 通り）</p>")
    comp = pd.DataFrame([{"系列": "WF選択（OOS）", **{k: wf["wf_selected_oos"].get(k) for k in ("cagr", "ann_vol", "sharpe", "max_drawdown")}},
                         {"系列": "基準案（同OOS期間）", **{k: wf["baseline_oos"].get(k) for k in ("cagr", "ann_vol", "sharpe", "max_drawdown")}}])
    parts.append("<div class='card'>" + table(comp) + "</div>")
    wfd = pd.read_csv(report_dir / "walk_forward.csv")
    parts.append("<div class='card'>" + table(wfd.round(4)) + "</div>")

    runs = pd.read_csv(report_dir / "runs_dev.csv")
    cols = ["name", "cagr", "ann_vol", "sharpe", "max_drawdown", "worst_year", "costs_pct_nav_per_year", "turnover_notional_per_year",
            "margin_usage_max"]
    for title, prefix in (("3. 試行した全パラメータ（事前登録グリッド、不採用案を含む）", "grid:"), ("4. 感度分析", "sens:"),
                          ("5. 市場・資産分類を除いた頑健性", ("lomo:", "loco:")), ("6. ストレステスト（再計算）", "stress:"),
                          ("7. 資金規模と整数枚数", "cap:")):
        sel = runs[runs["name"].str.startswith(prefix) | (runs["name"] == "D_base (dev)")]
        parts.append(f"<h2>{title}</h2><div class='card'>" + table(sel[cols]) + "</div>")
        if prefix == "cap:" and "capacity" in s:
            parts.append("<div class='card'>" + table(pd.DataFrame(s["capacity"])) + "</div>")
    lo = pd.read_csv(report_dir / "leave_one_year_out.csv")
    parts.append("<h3>特定年を除外した場合</h3><div class='card'>" + table(lo.round(4)) + "</div>")
    parts.append("<h3>瞬間ショック（保有全市場が同時に kσ 逆行、分散効果なし）</h3><div class='card'>" +
                 table(pd.DataFrame(s["shock_scenarios_dev"]).T.reset_index().rename(columns={"index": "scenario"})) + "</div>")
    parts.append("<h3>相場環境別（開発期間内）</h3><div class='card'>" +
                 table(pd.DataFrame(s["historical_windows_dev"]).T.reset_index().rename(columns={"index": "window"})) + "</div>")

    parts.append("<h2>8. 比較対象 A/B/C/D（月次換算）</h2>")
    cd = s.get("comparison_dev", {})
    rows = []
    for k in ("D_monthly", "C_single_12m_monthly", "B_usd_excess", "D_vol_matched_to_B_usd_excess", "B_jpy_unhedged_total"):
        if cd.get(k):
            rows.append({"series": k, **{m: cd[k].get(m) for m in ("start", "end", "cagr", "ann_vol", "sharpe", "max_drawdown", "worst_year")}})
    if rows:
        parts.append("<div class='card'>" + table(pd.DataFrame(rows)) + "</div>")
    parts.append(f"<p class='muted'>A（現金）: {html.escape(json.dumps(cd.get('A_cash', {}), ensure_ascii=False))} / "
                 f"USD T-bill 参考: {html.escape(json.dumps(cd.get('A_usd_tbill_reference', {}), ensure_ascii=False))}</p>")
    if cd.get("overlay"):
        ov = cd["overlay"]
        parts.append("<h3>既存株式ポートフォリオ（米国株・円換算・為替ヘッジなし）への組入れ</h3>")
        parts.append(f"<p>BOTと株式の月次相関 {fmt(ov.get('corr_bot_equity'))} / 株式の最悪10%月の平均: 株式 "
                     f"{fmt(ov.get('eq_mean_in_worst_equity_decile'), 'pct')}、BOT {fmt(ov.get('bot_mean_in_worst_equity_decile'), 'pct')}</p>")
        parts.append("<div class='card'>" + table(pd.DataFrame(ov["table"])) + "</div>")
        parts.append("<p class='muted'>BOT部分の担保現金利息は含めていない（JPY短期金利未入手）。</p>")

    if "holdout" in s:
        h = s["holdout"]
        parts.append("<h2>9. 最終評価期間（調整に一度も使っていない期間）</h2>")
        parts.append(f"<p>評価回数（同データセットでの過去評価数）: {h['log_entry']['n_prior_evaluations_same_dataset']} / "
                     f"期間中にデータがある市場: {', '.join(h['markets_in_holdout'])}</p>")
        parts.append("<div class='card'>" + kv_table(h["summary"]) + "</div>")
        parts.append("<h3>C（単一期間）同期間</h3><div class='card'>" + kv_table(h["C_summary"]) + "</div>")
        parts.append("<div class='card'>" + table(pd.DataFrame([{"year": k, "return": v} for k, v in h["yearly"].items()])) + "</div>")
        if h.get("historical_windows"):
            parts.append("<div class='card'>" + table(pd.DataFrame(h["historical_windows"]).T.reset_index().rename(columns={"index": "window"})) + "</div>")
        hr = pd.read_csv(report_dir / "holdout_returns.csv", index_col=0, parse_dates=True)["ret"]
        hn = (1 + hr).cumprod()
        parts.append('<div class="card">' + line_chart({"NAV（最終評価期間, 初期=1）": hn}, "最終評価期間の資産推移") + "</div>")
    out = report_dir / "report.html"
    out.write_text(page(f"評価 {s['dataset']}", "".join(parts)), encoding="utf-8")
    return out


# ------------------------------------------------------------------------------------------ paper dashboard
def paper_dashboard(state_dir: Path, out: Path | None = None) -> Path:
    db = sqlite3.connect(state_dir / "bot.db")
    meta = {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}
    nav = pd.read_sql("SELECT * FROM nav ORDER BY date", db, parse_dates=["date"]).set_index("date")
    pos = pd.read_sql("SELECT market, contract, qty, last_price FROM positions ORDER BY market", db)
    orders = pd.read_sql("SELECT decision_date, market, contract, qty, reason, intent, status, client_order_id, note "
                         "FROM orders ORDER BY created_utc DESC, client_order_id LIMIT 200", db)
    halts = pd.read_sql("SELECT id, scope, reason, detail, created_date, created_utc, cleared_utc, cleared_by FROM halts ORDER BY id DESC", db)
    quality = pd.read_sql("SELECT * FROM quality WHERE date=(SELECT MAX(date) FROM quality) ORDER BY market", db)
    events = pd.read_sql("SELECT ts, date, level, event, body FROM events ORDER BY id DESC LIMIT 50", db)
    label = meta.get("data_label", "")
    active = halts[halts["cleared_utc"].isna()]
    parts = ["<h1>模擬運用ダッシュボード</h1>", banner(label),
             f"<p class='muted'>最終処理日 {meta.get('last_processed_date')} / 設定ハッシュ {str(meta.get('config_hash'))[:16]} / "
             f"本番発注: 無効</p>"]
    if len(active):
        parts.append("<div class='banner synth'>停止中: " + html.escape("; ".join(f"{r.scope}:{r.reason}" for r in active.itertuples())) + "</div>")
    else:
        parts.append("<p><span class='pill' style='background:#0ca30c'>稼働中（停止なし）</span></p>")
    if len(nav):
        n = nav["nav"]
        parts.append('<div class="card">' + line_chart({"NAV（円）": n}, "資産推移") + "</div>")
        parts.append('<div class="card">' + line_chart({"ドローダウン": n / n.cummax() - 1}, "ドローダウン", pct=True, zero=True) + "</div>")
        parts.append('<div class="card">' + line_chart({"事前推定ボラ": nav["exante_vol"].dropna(), "証拠金使用率": nav["margin_usage"].dropna()},
                                                        "リスク（NAV比）", pct=True) + "</div>")
        last = nav.iloc[-1]
        parts.append("<h2>直近の状態</h2><div class='card'>" + kv_table({
            "NAV": float(last["nav"]), "exante_vol": last["exante_vol"], "margin_usage_mean": last["margin_usage"],
            "gross_notional": last["gross_notional"], "dd_scale": last["dd_scale"],
            "累積コスト(円)": float(nav["costs"].sum()), "累積先物損益(円)": float(nav["futures_pnl"].sum()),
            "累積為替換算損益(円)": float(nav["fx_translation"].sum()), "累積現金利息(円)": float(nav["interest"].sum())}) + "</div>")
    parts.append("<h2>建玉</h2><div class='card'>" + table(pos) + "</div>")
    parts.append("<h2>データ状態（最新判断日）</h2><div class='card'>" + table(quality) + "</div>")
    parts.append("<h2>停止履歴</h2><div class='card'>" + table(halts) + "</div>")
    parts.append("<h2>注文履歴（直近200件）</h2><div class='card'>" + table(orders) + "</div>")
    parts.append("<h2>イベント（直近50件）</h2><div class='card'>" + table(events) + "</div>")
    out = out or state_dir / "dashboard.html"
    out.write_text(page("模擬運用ダッシュボード", "".join(parts)), encoding="utf-8")
    return out
