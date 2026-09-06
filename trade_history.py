"""Read-only access to the trade log (``logs/trades.jsonl``).

The log is append-only (see ``logger.append_jsonl``) and nothing else in the
codebase reads it back. This module normalises its two historical schemas
(10-key rows written before fee tracking existed, 13-key rows since), pairs
open/close records into one row per position, and serialises the result for
download. Bottom-tier module: it may import only stdlib and ``models``.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from models import TradeRecord

RAW_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(TradeRecord))
RAW_DEFAULTS: dict[str, Any] = {"gross_pnl": 0.0, "fees": 0.0, "entry_is_maker": False}

POSITION_COLUMNS: tuple[str, ...] = (
    "entry_time",
    "exit_time",
    "hold_minutes",
    "symbol",
    "mode",
    "side",
    "qty",
    "entry_price",
    "exit_price",
    "gross_pnl",
    "fees",
    "pnl",
    "entry_reason",
    "exit_reason",
    "entry_is_maker",
    "status",
)


def read_trade_rows(path: Path) -> list[dict[str, Any]]:
    """Return every record in the log, normalised to ``RAW_COLUMNS``.

    A missing file yields an empty list. Blank lines, undecodable lines (for
    example a trailing line still being appended by the trader) and non-object
    payloads are skipped rather than raised.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            rows.append({column: payload.get(column, RAW_DEFAULTS.get(column, "")) for column in RAW_COLUMNS})
    return rows


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _hold_minutes(entry_time: Any, exit_time: Any) -> Any:
    opened = _parse_time(entry_time)
    closed = _parse_time(exit_time)
    if opened is None or closed is None:
        return ""
    if (opened.tzinfo is None) != (closed.tzinfo is None):
        return ""
    return round((closed - opened).total_seconds() / 60.0, 1)


def _position_row(opened: Optional[dict[str, Any]], closed: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Merge an open record and/or a close record into one position row.

    PnL columns come only from the close record: a close already carries the
    full round trip, and an open record's ``fees`` is the entry fee alone, so
    copying it would make ``sum(fees)`` double-count.
    """
    if closed is not None:
        status = "closed" if opened is not None else "unmatched_close"
        return {
            "entry_time": opened["timestamp"] if opened else "",
            "exit_time": closed["timestamp"],
            "hold_minutes": _hold_minutes(opened["timestamp"], closed["timestamp"]) if opened else "",
            "symbol": closed["symbol"],
            "mode": closed["mode"],
            "side": closed["side"],
            "qty": closed["qty"],
            "entry_price": closed["entry_price"],
            "exit_price": closed["exit_price"],
            "gross_pnl": closed["gross_pnl"],
            "fees": closed["fees"],
            "pnl": closed["pnl"],
            "entry_reason": opened["reason"] if opened else "",
            "exit_reason": closed["reason"],
            "entry_is_maker": closed["entry_is_maker"],
            "status": status,
        }
    assert opened is not None
    return {
        "entry_time": opened["timestamp"],
        "exit_time": "",
        "hold_minutes": "",
        "symbol": opened["symbol"],
        "mode": opened["mode"],
        "side": opened["side"],
        "qty": opened["qty"],
        "entry_price": opened["entry_price"],
        "exit_price": "",
        "gross_pnl": "",
        "fees": "",
        "pnl": "",
        "entry_reason": opened["reason"],
        "exit_reason": "",
        "entry_is_maker": opened["entry_is_maker"],
        "status": "open",
    }


def pair_positions(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse open/close records into one row per position.

    Rows are consumed in file order, which is chronological. Opens wait on a
    per-(symbol, mode) stack; a close takes the most recent open on the same
    side (falling back to the most recent of any side), so an open that lost
    its close to a process restart is left behind and reported as ``open``.
    A close with no candidate open is reported as ``unmatched_close``.
    """
    pending: dict[tuple[str, str], list[dict[str, Any]]] = {}
    positions: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["symbol"]), str(row["mode"]))
        if row["action"] == "open":
            pending.setdefault(key, []).append(row)
            continue
        if row["action"] != "close":
            continue
        stack = pending.get(key, [])
        index = next((i for i in range(len(stack) - 1, -1, -1) if stack[i]["side"] == row["side"]), len(stack) - 1)
        opened = stack.pop(index) if index >= 0 else None
        positions.append(_position_row(opened, row))
    for stack in pending.values():
        positions.extend(_position_row(opened, None) for opened in stack)
    positions.sort(key=lambda item: str(item["entry_time"] or item["exit_time"]))
    return positions


def to_csv(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def to_json(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> str:
    return json.dumps([{column: row.get(column, "") for column in columns} for row in rows], ensure_ascii=True)
