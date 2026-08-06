from functools import lru_cache
from urllib.parse import quote_plus
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
        
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