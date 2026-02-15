from __future__ import annotations

import pandas as pd

def add_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Espera colunas: dt, asset_id, price_usd, market_cap_usd, volume_24h_usd
    Retorna com: return_1d, vol_30d
    """
    out = df.copy()
    out["dt"] = pd.to_datetime(out["dt"])
    out = out.sort_values(["asset_id","dt"])

    # retorno diário
    out["return_1d"] = out.groupby("asset_id")["price_usd"].pct_change()

    # vol 30d (desvio padrão dos retornos em janela)
    out["vol_30d"] = (
        out.groupby("asset_id")["return_1d"]
           .rolling(window=30, min_periods=10)
           .std()
           .reset_index(level=0, drop=True)
    )

    out["dt"] = out["dt"].dt.date
    return out
