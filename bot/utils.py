"""Shared math/time/IO helpers."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

SQRT2 = math.sqrt(2.0)


def phi(z: float) -> float:
    """Standard normal CDF. erf-based, no scipy needed."""
    if math.isnan(z):
        return 0.5
    if z > 12.0:
        return 1.0
    if z < -12.0:
        return 0.0
    return 0.5 * (1.0 + math.erf(z / SQRT2))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_ts() -> float:
    return now_utc().timestamp()


def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO timestamp (Gamma API style) into an aware UTC datetime."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def safe_read_json(path: str, default: Any) -> Any:
    """Load JSON, returning `default` on missing/empty/corrupt files (panama lesson)."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def safe_write_json(path: str, data: Any) -> None:
    """Atomic-ish JSON write: write temp then replace."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)
