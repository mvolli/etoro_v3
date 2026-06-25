#!/usr/bin/env python3
"""Config loader for V3 — reads config/config.yaml + .env API keys."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# Project root = 3 levels up from this file (src/bot/config.py)
_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _load_env(path: Path) -> dict[str, str]:
    """Load key=value pairs from a .env file."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip().strip('"').strip("'")
    return result


@dataclass
class APIConfig:
    base_url: str = "https://public-api.etoro.com/api/v1"
    timeout_connect: int = 5
    timeout_read: int = 10
    retry_attempts: int = 3
    retry_wait_min: int = 2
    retry_wait_max: int = 10


@dataclass
class DBConfig:
    path: str = "data/trading.db"
    wal_mode: bool = True
    busy_timeout_ms: int = 5000

    @property
    def abs_path(self) -> Path:
        p = Path(self.path)
        return p if p.is_absolute() else _PROJECT_ROOT / p


@dataclass
class TradingConfig:
    max_positions: int = 21
    max_fragments_per_instrument: int = 3
    min_buy_usd: float = 50.0
    cash_target_min_pct: float = 15.0
    cash_target_max_pct: float = 30.0
    cash_emergency_pct: float = 10.0


@dataclass
class RegimeConfig:
    drawdown_soft_cb_pct: float = 4.0
    drawdown_recovery_pct: float = 2.0
    normal_upper_pct: float = 3.5


@dataclass
class SLConfig:
    default_pct: float = 3.0
    emergency_pct: float = 4.0
    warning_pct: float = 2.0


@dataclass
class SizingConfig:
    very_high_pct: float = 8.0
    high_pct: float = 7.0
    medium_pct: float = 6.0
    low_pct: float = 2.0


@dataclass
class DiscordConfig:
    main_channel: str = "1513971015108263957"
    trades_channel: str = "1514786489110630600"


@dataclass
class CacheConfig:
    instrument_map_ttl_hours: int = 24
    signal_ttl_minutes: int = 60


@dataclass
class MarketHoursConfig:
    open: str = "15:30"
    close: str = "22:00"
    timezone: str = "Europe/Berlin"


@dataclass
class Config:
    api: APIConfig = field(default_factory=APIConfig)
    db: DBConfig = field(default_factory=DBConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    sl: SLConfig = field(default_factory=SLConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    market_hours: MarketHoursConfig = field(default_factory=MarketHoursConfig)
    instrument_limits: dict[str, float] = field(default_factory=dict)

    # API credentials (loaded from .env, not config.yaml)
    api_key: str = ""
    user_key: str = ""
    discord_token: str = ""


def load_config(
    config_path: Optional[Path] = None,
    env_path: Optional[Path] = None,
) -> Config:
    """Load config from YAML + API keys from .env.

    Falls back to defaults if files don't exist.
    """
    cfg = Config()

    # ── Load YAML ───────────────────────────────────────────────────────────
    config_file = config_path or (_PROJECT_ROOT / "config" / "config.yaml")
    if config_file.exists():
        raw = yaml.safe_load(config_file.read_text()) or {}

        if "api" in raw:
            for k, v in raw["api"].items():
                if hasattr(cfg.api, k):
                    setattr(cfg.api, k, v)

        if "db" in raw:
            for k, v in raw["db"].items():
                if hasattr(cfg.db, k):
                    setattr(cfg.db, k, v)

        if "trading" in raw:
            for k, v in raw["trading"].items():
                if hasattr(cfg.trading, k):
                    setattr(cfg.trading, k, v)

        if "regime" in raw:
            for k, v in raw["regime"].items():
                if hasattr(cfg.regime, k):
                    setattr(cfg.regime, k, v)

        if "sl" in raw:
            for k, v in raw["sl"].items():
                if hasattr(cfg.sl, k):
                    setattr(cfg.sl, k, v)

        if "sizing" in raw:
            for k, v in raw["sizing"].items():
                if hasattr(cfg.sizing, k):
                    setattr(cfg.sizing, k, v)

        if "discord" in raw:
            for k, v in raw["discord"].items():
                if hasattr(cfg.discord, k):
                    setattr(cfg.discord, k, str(v))

        if "cache" in raw:
            for k, v in raw["cache"].items():
                if hasattr(cfg.cache, k):
                    setattr(cfg.cache, k, v)

        if "market_hours" in raw:
            for k, v in raw["market_hours"].items():
                if hasattr(cfg.market_hours, k):
                    setattr(cfg.market_hours, k, str(v))

        if "instrument_limits" in raw:
            cfg.instrument_limits = {
                str(k).upper(): float(v)
                for k, v in raw["instrument_limits"].items()
            }

    # ── Load API keys from .env ──────────────────────────────────────────────
    env_paths = [
        env_path,
        _PROJECT_ROOT / ".env",
        Path.home() / ".hermes" / ".env",
    ]
    for ep in env_paths:
        if ep and ep.exists():
            env = _load_env(ep)
            cfg.api_key = cfg.api_key or env.get("ETORO_API_KEY", "")
            cfg.user_key = cfg.user_key or env.get("ETORO_USER_KEY", "")
            cfg.discord_token = cfg.discord_token or env.get("DISCORD_BOT_TOKEN", "")

    # Fallback: environment variables
    cfg.api_key = cfg.api_key or os.environ.get("ETORO_API_KEY", "")
    cfg.user_key = cfg.user_key or os.environ.get("ETORO_USER_KEY", "")
    cfg.discord_token = cfg.discord_token or os.environ.get("DISCORD_BOT_TOKEN", "")

    return cfg


def is_market_open(cfg: Optional[Config] = None) -> bool:
    """Check if NYSE is currently open (Mon-Fri 15:30-22:00 Europe/Berlin)."""
    from datetime import datetime
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Berlin")
    except ImportError:
        import pytz
        tz = pytz.timezone("Europe/Berlin")

    now = datetime.now(tz)

    # Weekends closed
    if now.weekday() >= 5:
        return False

    mh = cfg.market_hours if cfg else MarketHoursConfig()
    open_h, open_m = map(int, mh.open.split(":"))
    close_h, close_m = map(int, mh.close.split(":"))

    open_minutes = open_h * 60 + open_m
    close_minutes = close_h * 60 + close_m
    now_minutes = now.hour * 60 + now.minute

    return open_minutes <= now_minutes < close_minutes
