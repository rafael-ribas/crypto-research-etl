from __future__ import annotations

import time
import random
import requests
import pandas as pd

from .config import settings


class CoinGeckoClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = 30,
        max_retries: int = 8,
        base_sleep: float = 1.2,
    ):
        self.base_url = (base_url or settings.coingecko_base).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_sleep = base_sleep

    def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        # Se o servidor informar Retry-After, espera
        if retry_after:
            try:
                wait = float(retry_after)
                time.sleep(max(0.0, wait))
                return
            except Exception:
                pass
        # Exponential backoff com jitter
        wait = min(60.0, (self.base_sleep * (2 ** attempt)) + random.random())
        time.sleep(wait)

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {
            "accept": "application/json",
            "user-agent": "crypto-research-etl/0.1 (+https://github.com/your-handle)",
        }

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.get(url, params=params or {}, timeout=self.timeout, headers=headers)

                # Rate limit
                if r.status_code == 429:
                    self._sleep_backoff(attempt, r.headers.get("Retry-After"))
                    continue

                # Transientes comuns
                if r.status_code in (500, 502, 503, 504):
                    self._sleep_backoff(attempt, r.headers.get("Retry-After"))
                    continue

                r.raise_for_status()
                return r.json()

            except requests.RequestException as e:
                last_exc = e
                # backoff para falhas de rede
                self._sleep_backoff(attempt, None)

        # se estourou retries
        raise last_exc if last_exc else RuntimeError(f"Falha ao GET {url}")

    def top_marketcap(self, n: int = 5, vs_currency: str = "usd") -> pd.DataFrame:
        data = self._get(
            "/coins/markets",
            params={
                "vs_currency": vs_currency,
                "order": "market_cap_desc",
                "per_page": n,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
        )
        df = pd.DataFrame(data)
        cols = ["id", "symbol", "name", "current_price", "market_cap", "total_volume", "last_updated"]
        df = df[cols].rename(
            columns={
                "id": "asset_id",
                "current_price": "price_usd",
                "market_cap": "market_cap_usd",
                "total_volume": "volume_24h_usd",
            }
        )
        df["symbol"] = df["symbol"].str.lower()
        df["last_updated"] = pd.to_datetime(df["last_updated"], utc=True, errors="coerce")
        df["dt"] = df["last_updated"].dt.date
        return df[["dt", "asset_id", "symbol", "name", "price_usd", "market_cap_usd", "volume_24h_usd"]]

    def market_chart_daily(self, asset_id: str, vs_currency: str = "usd", days: int = 120) -> pd.DataFrame:
        data = self._get(
            f"/coins/{asset_id}/market_chart",
            params={"vs_currency": vs_currency, "days": days, "interval": "daily"},
        )
        prices = pd.DataFrame(data.get("prices", []), columns=["ts_ms", "price_usd"])
        mcap = pd.DataFrame(data.get("market_caps", []), columns=["ts_ms", "market_cap_usd"])
        vol = pd.DataFrame(data.get("total_volumes", []), columns=["ts_ms", "volume_24h_usd"])

        df = prices.merge(mcap, on="ts_ms", how="outer").merge(vol, on="ts_ms", how="outer")
        df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True, errors="coerce")
        df["dt"] = df["ts"].dt.date
        df = df.drop(columns=["ts_ms", "ts"]).dropna(subset=["dt"]).sort_values("dt")
        df["asset_id"] = asset_id
        return df[["dt", "asset_id", "price_usd", "market_cap_usd", "volume_24h_usd"]]


def extract_top5_and_history(days: int = 120, per_coin_pause: float = 1.0) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    client = CoinGeckoClient()
    top5 = client.top_marketcap(n=5)
    history: dict[str, pd.DataFrame] = {}

    for asset_id in top5["asset_id"].tolist():
        # Pausa entre moedas para reduzir 429 (além do backoff automático)
        time.sleep(per_coin_pause)
        history[asset_id] = client.market_chart_daily(asset_id=asset_id, days=days)

    return top5, history
