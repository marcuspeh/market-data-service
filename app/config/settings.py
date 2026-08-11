from functools import lru_cache
from urllib.parse import quote_plus
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database (individual MySQL fields)
    mysql_host: str = Field(default="mysql")
    mysql_port: int = Field(default=3306)
    mysql_user: str = Field(default="")
    mysql_password: str = Field(default="")
    mysql_database: str = Field(default="")

    # Polygon.io
    polygon_api_key: str = Field(default="")
    polygon_base_url: str = Field(default="https://api.polygon.io")

    # Interactive Brokers (via ib-gateway-docker)
    # ib-gateway-docker maps container ports 4003=live, 4004=paper to the
    # host as 4001 and 4002 respectively. When running inside docker-compose
    # we connect to the service name ("ib-gateway") on its container port.
    ibkr_host: str = Field(default="ib-gateway")
    ibkr_trading_mode: str = Field(default="paper")  # "paper" | "live"
    ibkr_port_paper: int = Field(default=4004)
    ibkr_port_live: int = Field(default=4003)
    ibkr_client_id: int = Field(default=1)
    ibkr_timeout_seconds: float = Field(default=10.0)

    @property
    def ibkr_port(self) -> int:
        """Resolve the actual TWS API port based on the trading mode."""
        return (
            self.ibkr_port_live
            if self.ibkr_trading_mode == "live"
            else self.ibkr_port_paper
        )

    @property
    def database_url(self) -> str:
        # URL-encode credentials so passwords containing @, :, /, etc. don't
        # break the DSN.
        user = quote_plus(self.mysql_user)
        password = quote_plus(self.mysql_password)
        return (
            f"mysql://{user}:{password}@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()