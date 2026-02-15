from __future__ import annotations

import argparse
import pandas as pd

from .config import settings
from .db import get_engine, init_db, upsert_assets, upsert_market_daily
from .extract import extract_top5_and_history
from .transform import add_metrics
from .report import build_report


def run_etl(days: int, pause: float = 1.0) -> None:
    engine = get_engine()
    init_db(engine)

    top5, history = extract_top5_and_history(days=days, per_coin_pause=pause)

    asset_rows = top5[["asset_id", "symbol", "name"]].drop_duplicates().to_dict(orient="records")
    upsert_assets(engine, asset_rows)

    hist_df = pd.concat(history.values(), ignore_index=True)
    hist_df = add_metrics(hist_df)

    rows = hist_df.to_dict(orient="records")
    upsert_market_daily(engine, rows)

    print(f"ETL concluído. Ativos: {len(asset_rows)} | Linhas market_daily: {len(rows)}")


def run_report(window_days: int = 120, perf_window: int = 30, corr_window: int = 60, pdf: bool = True) -> None:
    engine = get_engine()
    html_path, pdf_path = build_report(
        engine,
        window_days=window_days,
        perf_window=perf_window,
        corr_window=corr_window,
        make_pdf=pdf,
    )
    print(f"Report HTML gerado em: {html_path}")
    if pdf_path:
        print(f"Report PDF gerado em: {pdf_path}")


def main():
    parser = argparse.ArgumentParser(prog="crypto-etl", description="Crypto Research ETL + Report (Top 5).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_etl = sub.add_parser("etl", help="Executa ETL (extract/transform/load).")
    p_etl.add_argument("--days", type=int, default=settings.default_days, help="Janela histórica (dias) para coleta diária.")
    p_etl.add_argument("--pause", type=float, default=1.0, help="Pausa (segundos) entre chamadas por moeda para reduzir rate-limit.")

    p_report = sub.add_parser("report", help="Gera report HTML (Jinja2) + gráficos + PDF.")
    p_report.add_argument("--window-days", type=int, default=120, help="Janela (dias) usada em gráficos e comentários.")
    p_report.add_argument("--perf-window", type=int, default=30, help="Janela (dias) para cálculo de melhor/pior performance.")
    p_report.add_argument("--corr-window", type=int, default=60, help="Janela (dias) para correlação vs BTC.")
    p_report.add_argument("--no-pdf", action="store_true", help="Desabilita geração de PDF.")

    p_all = sub.add_parser("all", help="Roda ETL e gera report.")
    p_all.add_argument("--days", type=int, default=settings.default_days, help="Janela histórica (dias) para coleta diária.")
    p_all.add_argument("--pause", type=float, default=1.0, help="Pausa (segundos) entre chamadas por moeda para reduzir rate-limit.")
    p_all.add_argument("--window-days", type=int, default=120, help="Janela (dias) usada em gráficos e comentários.")
    p_all.add_argument("--perf-window", type=int, default=30, help="Janela (dias) para cálculo de melhor/pior performance.")
    p_all.add_argument("--corr-window", type=int, default=60, help="Janela (dias) para correlação vs BTC.")
    p_all.add_argument("--no-pdf", action="store_true", help="Desabilita geração de PDF.")

    args = parser.parse_args()

    if args.cmd == "etl":
        run_etl(days=args.days, pause=args.pause)
    elif args.cmd == "report":
        run_report(
            window_days=args.window_days,
            perf_window=args.perf_window,
            corr_window=args.corr_window,
            pdf=(not args.no_pdf),
        )
    elif args.cmd == "all":
        run_etl(days=args.days, pause=args.pause)
        run_report(
            window_days=args.window_days,
            perf_window=args.perf_window,
            corr_window=args.corr_window,
            pdf=(not args.no_pdf),
        )


if __name__ == "__main__":
    main()
