from __future__ import annotations

from pathlib import Path
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, Date, Float, UniqueConstraint, Index
)
from sqlalchemy.engine import Engine
from sqlalchemy.sql import text

from .config import settings

metadata = MetaData()

assets = Table(
    "assets",
    metadata,
    Column("asset_id", String, primary_key=True),
    Column("symbol", String, nullable=False),
    Column("name", String, nullable=False),
)

market_daily = Table(
    "market_daily",
    metadata,
    Column("dt", Date, nullable=False),
    Column("asset_id", String, nullable=False),
    Column("price_usd", Float, nullable=True),
    Column("market_cap_usd", Float, nullable=True),
    Column("volume_24h_usd", Float, nullable=True),
    Column("return_1d", Float, nullable=True),
    Column("vol_30d", Float, nullable=True),
    UniqueConstraint("dt", "asset_id", name="uq_market_daily_dt_asset"),
)

Index("ix_market_daily_asset_dt", market_daily.c.asset_id, market_daily.c.dt)


def get_engine() -> Engine:
    # garante que a pasta exista quando usar sqlite em arquivo
    if settings.db_url.startswith("sqlite:///"):
        db_file = settings.db_url.replace("sqlite:///", "")
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(settings.db_url, future=True)


def init_db(engine: Engine | None = None) -> None:
    eng = engine or get_engine()
    metadata.create_all(eng)


def upsert_assets(engine: Engine, rows: list[dict]) -> None:
    stmt = text("""
        INSERT INTO assets (asset_id, symbol, name)
        VALUES (:asset_id, :symbol, :name)
        ON CONFLICT(asset_id) DO UPDATE SET
            symbol = excluded.symbol,
            name   = excluded.name
    """)
    with engine.begin() as conn:
        conn.execute(stmt, rows)


def upsert_market_daily(engine: Engine, rows: list[dict]) -> None:
    stmt = text("""
        INSERT INTO market_daily (dt, asset_id, price_usd, market_cap_usd, volume_24h_usd, return_1d, vol_30d)
        VALUES (:dt, :asset_id, :price_usd, :market_cap_usd, :volume_24h_usd, :return_1d, :vol_30d)
        ON CONFLICT(dt, asset_id) DO UPDATE SET
            price_usd      = excluded.price_usd,
            market_cap_usd = excluded.market_cap_usd,
            volume_24h_usd = excluded.volume_24h_usd,
            return_1d      = excluded.return_1d,
            vol_30d        = excluded.vol_30d
    """)
    with engine.begin() as conn:
        conn.execute(stmt, rows)
