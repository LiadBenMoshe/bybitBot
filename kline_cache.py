"""Disk cache for kline history used by the research harness.

Research sweeps must run on a frozen dataset so that candidate rows are
comparable; this cache therefore never refreshes on its own. Pass
``refresh=True`` to extend a file with newer candles. Bottom-tier module:
imports only pandas and stdlib.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_CACHE_DIR = Path("data") / "klines"


def cache_path(symbol: str, interval: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    return cache_dir / f"{symbol}_{interval}.parquet"


def _read(path: Path) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except ImportError:
        csv_path = path.with_suffix(".csv")
        frame = pd.read_csv(csv_path)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame


def _write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        frame.to_parquet(path, index=False)
    except ImportError:
        frame.to_csv(path.with_suffix(".csv"), index=False)


def load_klines(
    client: Any,
    category: str,
    symbol: str,
    interval: str,
    total: int,
    *,
    refresh: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> pd.DataFrame:
    """Return the newest ``total`` candles, from disk when possible.

    A cache hit requires an existing file with at least ``total`` rows and
    ``refresh=False``. Otherwise the history is fetched through
    ``client.get_kline_history``, merged with whatever was cached (deduplicated
    on timestamp), written back and returned.
    """
    path = cache_path(symbol, interval, cache_dir)
    cached: pd.DataFrame | None = None
    if path.exists() or path.with_suffix(".csv").exists():
        cached = _read(path)
        if not refresh and len(cached) >= total:
            return cached.tail(total).reset_index(drop=True)

    fetched = client.get_kline_history(category, symbol, interval, total)
    if cached is not None and not cached.empty:
        merged = pd.concat([cached, fetched], ignore_index=True)
        merged = merged.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp")
        merged = merged.reset_index(drop=True)
    else:
        merged = fetched.reset_index(drop=True)
    _write(path, merged)
    return merged.tail(total).reset_index(drop=True)
