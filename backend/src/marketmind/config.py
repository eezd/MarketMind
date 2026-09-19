from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MM_", extra="ignore", hide_input_in_errors=True)

    database_url: SecretStr
    redis_url: SecretStr
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    cookie_secure: bool = True
    session_hours: int = Field(default=12, ge=1, le=168)
    ai_base_url: str | None = None
    ai_api_key: SecretStr | None = None
    ai_model: str = ""
    ai_timeout_seconds: int = Field(default=30, ge=5, le=120)
    ai_reasoning_effort: Literal["disabled", "minimal", "low", "medium", "high"] = "medium"
    binance_proxy_url: SecretStr = SecretStr("socks5://127.0.0.1:12123")
    binance_api_key: SecretStr | None = None
    binance_rest_base_url: str = "https://api.binance.com"
    binance_spot_ws_url: str = "wss://stream.binance.com:9443"
    binance_stocks_ws_url: str = "wss://nbstream.binance.com/equity"
    binance_crypto_symbols: str = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT"
    binance_stock_symbols: str = "AAPL,NVDA,TSLA,SPY,QQQ"
    binance_sync_interval_seconds: int = Field(default=60, ge=10, le=3600)

    @field_validator("binance_proxy_url")
    @classmethod
    def socks_proxy(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in {"socks5", "socks5h"} or not parsed.hostname or not parsed.port:
            raise ValueError("MM_BINANCE_PROXY_URL must be a SOCKS5 URL with host and port")
        return value

    @field_validator("binance_crypto_symbols", "binance_stock_symbols")
    @classmethod
    def valid_market_symbols(cls, value: str) -> str:
        symbols = [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]
        if not symbols or any(not symbol.isalnum() for symbol in symbols):
            raise ValueError("Binance symbols must be comma-separated letters and numbers")
        return ",".join(dict.fromkeys(symbols))

    @property
    def crypto_symbols(self) -> tuple[str, ...]:
        return tuple(self.binance_crypto_symbols.split(","))

    @property
    def stock_symbols(self) -> tuple[str, ...]:
        return tuple(self.binance_stock_symbols.split(","))

    @field_validator("database_url")
    @classmethod
    def postgres_driver(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("MM_DATABASE_URL must use postgresql+psycopg")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def valid_origins(cls, value: str) -> str:
        origins = [origin.strip() for origin in value.split(",")]
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("MM_ALLOWED_ORIGINS must contain exact HTTP(S) origins")
        return ",".join(origins)

    @property
    def origins(self) -> frozenset[str]:
        return frozenset(self.allowed_origins.split(","))


@lru_cache
def get_settings() -> Settings:
    return Settings()
