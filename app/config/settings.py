from functools import lru_cache
from pathlib import Path

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


@lru_cache
def get_settings() -> Settings:
    return Settings()