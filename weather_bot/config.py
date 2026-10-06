"""Runtime configuration. Every threshold is env-overridable; secrets never print."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

KALSHI_DAILY_HIGH_SERIES: tuple[str, ...] = (
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHLAX",
    "KXHIGHPHIL", "KXHIGHTHOU", "KXHIGHTDAL", "KXHIGHTATL", "KXHIGHTSEA", "KXHIGHTSFO",
    "KXHIGHTPHX", "KXHIGHTDC", "KXHIGHTBOS", "KXHIGHTEWR",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Switches
    live_trading: bool = False
    trading_enabled: bool = True
    polymarket_trading_until: dt.date | None = None

    # Strategy thresholds
    min_net_edge: float = 0.10
    min_range_probability: float = 0.70
    min_bins: int = 3
    max_bins: int = 6
    max_basket_cost_kalshi: float = 1.00
    max_basket_cost_polymarket: float = 5.00
    max_city_date_exposure: float = 5.00
    max_daily_new_risk: float = 5.00
    max_total_open_risk: float = 10.00
    balance_buffer: float = 1.00             # keep this much free cash on each venue
    max_slippage_per_bin: float = 0.01
    max_unwind_loss_per_leg: float = 0.03
    min_unwind_recovery: float = 0.6        # sell a filled leg at the bid only if bid >= 60% of what we paid
    min_hours_to_target: float = 8.0
    max_hours_to_target: float = 48.0

    # Quoting / execution
    taker_when_edge: bool = True          # lift all asks at once (FOK legs) when the taker basket still clears MIN_NET_EDGE
    min_resting_bid: float = 0.05         # legs priced below this are not rested; they are bought at completion
    bid_ttl_min: int = 30
    basket_complete_timeout_min: int = 0  # evaluate completion as soon as any leg fills
    unwind_ttl_min: int = 30

    # Refresh cadence
    nbm_poll_min: int = 10
    openmeteo_refresh_min: int = 60
    book_refresh_sec: int = 60

    # Probability model
    nbm_sigma_inflation: float = 1.15
    cli_window_delta_f: float = 0.0
    hourly_max_delta_f: float = -1.0
    max_nbp_sd_f: float = 4.0
    max_model_disagreement_f: float = 3.0
    max_nws_disagreement_f: float = 4.0
    max_market_disagreement_f: float = 2.5   # |market-implied mean − model mean|; larger → skip and log
    max_market_disagreement_c: float = 1.5
    intl_ensemble_bias_c: float = 0.9        # observed daily max − Open-Meteo day-1 max (16 intl airports, Oct 3–5 2026)
    max_intl_model_spread_c: float = 1.5     # max spread between ECMWF/GEFS/ICON ensemble means (no NBM abroad)
    openmeteo_extra_sigma_f: float = 1.0
    openmeteo_extra_sigma_c: float = 1.0     # residual sd of that bias check was ~1.4°C
    max_bulletin_age_h: float = 7.5

    # Venues / credentials
    kalshi_series: tuple[str, ...] = KALSHI_DAILY_HIGH_SERIES
    polymarket_international: bool = True       # trade non-US Polymarket cities (Celsius, Open-Meteo-only model)
    polymarket_intl_cities: tuple[str, ...] = ()  # empty = all cities that settle on weather.gov hourly data
    polymarket_cities: tuple[str, ...] = ("nyc", "chicago", "dallas", "denver", "houston", "atlanta", "seattle",
                                          "san-francisco", "los-angeles", "miami", "austin")
    kalshi_api_key: str | None = Field(default=None, repr=False)
    kalshi_private_key_path: Path = Path("kalshi.pk")
    kalshi_host: str = "https://api.elections.kalshi.com/trade-api/v2"
    poly_pk: str | None = Field(default=None, repr=False)
    poly_funder: str | None = Field(default=None, repr=False)
    poly_sig_type: int = 3
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"

    # Weather data
    openmeteo_api_key: str | None = Field(default=None, repr=False)
    nws_user_agent: str = "weather-bot-2 (contact: set NWS_USER_AGENT)"

    # Storage
    db_path: Path = PROJECT_ROOT / "data" / "weather_bot.sqlite"
    kill_file: Path = PROJECT_ROOT / "data" / ".trading_disabled"
    stations_cache: Path = PROJECT_ROOT / "data" / "stations_cache.json"

    @field_validator("polymarket_trading_until", mode="before")
    @classmethod
    def _parse_us_date(cls, v):
        if isinstance(v, str) and "/" in v:
            m, d, y = v.strip().split("/")
            return dt.date(int(y), int(m), int(d))
        return v

    def polymarket_allowed_on(self, day: dt.date) -> bool:
        return self.polymarket_trading_until is None or day <= self.polymarket_trading_until

    def max_market_disagreement(self, unit: str) -> float:
        return self.max_market_disagreement_c if unit == "C" else self.max_market_disagreement_f

    def max_basket_cost(self, venue: str) -> float:
        return {"kalshi": self.max_basket_cost_kalshi, "polymarket": self.max_basket_cost_polymarket}[venue]


def load_settings() -> Settings:
    return Settings()
