from functools import lru_cache
from pathlib import Path
from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo  # type: ignore[import-not-found]

    NY_TZ: ZoneInfo | None = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover — only on Python <3.9
    NY_TZ = None  # type: ignore[assignment]

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Layout: <data_dir>/market/<TICKER>/<YEAR>.parquet
    #         <data_dir>/constituents/<TICKER>.parquet
    data_dir: str = Field(default="./data")

    polygon_api_key: str = Field(default="")
    polygon_base_url: str = Field(default="https://api.polygon.io")

    # Paper trading is the only mode supported in this deployment;
    # the host/port/client_id below are fixed and not configurable.
    ibkr_host: str = Field(default="127.0.0.1")
    ibkr_trading_mode: str = Field(default="paper")  # "paper" | "live"
    ibkr_port_paper: int = Field(default=4004)
    ibkr_port_live: int = Field(default=4003)
    ibkr_client_id: int = Field(default=1)
    ibkr_timeout_seconds: float = Field(default=10.0)

    @property
    def market_data_dir(self) -> Path:
        return Path(self.data_dir) / "market"

    @property
    def constituents_dir(self) -> Path:
        return Path(self.data_dir) / "constituents"

    @property
    def ibkr_port(self) -> int:
        """Resolve the actual TWS API port based on the trading mode."""
        return (
            self.ibkr_port_live
            if self.ibkr_trading_mode == "live"
            else self.ibkr_port_paper
        )

    @staticmethod
    def now_ny_date() -> date:
        """Today on the US/Eastern calendar — the date that owns the live bar.

        The service uses the Nasdaq trading day as "today" (the day
        whose bar is served live by IBKR). At midnight UTC the US market
        is already on the next calendar date, but the live bar for that
        US session has only just begun — so we always reason in ET.
        """
        return ny_now().date()


def ny_now() -> datetime:
    """Wall-clock time in US/Eastern.

    Used as the canonical "now" everywhere in this service: bar
    dates, cache keys, and scheduler triggers all reason in the
    Nasdaq trading calendar. Falls back to a fixed UTC-5 offset if
    ``zoneinfo`` is unavailable (Python <3.9).
    """
    if NY_TZ is None:  # pragma: no cover — only on Python <3.9
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.now(NY_TZ)


def ny_from_ts(ts_ms: int) -> date:
    """ET calendar date for an epoch-ms timestamp.

    Polygon's ``t`` is UTC midnight ms, but we store the parquet
    ``date`` column in ET. A bar published as 2026-08-22 00:30 UTC
    is the 2026-08-21 ET session and must be stored as 2026-08-21.
    """
    if NY_TZ is None:  # pragma: no cover — only on Python <3.9
        return datetime.utcfromtimestamp(ts_ms / 1000).date()
    return datetime.fromtimestamp(ts_ms / 1000, tz=NY_TZ).date()


def ny_midnight_ts(d: date) -> int:
    """Epoch-ms for NY-midnight on ``d``."""
    if NY_TZ is None:  # pragma: no cover — only on Python <3.9
        return int(
            datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
            * 1000
        )
    return int(
        datetime(d.year, d.month, d.day, tzinfo=NY_TZ)
        .astimezone(timezone.utc)
        .timestamp()
        * 1000
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()