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

    # Longbridge OpenAPI: credentials loaded via LONGBRIDGE_APP_KEY/SECRET/ACCESS_TOKEN.
    longbridge_timeout_seconds: float = Field(default=10.0)
    longbridge_region_suffix: str = Field(default=".US")

    @property
    def market_data_dir(self) -> Path:
        return Path(self.data_dir) / "market"

    @property
    def constituents_dir(self) -> Path:
        return Path(self.data_dir) / "constituents"

    @staticmethod
    def now_ny_date() -> date:
        """Today on the US/Eastern calendar — the date that owns the live bar."""
        return ny_now().date()


def ny_now() -> datetime:
    """Wall-clock time in US/Eastern."""
    if NY_TZ is None:  # pragma: no cover — only on Python <3.9
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.now(NY_TZ)


def ny_from_ts(ts_ms: int) -> date:
    """ET calendar date for an epoch-ms timestamp."""
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