from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    db_url: str = os.getenv("DB_URL", "sqlite:///data/crypto.db")
    coingecko_base: str = os.getenv("COINGECKO_BASE", "https://api.coingecko.com/api/v3")
    report_title: str = os.getenv("REPORT_TITLE", "Crypto Research Daily Report")

    # Projeto: dias de histórico diário para o ETL
    default_days: int = int(os.getenv("DEFAULT_DAYS", "120"))


settings = Settings()
