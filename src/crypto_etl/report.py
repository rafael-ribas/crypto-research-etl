from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import text

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from .config import settings

REPORT_DIR = Path("reports")
CHART_DIR = REPORT_DIR / "charts"
ASSETS_DIR = REPORT_DIR / "assets"
TEMPLATE_DIR = Path("templates")


@dataclass
class ReportContext:
    last_dt: str
    window_days: int
    perf_window: int
    corr_window: int
    snapshot: pd.DataFrame
    series: pd.DataFrame
    corr_vs_btc: pd.DataFrame
    corr_matrix: pd.DataFrame
    perf_table: pd.DataFrame
    risk_table: pd.DataFrame
    commentary: list[str]
    kpis: dict[str, str]
    charts: dict[str, str]


def _ensure_dirs():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    (ASSETS_DIR / "css").mkdir(parents=True, exist_ok=True)
    (ASSETS_DIR / "images").mkdir(parents=True, exist_ok=True)


def query_for_report(engine, window_days: int = 120) -> tuple[str, pd.DataFrame, pd.DataFrame]:
    with engine.connect() as conn:
        last_dt = conn.execute(text("SELECT MAX(dt) FROM market_daily")).scalar()

    if last_dt is None:
        raise RuntimeError("Sem dados em market_daily. Rode o ETL primeiro.")

    snap = pd.read_sql(
        text("""            SELECT a.asset_id, a.symbol, a.name,
                   m.dt, m.price_usd, m.market_cap_usd, m.volume_24h_usd,
                   m.return_1d, m.vol_30d
            FROM market_daily m
            JOIN assets a ON a.asset_id = m.asset_id
            WHERE m.dt = :dt
            ORDER BY m.market_cap_usd DESC
        """),
        engine,
        params={"dt": last_dt},
    )

    series = pd.read_sql(
        text("""            SELECT a.asset_id, a.symbol, a.name,
                   m.dt, m.price_usd, m.market_cap_usd, m.volume_24h_usd, m.return_1d, m.vol_30d
            FROM market_daily m
            JOIN assets a ON a.asset_id = m.asset_id
            ORDER BY m.dt ASC
        """),
        engine,
    )

    series["dt"] = pd.to_datetime(series["dt"])
    last_dt_ts = pd.to_datetime(last_dt)
    start = last_dt_ts - pd.Timedelta(days=window_days)
    series = series[series["dt"] >= start].copy()

    return str(last_dt_ts.date()), snap, series


def _ensure_returns(series: pd.DataFrame) -> pd.DataFrame:
    df = series.copy().sort_values("dt")
    if "return_1d" not in df.columns or df["return_1d"].isna().all():
        df["return_1d"] = df.groupby("asset_id")["price_usd"].pct_change()
    return df


def compute_corr_vs_btc(series: pd.DataFrame, corr_window: int = 60) -> pd.DataFrame:
    df = _ensure_returns(series)
    last_dt = df["dt"].max()
    start = last_dt - pd.Timedelta(days=corr_window)
    w = df[df["dt"] >= start].copy()

    pivot = w.pivot_table(index="dt", columns="asset_id", values="return_1d", aggfunc="mean").sort_index()
    if "bitcoin" not in pivot.columns:
        return pd.DataFrame(columns=["asset_id", "corr_btc"])

    btc = pivot["bitcoin"]
    out = []
    for col in pivot.columns:
        if col == "bitcoin":
            continue
        out.append({"asset_id": col, "corr_btc": pivot[col].corr(btc)})
    return pd.DataFrame(out)


def compute_corr_matrix(series: pd.DataFrame, corr_window: int = 60) -> pd.DataFrame:
    df = _ensure_returns(series)
    last_dt = df["dt"].max()
    start = last_dt - pd.Timedelta(days=corr_window)
    w = df[df["dt"] >= start].copy()

    pivot = w.pivot_table(index="dt", columns="asset_id", values="return_1d", aggfunc="mean").sort_index()
    # top5 only (based on last snapshot market cap ordering in series doesn't guarantee; use available columns)
    cols = list(pivot.columns)
    if not cols:
        return pd.DataFrame()
    corr = pivot[cols].corr()
    return corr


def cumulative_return(prices: pd.Series) -> float:
    prices = prices.dropna()
    if len(prices) < 2:
        return float("nan")
    return (prices.iloc[-1] / prices.iloc[0]) - 1.0


def max_drawdown(prices: pd.Series) -> float:
    prices = prices.dropna()
    if len(prices) < 2:
        return float("nan")
    roll_max = prices.cummax()
    dd = (prices / roll_max) - 1.0
    return float(dd.min())


def build_perf_and_risk_tables(series: pd.DataFrame, window_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = series.copy().sort_values("dt")
    last_dt = df["dt"].max()

    windows = {"ret_7d": 7, "ret_30d": 30, "ret_90d": 90}
    perf_rows = []
    risk_rows = []

    for asset_id, g in df.groupby("asset_id"):
        g = g.sort_values("dt")
        row = {"asset_id": asset_id}
        for k, w in windows.items():
            start = last_dt - pd.Timedelta(days=w)
            gw = g[g["dt"] >= start]
            row[k] = cumulative_return(gw["price_usd"])
        perf_rows.append(row)

        # risk metrics
        dd = max_drawdown(g["price_usd"])
        # last vol_30d
        last_vol = g.dropna(subset=["vol_30d"]).tail(1)
        vol30 = float(last_vol["vol_30d"].iloc[0]) if not last_vol.empty else float("nan")
        ret30 = row["ret_30d"]
        sharpe = (ret30 / vol30) if (pd.notna(ret30) and pd.notna(vol30) and vol30 != 0) else float("nan")
        risk_rows.append({"asset_id": asset_id, "sharpe_30d": sharpe, "max_dd": dd})

    perf_df = pd.DataFrame(perf_rows).sort_values("ret_30d", ascending=False)
    risk_df = pd.DataFrame(risk_rows).sort_values("sharpe_30d", ascending=False)
    return perf_df, risk_df


def make_charts(series: pd.DataFrame, corr_vs_btc: pd.DataFrame, corr_matrix: pd.DataFrame, perf_df: pd.DataFrame, risk_df: pd.DataFrame, corr_window: int) -> dict[str, str]:
    _ensure_dirs()
    charts: dict[str, str] = {}

    # Chart 1: preços normalizados
    fig = plt.figure()
    for asset_id, g in series.groupby("asset_id"):
        g = g.sort_values("dt")
        base = g["price_usd"].iloc[0]
        if pd.notna(base) and base != 0:
            plt.plot(g["dt"], (g["price_usd"] / base) * 100, label=asset_id)
    plt.title("Preços Normalizados (base=100 no início da janela)")
    plt.xlabel("Data")
    plt.ylabel("Índice")
    plt.legend()

    ax = plt.gca()
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.autofmt_xdate(rotation=0)

    p1 = CHART_DIR / "normalized_prices.png"
    fig.tight_layout()
    fig.savefig(p1, dpi=140)
    plt.close(fig)
    charts["normalized_prices"] = f"charts/{p1.name}"

    # Chart 2: vol 30d (último valor)
    fig = plt.figure()
    last_vol = series.sort_values("dt").groupby("asset_id").tail(1)
    plt.bar(last_vol["asset_id"], last_vol["vol_30d"])
    plt.title("Volatilidade 30d (último valor disponível)")
    plt.xlabel("Ativo")
    plt.ylabel("Vol (std dos retornos)")
    p2 = CHART_DIR / "vol_30d.png"
    fig.tight_layout()
    fig.savefig(p2, dpi=140)
    plt.close(fig)
    charts["vol_30d"] = f"charts/{p2.name}"

    # Chart 3: correlação vs BTC
    fig = plt.figure()
    plot_df = corr_vs_btc.dropna().sort_values("corr_btc")
    if not plot_df.empty:
        plt.bar(plot_df["asset_id"], plot_df["corr_btc"])
    plt.title(f"Correlação de retornos vs BTC ({corr_window}d)")
    plt.xlabel("Ativo")
    plt.ylabel("Correlação")
    p3 = CHART_DIR / "corr_btc.png"
    fig.tight_layout()
    fig.savefig(p3, dpi=140)
    plt.close(fig)
    charts["corr_btc"] = f"charts/{p3.name}"

    # Chart 4: heatmap correlação top5
    fig = plt.figure()
    if corr_matrix is not None and not corr_matrix.empty:
        mat = corr_matrix.values
        plt.imshow(mat, vmin=-1, vmax=1, aspect="auto")
        plt.colorbar()
        labels = [c.upper() for c in corr_matrix.columns]
        plt.xticks(range(len(labels)), labels, rotation=45, ha="right")
        plt.yticks(range(len(labels)), labels)
        plt.title(f"Heatmap de correlação (retornos, {corr_window}d)")
    else:
        plt.text(0.5, 0.5, "Sem dados", ha="center", va="center")
        plt.axis("off")
    p4 = CHART_DIR / "corr_heatmap.png"
    fig.tight_layout()
    fig.savefig(p4, dpi=140)
    plt.close(fig)
    charts["corr_heatmap"] = f"charts/{p4.name}"

    # Chart 5: risco x retorno 30d
    fig = plt.figure()
    # Build from perf ret_30d and vol30 last
    rr = []
    last = series.sort_values("dt").groupby("asset_id").tail(1)[["asset_id","vol_30d"]]
    merged = perf_df[["asset_id","ret_30d"]].merge(last, on="asset_id", how="left")
    for _, r in merged.iterrows():
        if pd.isna(r["ret_30d"]) or pd.isna(r["vol_30d"]):
            continue
        rr.append((r["asset_id"], float(r["vol_30d"]), float(r["ret_30d"])))
    if rr:
        xs = [x[1] for x in rr]
        ys = [x[2] for x in rr]
        plt.scatter(xs, ys)
        for a, x, y in rr:
            plt.annotate(a.upper(), (x, y), textcoords="offset points", xytext=(6, 4))
        plt.title("Risco x Retorno (30d)")
        plt.xlabel("Vol 30d (std)")
        plt.ylabel("Retorno 30d")
    else:
        plt.text(0.5, 0.5, "Sem dados", ha="center", va="center")
        plt.axis("off")
    p5 = CHART_DIR / "risk_return.png"
    fig.tight_layout()
    fig.savefig(p5, dpi=140)
    plt.close(fig)
    charts["risk_return"] = f"charts/{p5.name}"

    return charts


def auto_commentary(snapshot: pd.DataFrame, series: pd.DataFrame, corr_vs_btc: pd.DataFrame, perf_df: pd.DataFrame, risk_df: pd.DataFrame, perf_window: int = 30) -> tuple[list[str], dict[str, str]]:
    comments: list[str] = []
    kpis: dict[str, str] = {}

    # Best/Worst performance window (already in perf_df ret_30d, but keep perf_window generic)
    # We'll compute based on perf_window using series
    last_dt = series["dt"].max()
    start = last_dt - pd.Timedelta(days=perf_window)
    w = series[series["dt"] >= start].copy()

    perf = []
    for asset_id, g in w.groupby("asset_id"):
        g = g.sort_values("dt")
        if g["price_usd"].notna().sum() < 2:
            continue
        ret = (g["price_usd"].iloc[-1] / g["price_usd"].iloc[0]) - 1
        perf.append((asset_id, ret))
    if perf:
        best_asset, best_ret = sorted(perf, key=lambda x: x[1], reverse=True)[0]
        worst_asset, worst_ret = sorted(perf, key=lambda x: x[1])[0]
        kpis["best_asset"] = best_asset
        kpis["best_return"] = f"{best_ret*100:.2f}% em {perf_window}d"
        comments.append(f"{best_asset.upper()} lidera o desempenho na janela de {perf_window} dias ({best_ret*100:.2f}%).")
        comments.append(f"{worst_asset.upper()} é o pior desempenho na janela de {perf_window} dias ({worst_ret*100:.2f}%).")
    else:
        kpis["best_asset"] = "-"
        kpis["best_return"] = "-"

    # Most volatile 30d by snapshot
    snap_last = snapshot.dropna(subset=["vol_30d"]).copy()
    if not snap_last.empty:
        row = snap_last.sort_values("vol_30d", ascending=False).iloc[0]
        kpis["riskiest_asset"] = row["asset_id"]
        kpis["riskiest_vol"] = f"{row['vol_30d']*100:.2f}% (std 30d)"
        comments.append(f"{row['asset_id'].upper()} apresenta a maior volatilidade 30d no snapshot ({row['vol_30d']*100:.2f}%).")
    else:
        kpis["riskiest_asset"] = "-"
        kpis["riskiest_vol"] = "-"

    # Correlation notes
    if not corr_vs_btc.empty and corr_vs_btc["corr_btc"].notna().any():
        hi = corr_vs_btc.sort_values("corr_btc", ascending=False).iloc[0]
        lo = corr_vs_btc.sort_values("corr_btc", ascending=True).iloc[0]
        comments.append(f"Maior correlação vs BTC: {hi['asset_id'].upper()} (corr={hi['corr_btc']:.2f}).")
        comments.append(f"Menor correlação vs BTC: {lo['asset_id'].upper()} (corr={lo['corr_btc']:.2f}).")

    # Liquidity highlight
    if "volume_24h_usd" in snapshot.columns and "market_cap_usd" in snapshot.columns:
        liq = snapshot.assign(liq=lambda d: d["volume_24h_usd"] / d["market_cap_usd"]).dropna(subset=["liq"])
        if not liq.empty:
            top_liq = liq.sort_values("liq", ascending=False).iloc[0]
            comments.append(f"Maior giro (Volume/MarketCap): {top_liq['asset_id'].upper()} ({top_liq['liq']*100:.2f}%).")

    # Risk-adjusted leader (Sharpe)
    rk = risk_df.dropna(subset=["sharpe_30d"]).copy()
    if not rk.empty:
        best_sh = rk.sort_values("sharpe_30d", ascending=False).iloc[0]
        comments.append(f"Melhor ajuste ao risco (Sharpe 30d simplificado): {best_sh['asset_id'].upper()} ({best_sh['sharpe_30d']:.2f}).")

    # BTC dominance
    btc_dom = "—"
    if (snapshot["asset_id"] == "bitcoin").any():
        btc_m = float(snapshot.loc[snapshot["asset_id"]=="bitcoin","market_cap_usd"].iloc[0])
        total = float(snapshot["market_cap_usd"].sum())
        if total > 0:
            btc_dom = f"{(btc_m/total)*100:.1f}%"
            comments.append(f"BTC representa {btc_dom} do Market Cap do grupo Top 5 no snapshot.")
    kpis["btc_dominance"] = btc_dom

    # Stablecoin hint
    if (snapshot["asset_id"] == "tether").any():
        comments.append("Stablecoin (USDT) tende a apresentar baixa volatilidade/retorno; útil como referência de estabilidade.")

    return comments, kpis


def render_html(ctx: ReportContext) -> str:
    _ensure_dirs()
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html")

    snap = ctx.snapshot.copy()
    snap = snap.merge(ctx.corr_vs_btc, on="asset_id", how="left")

    # liquidity ratio
    snap["liq_ratio"] = snap["volume_24h_usd"] / snap["market_cap_usd"]

    def fmt_money0(x):
        try:
            return f"${x:,.0f}"
        except Exception:
            return ""

    snap_display = snap.copy()
    snap_display["price_usd"] = snap_display["price_usd"].map(lambda x: f"${x:,.2f}" if pd.notna(x) else "")
    snap_display["market_cap_usd"] = snap_display["market_cap_usd"].map(fmt_money0)
    snap_display["volume_24h_usd"] = snap_display["volume_24h_usd"].map(fmt_money0)
    snap_display["return_1d"] = snap_display["return_1d"].map(lambda x: f"{x*100:.2f}%" if pd.notna(x) else "")
    snap_display["vol_30d"] = snap_display["vol_30d"].map(lambda x: f"{x*100:.2f}%" if pd.notna(x) else "")
    snap_display["corr_btc"] = snap_display["corr_btc"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
    snap_display["liq_ratio"] = snap_display["liq_ratio"].map(lambda x: f"{x*100:.2f}%" if pd.notna(x) else "-")

    # perf & risk display
    perf_disp = ctx.perf_table.copy()
    for col, w in [("ret_7d",7),("ret_30d",30),("ret_90d",90)]:
        perf_disp[col] = perf_disp[col].map(lambda x: f"{x*100:.2f}%" if pd.notna(x) else "-")

    risk_disp = ctx.risk_table.copy()
    risk_disp["sharpe_30d"] = risk_disp["sharpe_30d"].map(lambda x: f"{x:.2f}" if pd.notna(x) else "-")
    risk_disp["max_dd"] = risk_disp["max_dd"].map(lambda x: f"{x*100:.2f}%" if pd.notna(x) else "-")

    css_path = "assets/css/report.css"
    logo_path = "assets/images/logo.svg"

    html = template.render(
        title=settings.report_title,
        as_of=ctx.last_dt,
        window_days=ctx.window_days,
        perf_window=ctx.perf_window,
        corr_window=ctx.corr_window,
        snapshot_table=snap_display.to_dict(orient="records"),
        perf_table=perf_disp.to_dict(orient="records"),
        risk_table=risk_disp.to_dict(orient="records"),
        charts=ctx.charts,
        commentary=ctx.commentary,
        kpis=ctx.kpis,
        notes=[
            "Fonte: CoinGecko (dados públicos).",
            "Retornos acumulados calculados a partir de preços diários (close aproximado via endpoint de market_chart).",
            "Vol 30d = desvio padrão dos retornos diários em janela móvel (mín. 10 observações).",
            f"Correlação vs BTC e heatmap calculados sobre retornos diários na janela de {ctx.corr_window} dias.",
            "Sharpe 30d simplificado = retorno acumulado 30d / vol 30d (sem taxa livre de risco).",
            "Liq (V/MCap) = Volume 24h dividido por Market Cap (proxy simples de giro/liquidez).",
            "Max DD = maior drawdown na janela exibida.",
        ],
        css_path=css_path,
        logo_path=logo_path,
    )

    out_path = REPORT_DIR / "report.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)


def render_pdf(ctx: ReportContext) -> str:
    _ensure_dirs()
    out_path = REPORT_DIR / "report.pdf"

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="H1x", fontSize=16, leading=20, spaceAfter=10))
    styles.add(ParagraphStyle(name="Muted", fontSize=9.5, leading=12, textColor=colors.HexColor("#475569")))
    styles.add(ParagraphStyle(name="Bodyx", fontSize=10.5, leading=14))

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=1.6*cm,
        rightMargin=1.6*cm,
        topMargin=1.4*cm,
        bottomMargin=1.4*cm,
        title=settings.report_title,
    )

    story = []
    story.append(Paragraph(settings.report_title, styles["H1x"]))
    story.append(Paragraph(f"As of: {ctx.last_dt} (UTC) • Janela: {ctx.window_days}d • Corr: {ctx.corr_window}d", styles["Muted"]))
    story.append(Spacer(1, 10))

    # Snapshot table
    snap = ctx.snapshot.copy().merge(ctx.corr_vs_btc, on="asset_id", how="left")
    snap["liq_ratio"] = snap["volume_24h_usd"] / snap["market_cap_usd"]

    def m0(x):
        try: return f"${x:,.0f}"
        except: return ""
    def m2(x):
        try: return f"${x:,.2f}"
        except: return ""
    def p2(x):
        try: return f"{x*100:.2f}%"
        except: return ""
    def c2(x):
        try: return f"{x:.2f}"
        except: return "-"

    table_data = [[
        "Ativo", "Preço", "Market Cap", "Vol 24h", "Ret 1d", "Vol 30d", "Liq", f"Corr BTC\n({ctx.corr_window}d)"
    ]]
    for _, r in snap.iterrows():
        table_data.append([
            f"{r['name']} ({r['symbol']})",
            m2(r["price_usd"]),
            m0(r["market_cap_usd"]),
            m0(r["volume_24h_usd"]),
            p2(r["return_1d"]),
            p2(r["vol_30d"]),
            f"{r['liq_ratio']*100:.2f}%" if pd.notna(r["liq_ratio"]) else "-",
            c2(r.get("corr_btc")),
        ])

    t = Table(table_data, colWidths=[4.1*cm, 2.0*cm, 2.6*cm, 2.6*cm, 1.6*cm, 1.6*cm, 1.5*cm, 2.0*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B1220")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9.0),
        ("ALIGN", (1,0), (-1,0), "CENTER"),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("ALIGN", (0,0), (0,-1), "LEFT"),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.HexColor("#F8FAFC")]),
        ("FONTSIZE", (0,1), (-1,-1), 8.6),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(Paragraph("Resumo (Snapshot)", styles["Bodyx"]))
    story.append(Spacer(1, 6))
    story.append(t)
    story.append(Spacer(1, 10))

    story.append(Paragraph(f"Dominância BTC (Top 5): <b>{ctx.kpis.get('btc_dominance','—')}</b>", styles["Bodyx"]))
    story.append(Spacer(1, 10))

    story.append(Paragraph("Comentários automáticos", styles["Bodyx"]))
    for c in ctx.commentary:
        story.append(Paragraph(f"• {c}", styles["Bodyx"]))
    story.append(Spacer(1, 10))

    # Performance & Risk tables (compact)
    perf = ctx.perf_table.copy()
    risk = ctx.risk_table.copy()

    perf_data = [["Ativo", "Ret 7d", "Ret 30d", "Ret 90d"]]
    for _, r in perf.iterrows():
        perf_data.append([
            r["asset_id"].upper(),
            p2(r["ret_7d"]),
            p2(r["ret_30d"]),
            p2(r["ret_90d"]),
        ])
    tp = Table(perf_data, colWidths=[3.0*cm, 2.2*cm, 2.2*cm, 2.2*cm])
    tp.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B1220")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9.0),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.HexColor("#F8FAFC")]),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("FONTSIZE", (0,1), (-1,-1), 8.6),
    ]))

    risk_data = [["Ativo", "Sharpe 30d*", f"Max DD ({ctx.window_days}d)"]]
    for _, r in risk.iterrows():
        risk_data.append([
            r["asset_id"].upper(),
            f"{r['sharpe_30d']:.2f}" if pd.notna(r["sharpe_30d"]) else "-",
            f"{r['max_dd']*100:.2f}%" if pd.notna(r["max_dd"]) else "-",
        ])
    tr = Table(risk_data, colWidths=[3.0*cm, 2.6*cm, 3.0*cm])
    tr.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B1220")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9.0),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.HexColor("#F8FAFC")]),
        ("ALIGN", (1,1), (-1,-1), "RIGHT"),
        ("FONTSIZE", (0,1), (-1,-1), 8.6),
    ]))

    story.append(Paragraph("Performance & Risco", styles["Bodyx"]))
    story.append(Spacer(1, 6))
    story.append(tp)
    story.append(Spacer(1, 10))
    story.append(tr)
    story.append(Spacer(1, 8))
    story.append(Paragraph("* Sharpe simplificado = Ret 30d / Vol 30d (sem taxa livre de risco).", styles["Muted"]))

    story.append(PageBreak())

    # Charts page
    def add_chart(rel_path: str, title: str, height_cm: float = 8.8):
        story.append(Paragraph(title, styles["Bodyx"]))
        fp = (REPORT_DIR / rel_path).resolve()
        if fp.exists():
            story.append(Spacer(1, 6))
            story.append(RLImage(str(fp), width=17.2*cm, height=height_cm*cm))
            story.append(Spacer(1, 10))
        else:
            story.append(Paragraph("(gráfico não encontrado)", styles["Muted"]))
            story.append(Spacer(1, 8))

    add_chart(ctx.charts["normalized_prices"], "Preços Normalizados", height_cm=8.6)
    add_chart(ctx.charts["vol_30d"], "Volatilidade 30d", height_cm=8.0)
    add_chart(ctx.charts["corr_btc"], f"Correlação vs BTC ({ctx.corr_window}d)", height_cm=8.0)
    add_chart(ctx.charts["corr_heatmap"], f"Heatmap de correlação ({ctx.corr_window}d)", height_cm=8.0)
    add_chart(ctx.charts["risk_return"], "Risco x Retorno (30d)", height_cm=8.0)

    story.append(Paragraph("Notas metodológicas", styles["Bodyx"]))
    for n in [
        "Fonte: CoinGecko (dados públicos).",
        "Vol 30d = desvio padrão dos retornos diários em janela móvel.",
        f"Correlação calculada sobre retornos diários na janela de {ctx.corr_window} dias.",
        "Top 5 é recalculado no momento da extração e pode variar.",
    ]:
        story.append(Paragraph(f"• {n}", styles["Bodyx"]))

    doc.build(story)
    return str(out_path)


def build_report(engine, window_days: int = 120, perf_window: int = 30, corr_window: int = 60, make_pdf: bool = True) -> tuple[str, str | None]:
    last_dt, snap, series = query_for_report(engine, window_days=window_days)

    corr_vs_btc = compute_corr_vs_btc(series, corr_window=corr_window)
    corr_matrix = compute_corr_matrix(series, corr_window=corr_window)
    perf_df, risk_df = build_perf_and_risk_tables(series, window_days=window_days)

    commentary, kpis = auto_commentary(snap, series, corr_vs_btc, perf_df, risk_df, perf_window=perf_window)
    charts = make_charts(series, corr_vs_btc, corr_matrix, perf_df, risk_df, corr_window=corr_window)

    ctx = ReportContext(
        last_dt=last_dt,
        window_days=window_days,
        perf_window=perf_window,
        corr_window=corr_window,
        snapshot=snap,
        series=series,
        corr_vs_btc=corr_vs_btc,
        corr_matrix=corr_matrix,
        perf_table=perf_df,
        risk_table=risk_df,
        commentary=commentary,
        kpis=kpis,
        charts=charts,
    )

    html_path = render_html(ctx)
    pdf_path = render_pdf(ctx) if make_pdf else None
    return html_path, pdf_path
