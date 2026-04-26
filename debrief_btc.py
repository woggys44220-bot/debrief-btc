#!/usr/bin/env python3
"""Outil V1 de débrief journalier BTC (analyse uniquement)."""

from __future__ import annotations

import csv
import html
import json
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CONSOLE_EVENTS = [
    "ALLOW",
    "BLOCK",
    "WAIT_1",
    "WAIT_2",
    "GPT_TIMEOUT",
    "TIMEOUT_SAFE",
    "M5_TOO_FAR",
    "OUT_OF_SESSION",
    "CLOSE_DETECTED",
    "REENTRY_TOO_CLOSE",
    "ENTRY_FILTER_SKIP",
    "GPT_BLOCK",
    "TIMEOUTERROR_FALLBACK_ALLOW",
    "TIMEOUT_FALLBACK_ALLOW",
]

ERROR_PATTERNS = [
    r"\berror\b",
    r"\bexception\b",
    r"\btraceback\b",
    r"\bfailed\b",
    r"\bfatal\b",
]

TIME_PATTERNS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
]

MATCH_WINDOWS_MINUTES = {
    "entry_before": 10,
    "entry_after": 5,
    "exit_around": 5,
}

SNAPSHOT_ASSOCIATION_OFFSETS_HOURS = [0, 2, -2, 3, -3, 4, -4]
WIDE_PRE_ENTRY_WINDOW_MINUTES = 120


@dataclass
class Trade:
    index: int
    open_time: Optional[datetime]
    close_time: Optional[datetime]
    symbol: str
    side: str
    lot: Optional[float]
    entry_price: Optional[float]
    exit_price: Optional[float]
    profit: Optional[float]
    commission: Optional[float]
    swap: Optional[float]
    comment: str
    ticket: Optional[str] = None
    order: Optional[str] = None
    deal: Optional[str] = None
    sl: Optional[float] = None
    tp: Optional[float] = None


@dataclass
class ChartImage:
    path: Path
    timeframe: str
    timestamp: Optional[datetime]


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    iso_candidate = text.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(iso_candidate)
        return normalize_to_utc_naive(dt)
    except ValueError:
        pass

    for fmt in TIME_PATTERNS:
        try:
            return normalize_to_utc_naive(datetime.strptime(text, fmt))
        except ValueError:
            continue
    for pattern in (
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})",
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})",
        r"(\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}:\d{2})",
        r"(\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2})",
    ):
        m = re.search(pattern, text)
        if m:
            return parse_datetime(m.group(1))
    return None


def normalize_to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    elif text.count(",") > 0 and text.count(".") > 0:
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_fr_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Gère des formats MT5 type "76 247,00" et négatifs "- 15,45".
    text = text.replace("\xa0", " ")
    text = re.sub(r"^\-\s+", "-", text)
    text = text.replace(" ", "")
    text = text.replace(",", ".")
    # Nettoyage conservateur: garde uniquement chiffre, signe, point.
    text = re.sub(r"[^0-9\.\-+]", "", text)
    if text in {"", "-", "+", ".", "-.", "+."}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def detect_column(headers: List[str], aliases: List[str]) -> Optional[str]:
    norm_map = {normalize_name(h): h for h in headers}
    for alias in aliases:
        if alias in norm_map:
            return norm_map[alias]
    for norm, original in norm_map.items():
        if any(alias in norm for alias in aliases):
            return original
    return None


def ensure_output_dir(base_dir: Path) -> Path:
    out_dir = base_dir / "OUTPUT"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def read_text_file(path: Path) -> Tuple[str, Optional[str]]:
    if not path.exists():
        return "", f"Fichier manquant: {path.name}"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return "", f"Impossible de lire {path.name}: {exc}"


def parse_console(path: Path) -> Dict[str, Any]:
    text, warning = read_text_file(path)
    lines = text.splitlines()
    event_counts = Counter()
    matches: List[Dict[str, Any]] = []
    errors: List[str] = []

    close_detected_event_map = {
        "TP": "CLOSE_DETECTED_TP",
        "SL": "CLOSE_DETECTED_SL",
        "BE": "CLOSE_DETECTED_BE",
    }

    for i, line in enumerate(lines, start=1):
        line_upper = line.upper()
        ts = parse_datetime(line)
        for event in CONSOLE_EVENTS:
            if event in line_upper:
                event_counts[event] += 1
                matches.append({"line": i, "timestamp": ts, "event": event, "raw": line})

        if "CLOSE_DETECTED" in line_upper:
            for token, close_event in close_detected_event_map.items():
                if token in line_upper:
                    event_counts[close_event] += 1
                    matches.append({"line": i, "timestamp": ts, "event": close_event, "raw": line})

        if any(re.search(pat, line, flags=re.IGNORECASE) for pat in ERROR_PATTERNS):
            errors.append(f"L{i}: {line}")

    return {
        "warning": warning,
        "line_count": len(lines),
        "event_counts": dict(event_counts),
        "events": matches,
        "errors": errors,
    }


def parse_snapshots(path: Path) -> Dict[str, Any]:
    text, warning = read_text_file(path)
    lines = text.splitlines()
    records: List[Dict[str, Any]] = []
    invalid_lines: List[int] = []
    fields = Counter()

    for i, line in enumerate(lines, start=1):
        raw = line.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                invalid_lines.append(i)
                continue
        except json.JSONDecodeError:
            invalid_lines.append(i)
            continue

        data["_line"] = i
        data["_timestamp"] = parse_datetime(
            data.get("timestamp")
            or data.get("time")
            or data.get("datetime")
            or data.get("ts")
        )
        records.append(data)
        for key in data.keys():
            fields[key] += 1

    skip_reasons = Counter()
    gpt_decisions = Counter()
    spreads: List[float] = []
    m5_distances: List[float] = []

    for rec in records:
        for key in ["skip_reason", "reason", "raison_skip", "entry_filter_skip_reason"]:
            val = rec.get(key)
            if val:
                skip_reasons[str(val)] += 1
        for key in ["gpt_decision", "decision_gpt", "gpt", "decision"]:
            val = rec.get(key)
            if val:
                gpt_decisions[str(val)] += 1
        for key in ["spread", "spread_points", "spread_pips"]:
            val = safe_float(rec.get(key))
            if val is not None:
                spreads.append(val)
                break
        for key in ["distance_m5", "m5_distance", "distanceM5", "m5_too_far_distance"]:
            val = safe_float(rec.get(key))
            if val is not None:
                m5_distances.append(val)
                break

    records.sort(key=lambda r: r.get("_timestamp") or datetime.min)

    return {
        "warning": warning,
        "line_count": len(lines),
        "records": records,
        "invalid_lines": invalid_lines,
        "fields": fields,
        "skip_reasons": skip_reasons,
        "gpt_decisions": gpt_decisions,
        "spread_avg": statistics.mean(spreads) if spreads else None,
        "m5_distance_avg": statistics.mean(m5_distances) if m5_distances else None,
    }


def parse_mt5(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"warning": f"Fichier manquant: {path.name}", "trades": [], "headers": []}

    text, warning = read_text_file(path)
    if warning:
        return {"warning": warning, "trades": [], "headers": []}

    if is_mt5_tabulated_report(text):
        return parse_mt5_tabulated_report(text)
    return parse_mt5_simple_csv(path)


def is_mt5_tabulated_report(text: str) -> bool:
    lower = text.lower()
    markers = [
        "rapport d'historique de trading",
        "positions",
        "ordres",
        "transactions",
        "résultats",
        "resultats",
    ]
    tabs_count = text.count("\t")
    return tabs_count > 20 and sum(1 for m in markers if m in lower) >= 2


def parse_mt5_tabulated_report(text: str) -> Dict[str, Any]:
    lines = text.splitlines()
    headers: List[str] = []
    trades: List[Trade] = []
    ignored_rows = 0
    non_empty_rows = 0
    report_stats: Dict[str, float] = {}

    start_idx = next((i for i, line in enumerate(lines) if line.strip().lower() == "positions"), None)
    if start_idx is None:
        return {"warning": "Section Positions introuvable dans le rapport MT5", "trades": [], "headers": []}

    end_markers = {"ordres", "orders"}
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if lines[i].strip().lower() in end_markers:
            end_idx = i
            break

    section = lines[start_idx + 1 : end_idx]
    if not section:
        return {"warning": "Section Positions vide", "trades": [], "headers": []}

    for raw in section:
        if not raw.strip():
            ignored_rows += 1
            continue
        cols = [c.strip() for c in raw.split("\t")]
        cols = [c for c in cols if c != ""]
        if not cols:
            ignored_rows += 1
            continue

        # Ligne d'en-tête de la section Positions.
        if normalize_name(cols[0]) == "heure" and len(cols) >= 10:
            headers = cols
            continue

        non_empty_rows += 1
        if len(cols) < 13:
            ignored_rows += 1
            continue

        open_time = parse_datetime(cols[0])
        symbol = cols[2]
        side_raw = cols[3].strip().upper()
        side = "BUY" if "BUY" in side_raw else "SELL" if "SELL" in side_raw else side_raw
        entry_price = parse_fr_number(cols[5])
        close_time = parse_datetime(cols[8])
        exit_price = parse_fr_number(cols[9])
        commission = parse_fr_number(cols[10])
        swap = parse_fr_number(cols[11])
        profit = parse_fr_number(cols[12])

        has_core_data = any(
            (
                open_time is not None or close_time is not None,
                bool(symbol),
                bool(side),
                entry_price is not None,
                profit is not None,
            )
        )
        if not has_core_data:
            ignored_rows += 1
            continue

        trades.append(
            Trade(
                index=len(trades) + 1,
                open_time=open_time,
                close_time=close_time,
                symbol=symbol,
                side=side,
                lot=parse_fr_number(cols[4]),
                entry_price=entry_price,
                exit_price=exit_price,
                profit=profit,
                commission=commission,
                swap=swap,
                comment=f"position_id={cols[1]} sl={cols[6]} tp={cols[7]}",
                ticket=str(cols[1]).strip() if cols[1] else None,
                sl=parse_fr_number(cols[6]),
                tp=parse_fr_number(cols[7]),
            )
        )

    report_stats = parse_mt5_results_section(lines)
    return finalize_mt5_parse(
        trades=trades,
        headers=headers,
        ignored_rows=ignored_rows,
        non_empty_rows=non_empty_rows,
        parse_warning="CSV MT5 non reconnu ou colonnes incompatibles" if non_empty_rows > 0 and len(trades) == 0 else None,
        report_stats=report_stats,
        source_format="mt5_report_tabulated",
    )


def parse_mt5_results_section(lines: List[str]) -> Dict[str, float]:
    stats: Dict[str, float] = {}
    start_idx = next((i for i, line in enumerate(lines) if line.strip().lower() in {"résultats", "resultats", "results"}), None)
    if start_idx is None:
        return stats

    for line in lines[start_idx + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() in {"positions", "ordres", "orders", "transactions"}:
            break
        cols = [c.strip() for c in line.split("\t") if c.strip()]
        if len(cols) < 2:
            continue
        key = normalize_name(cols[0])
        value = parse_fr_number(cols[1])
        if value is None:
            continue
        if key in {"nbtrades", "nombretrades", "totaltrades", "trades"}:
            stats["nb_trades"] = value
        if key in {"profittotalnet", "netprofit", "profitnet"}:
            stats["profit_total_net"] = value
    return stats


def parse_mt5_simple_csv(path: Path) -> Dict[str, Any]:
    trades: List[Trade] = []
    headers: List[str] = []
    ignored_rows = 0
    non_empty_rows = 0

    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(f, dialect=dialect)
            if not reader.fieldnames:
                return {"warning": "CSV vide ou sans en-têtes", "trades": [], "headers": []}
            headers = list(reader.fieldnames)

            col_open = detect_column(headers, ["opentime", "timeopen", "ouverture", "time"])
            col_close = detect_column(headers, ["closetime", "timeclose", "fermeture"])
            col_symbol = detect_column(headers, ["symbol", "symbole"])
            col_side = detect_column(headers, ["type", "side", "ordertype", "buyorsell"])
            col_lot = detect_column(headers, ["volume", "lot", "lots", "size"])
            col_entry = detect_column(headers, ["price", "entryprice", "openprice", "prixentree"])
            col_exit = detect_column(headers, ["priceclose", "exitprice", "closeprice", "prixsortie"])
            col_profit = detect_column(headers, ["profit", "pnl", "result", "gain"])
            col_comm = detect_column(headers, ["commission", "comm"])
            col_swap = detect_column(headers, ["swap"])
            col_ticket = detect_column(headers, ["ticket", "positionid", "position", "idposition"])
            col_order = detect_column(headers, ["order", "orderid", "ordre", "idordre"])
            col_deal = detect_column(headers, ["deal", "dealid", "transactionid", "iddeal"])
            col_sl = detect_column(headers, ["sl", "stoploss", "stop_loss"])
            col_tp = detect_column(headers, ["tp", "takeprofit", "take_profit"])
            col_comment = detect_column(headers, ["comment", "commentaire", "note"])

            for idx, row in enumerate(reader, start=1):
                row_values = [str(v).strip() for v in row.values() if v is not None]
                if not any(row_values):
                    ignored_rows += 1
                    continue

                non_empty_rows += 1
                open_time = parse_datetime(row.get(col_open)) if col_open else None
                close_time = parse_datetime(row.get(col_close)) if col_close else None
                symbol = str(row.get(col_symbol, "")).strip() if col_symbol else ""
                side = str(row.get(col_side, "")).strip().upper() if col_side else ""
                entry_price = safe_float(row.get(col_entry)) if col_entry else None
                profit = safe_float(row.get(col_profit)) if col_profit else None

                # Règle demandée: ne pas compter les lignes vides/inexploitables comme trades.
                has_core_data = any(
                    (
                        open_time is not None or close_time is not None,
                        bool(symbol),
                        bool(side),
                        entry_price is not None,
                        profit is not None,
                    )
                )
                if not has_core_data:
                    ignored_rows += 1
                    continue

                trades.append(
                    Trade(
                        index=idx,
                        open_time=open_time,
                        close_time=close_time,
                        symbol=symbol,
                        side=side,
                        lot=safe_float(row.get(col_lot)) if col_lot else None,
                        entry_price=entry_price,
                        exit_price=safe_float(row.get(col_exit)) if col_exit else None,
                        profit=profit,
                        commission=safe_float(row.get(col_comm)) if col_comm else None,
                        swap=safe_float(row.get(col_swap)) if col_swap else None,
                        comment=str(row.get(col_comment, "")).strip() if col_comment else "",
                        ticket=str(row.get(col_ticket, "")).strip() if col_ticket and row.get(col_ticket) else None,
                        order=str(row.get(col_order, "")).strip() if col_order and row.get(col_order) else None,
                        deal=str(row.get(col_deal, "")).strip() if col_deal and row.get(col_deal) else None,
                        sl=safe_float(row.get(col_sl)) if col_sl else None,
                        tp=safe_float(row.get(col_tp)) if col_tp else None,
                    )
                )
    except OSError as exc:
        return {"warning": f"Impossible de lire {path.name}: {exc}", "trades": [], "headers": []}

    return finalize_mt5_parse(
        trades=trades,
        headers=headers,
        ignored_rows=ignored_rows,
        non_empty_rows=non_empty_rows,
        parse_warning="CSV MT5 non reconnu ou colonnes incompatibles" if non_empty_rows > 0 and len(trades) == 0 else None,
        report_stats={},
        source_format="simple_csv",
    )


def finalize_mt5_parse(
    trades: List[Trade],
    headers: List[str],
    ignored_rows: int,
    non_empty_rows: int,
    parse_warning: Optional[str],
    report_stats: Dict[str, float],
    source_format: str,
) -> Dict[str, Any]:

    profits = [t.profit for t in trades if t.profit is not None]
    net_results = []
    for t in trades:
        if t.profit is None:
            continue
        net_results.append(t.profit + (t.commission or 0.0) + (t.swap or 0.0))
    positives = [p for p in profits if p > 0]
    negatives = [p for p in profits if p < 0]
    zeros = [p for p in profits if p == 0]

    durations = []
    for t in trades:
        if t.open_time and t.close_time and t.close_time >= t.open_time:
            durations.append((t.close_time - t.open_time).total_seconds() / 60.0)

    first_trade = min((t.open_time for t in trades if t.open_time), default=None)
    last_trade = max((t.close_time or t.open_time for t in trades if (t.close_time or t.open_time)), default=None)
    return {
        "warning": parse_warning,
        "headers": headers,
        "trades": trades,
        "ignored_rows": ignored_rows,
        "non_empty_rows": non_empty_rows,
        "source_format": source_format,
        "report_stats": report_stats,
        "metrics": {
            "total_trades": len(trades),
            "tp_count": len(positives),
            "sl_count": len(negatives),
            "be_count": len(zeros),
            "profit_net": sum(net_results) if net_results else 0.0,
            "avg_gain": statistics.mean(positives) if positives else None,
            "avg_loss": statistics.mean(negatives) if negatives else None,
            "best_trade": max(profits) if profits else None,
            "worst_trade": min(profits) if profits else None,
            "first_trade": first_trade,
            "last_trade": last_trade,
            "avg_duration_min": statistics.mean(durations) if durations else None,
        },
    }


def parse_charts(charts_dir: Path) -> Dict[str, Any]:
    if not charts_dir.exists() or not charts_dir.is_dir():
        return {"warning": "Dossier charts/ manquant", "charts": []}

    charts: List[ChartImage] = []
    for path in sorted(charts_dir.iterdir()):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        name = path.name.upper()
        timeframe = "M5" if "M5" in name else "M15" if "M15" in name else "UNKNOWN"
        ts = extract_time_from_filename(path.name)
        charts.append(ChartImage(path=path, timeframe=timeframe, timestamp=ts))

    return {"warning": None, "charts": charts}


def extract_time_from_filename(filename: str) -> Optional[datetime]:
    base = Path(filename).stem
    patterns = [
        r"(\d{4}-\d{2}-\d{2})[_ -](\d{2})h(\d{2})",
        r"(\d{4}-\d{2}-\d{2})[_ -](\d{2})[:h](\d{2})(?:[:m](\d{2}))?",
        r"(\d{8})[_ -]?(\d{4,6})",
    ]
    for pattern in patterns:
        m = re.search(pattern, base)
        if not m:
            continue
        groups = m.groups()
        try:
            if len(groups[0]) == 8 and groups[0].isdigit():
                date = datetime.strptime(groups[0], "%Y%m%d")
                hms = groups[1]
                if len(hms) == 4:
                    hour, minute = int(hms[:2]), int(hms[2:4])
                    sec = 0
                else:
                    hour, minute, sec = int(hms[:2]), int(hms[2:4]), int(hms[4:6])
                return date.replace(hour=hour, minute=minute, second=sec)
            date = datetime.strptime(groups[0], "%Y-%m-%d")
            hour = int(groups[1])
            minute = int(groups[2])
            second = int(groups[3]) if len(groups) > 3 and groups[3] else 0
            return date.replace(hour=hour, minute=minute, second=second)
        except ValueError:
            continue
    return None


def classify_trade_result(trade: Trade) -> str:
    if trade.profit is None:
        return "inconnu"
    if trade.profit > 0:
        return "TP"
    if trade.profit < 0:
        return "SL"
    return "BE"


def normalize_side(value: str) -> str:
    text = (value or "").strip().upper()
    if "BUY" in text or text in {"B", "LONG", "ACHAT"}:
        return "BUY"
    if "SELL" in text or text in {"S", "SHORT", "VENTE"}:
        return "SELL"
    return text or "N/A"


def snapshot_side(snapshot: Dict[str, Any]) -> str:
    for key in ("side", "signal_side", "direction", "trade_side", "order_side"):
        value = snapshot.get(key)
        if value:
            return normalize_side(str(value))
    return "N/A"


def snapshot_value(snapshot: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = snapshot.get(key)
        if value not in (None, ""):
            return str(value)
    return "N/A"


def snapshot_price(snapshot: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
    for key in keys:
        value = safe_float(snapshot.get(key))
        if value is not None:
            return value
    return None


def snapshot_identifier(snapshot: Dict[str, Any], key_family: str) -> Optional[str]:
    keys_map = {
        "ticket": ("ticket", "position_id", "position", "id_position"),
        "order": ("order", "order_id", "id_order", "ordre"),
        "deal": ("deal", "deal_id", "id_deal", "transaction_id"),
    }
    for key in keys_map[key_family]:
        value = snapshot.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def snapshot_is_order_ok(snapshot: Dict[str, Any]) -> bool:
    for key in ("status", "result", "event", "decision", "gpt_decision"):
        value = snapshot.get(key)
        if value and "ORDER_OK" in str(value).upper():
            return True
    return False


def has_full_setup(snapshot: Dict[str, Any]) -> bool:
    return (
        snapshot_side(snapshot) in {"BUY", "SELL"}
        and snapshot_price(snapshot, ("entry", "entry_price", "price", "open_price")) is not None
        and snapshot_price(snapshot, ("sl", "stop_loss", "stoploss")) is not None
        and snapshot_price(snapshot, ("tp", "take_profit", "takeprofit")) is not None
    )


def price_delta_ok(mt5_price: Optional[float], snapshot_entry: Optional[float]) -> bool:
    if mt5_price is None or snapshot_entry is None:
        return False
    tolerance = max(20.0, abs(mt5_price) * 0.003)
    return abs(mt5_price - snapshot_entry) <= tolerance


def select_by_time(candidates: List[Dict[str, Any]], anchor: datetime) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs((s.get("_timestamp") - anchor).total_seconds()) if s.get("_timestamp") else 1e18)


def match_trade_snapshot(trade: Trade, snapshots: List[Dict[str, Any]], anchor: datetime) -> Dict[str, Any]:
    closest_snapshot = select_by_time([s for s in snapshots if s.get("_timestamp")], anchor)
    closest_time = closest_snapshot.get("_timestamp") if closest_snapshot else None
    closest_gap_min = abs((closest_time - anchor).total_seconds()) / 60.0 if closest_time else None
    closest_entry = snapshot_price(closest_snapshot, ("entry", "entry_price", "price", "open_price")) if closest_snapshot else None

    trade_side = normalize_side(trade.side)
    trade_ids = {"ticket": trade.ticket, "order": trade.order, "deal": trade.deal}
    ids_available = any(trade_ids.values())

    id_candidates = []
    if ids_available:
        for snap in snapshots:
            if not snap.get("_timestamp"):
                continue
            for family, value in trade_ids.items():
                if value and snapshot_identifier(snap, family) == str(value):
                    id_candidates.append(snap)
                    break

    selected = select_by_time(id_candidates, anchor)
    method = "A:id(ticket/order/deal)"
    confidence = "haute"

    if not selected:
        order_ok_candidates = [s for s in snapshots if s.get("_timestamp") and snapshot_is_order_ok(s)]
        selected = select_by_time(order_ok_candidates, anchor)
        method = "B:ORDER_OK plus proche"
        confidence = "moyenne"

    if not selected:
        c_candidates = []
        for snap in snapshots:
            if not snap.get("_timestamp"):
                continue
            if snapshot_side(snap) != trade_side:
                continue
            if price_delta_ok(trade.entry_price, snapshot_price(snap, ("entry", "entry_price", "price", "open_price"))):
                c_candidates.append(snap)
        selected = select_by_time(c_candidates, anchor)
        method = "C:side + entry proche"
        confidence = "moyenne"

    if not selected and trade.open_time:
        setup_candidates = []
        for snap in snapshots:
            ts = snap.get("_timestamp")
            if not ts or ts > anchor:
                continue
            if snapshot_side(snap) != trade_side:
                continue
            if not has_full_setup(snap):
                continue
            if price_delta_ok(trade.entry_price, snapshot_price(snap, ("entry", "entry_price", "price", "open_price"))):
                setup_candidates.append(snap)
        selected = select_by_time(setup_candidates, anchor)
        method = "C-setup:side+entry+sl+tp (ancien)"
        confidence = "moyenne"

    if not selected and trade.open_time:
        lower_bound = anchor - timedelta(minutes=WIDE_PRE_ENTRY_WINDOW_MINUTES)
        d_candidates = [s for s in snapshots if s.get("_timestamp") and lower_bound <= s["_timestamp"] <= anchor]
        selected = select_by_time(d_candidates, anchor)
        method = "D:avant entrée (2h)"
        confidence = "faible"

    if not selected:
        return {
            "selected": None,
            "method": "aucune",
            "confidence": "faible",
            "diagnostic": {
                "trade_time": anchor,
                "closest_snapshot_time": closest_time,
                "gap_minutes": closest_gap_min,
                "entry_price_diff": abs(trade.entry_price - closest_entry) if trade.entry_price is not None and closest_entry is not None else None,
                "trade_ticket": trade.ticket,
                "trade_order": trade.order,
                "trade_deal": trade.deal,
                "snapshot_ticket": snapshot_identifier(closest_snapshot, "ticket") if closest_snapshot else None,
                "snapshot_order": snapshot_identifier(closest_snapshot, "order") if closest_snapshot else None,
                "snapshot_deal": snapshot_identifier(closest_snapshot, "deal") if closest_snapshot else None,
            },
        }

    selected_time = selected.get("_timestamp")
    selected_entry = snapshot_price(selected, ("entry", "entry_price", "price", "open_price"))
    return {
        "selected": selected,
        "method": method,
        "confidence": confidence,
        "diagnostic": {
            "trade_time": anchor,
            "closest_snapshot_time": closest_time,
            "gap_minutes": abs((selected_time - anchor).total_seconds()) / 60.0 if selected_time else None,
            "entry_price_diff": abs(trade.entry_price - selected_entry) if trade.entry_price is not None and selected_entry is not None else None,
            "trade_ticket": trade.ticket,
            "trade_order": trade.order,
            "trade_deal": trade.deal,
            "snapshot_ticket": snapshot_identifier(selected, "ticket"),
            "snapshot_order": snapshot_identifier(selected, "order"),
            "snapshot_deal": snapshot_identifier(selected, "deal"),
        },
    }


def associate_trade_context(
    mt5: Dict[str, Any], console: Dict[str, Any], snaps: Dict[str, Any], charts: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    result = []
    trades: List[Trade] = mt5.get("trades", [])
    console_events = console.get("events", [])
    snapshots = snaps.get("records", [])
    chart_items: List[ChartImage] = charts.get("charts", [])

    def build_with_offset(offset_hours: int) -> List[Dict[str, Any]]:
        items = []
        for t in trades:
            anchor = t.open_time or t.close_time
            if not anchor:
                items.append(
                    {
                        "trade": t,
                        "snapshots": [],
                        "console_events": [],
                        "charts": [],
                        "comment": "association impossible: trade sans open_time/close_time",
                        "snapshot_debug": {"reason": "trade_sans_horodatage"},
                        "association_method": "aucune",
                        "association_confidence": "faible",
                    }
                )
                continue

            shifted_anchor = anchor + timedelta(hours=offset_hours)
            assoc = match_trade_snapshot(t, snapshots, shifted_anchor)
            selected = assoc["selected"]
            near_console = [e for e in console_events if e.get("timestamp") and abs(e["timestamp"] - shifted_anchor) <= timedelta(minutes=20)]
            near_charts = [c for c in chart_items if c.timestamp and abs(c.timestamp - shifted_anchor) <= timedelta(minutes=20)]

            comment = "entrée propre"
            if not selected:
                comment = "association snapshots échouée (voir diagnostic horaire)"
            elif any("M5_TOO_FAR" in str(e.get("event", "")) for e in near_console):
                comment = "entrée tardive possible"
            elif any("OUT_OF_SESSION" in str(e.get("event", "")) for e in near_console):
                comment = "marché sale possible"

            items.append(
                {
                    "trade": t,
                    "snapshots": [selected] if selected else [],
                    "console_events": near_console[:8],
                    "charts": near_charts,
                    "comment": comment,
                    "snapshot_debug": assoc["diagnostic"],
                    "association_method": assoc["method"],
                    "association_confidence": assoc["confidence"],
                }
            )
        return items

    best_contexts = []
    best_offset = 0
    best_score = (-1, -1, -1, -999)
    for offset in SNAPSHOT_ASSOCIATION_OFFSETS_HOURS:
        contexts = build_with_offset(offset)
        matched = sum(1 for c in contexts if c["snapshots"])
        high = sum(1 for c in contexts if c["association_confidence"] == "haute")
        medium = sum(1 for c in contexts if c["association_confidence"] == "moyenne")
        score = (matched, high, medium, -abs(offset))
        if score > best_score:
            best_score = score
            best_offset = offset
            best_contexts = contexts

    return best_contexts, {
        "tested_offsets_hours": SNAPSHOT_ASSOCIATION_OFFSETS_HOURS,
        "best_offset_hours": best_offset,
        "matched_trades": best_score[0],
    }


def evaluate_day(mt5_metrics: Dict[str, Any], console_counts: Dict[str, int]) -> Tuple[str, str]:
    trades = mt5_metrics.get("total_trades", 0)
    pnl = mt5_metrics.get("profit_net", 0.0)
    timeouts = console_counts.get("GPT_TIMEOUT", 0)
    too_far = console_counts.get("M5_TOO_FAR", 0)
    if trades == 0:
        return "non tradable", "Aucun trade exécuté, journée possiblement non tradable ou filtre trop strict."
    if pnl > 0 and timeouts == 0:
        return "tradable", "Journée globalement saine avec résultat net positif."
    if pnl <= 0 and (timeouts > 0 or too_far > trades * 2):
        return "limite", "Résultat fragile: possible interaction entre qualité marché et filtres."
    return "limite", "Journée mitigée: des signaux exploitables existent mais nécessitent vérification."


def detect_anomalies(console: Dict[str, Any], snaps: Dict[str, Any], mt5: Dict[str, Any], contexts: List[Dict[str, Any]]) -> List[str]:
    anomalies_high = []
    anomalies = []
    counts = console.get("event_counts", {})
    timeout_fallback_allow = counts.get("TIMEOUTERROR_FALLBACK_ALLOW", 0) + counts.get("TIMEOUT_FALLBACK_ALLOW", 0)
    if timeout_fallback_allow > 0:
        anomalies_high.append("DANGER : GPT timeout fallback ALLOW détecté")
    if counts.get("GPT_TIMEOUT", 0) > 0:
        anomalies.append(f"GPT_TIMEOUT détecté: {counts.get('GPT_TIMEOUT', 0)}")
    if counts.get("GPT_TIMEOUT", 0) > 0 and counts.get("ALLOW", 0) > 0:
        anomalies.append("Vérifier si un timeout GPT a été transformé en ALLOW")
    if counts.get("OUT_OF_SESSION", 0) > 20:
        anomalies.append("Beaucoup de OUT_OF_SESSION: vérifier les horaires et filtres")
    if counts.get("M5_TOO_FAR", 0) > 20:
        anomalies.append("Beaucoup de M5_TOO_FAR: filtre possiblement trop strict")
    if snaps.get("invalid_lines"):
        anomalies.append(f"Snapshots invalides: {len(snaps['invalid_lines'])} lignes")
    if mt5.get("metrics", {}).get("total_trades", 0) == 0:
        anomalies.append("Historique MT5 vide ou sans trades")

    for ctx in contexts:
        trade: Trade = ctx["trade"]
        if not ctx["snapshots"]:
            anomalies.append(f"Trade #{trade.index} sans snapshot proche")
        if not ctx["console_events"]:
            anomalies.append(f"Trade #{trade.index} absent de la console (aucun événement proche)")

    close_detected = counts.get("CLOSE_DETECTED", 0)
    mt5_trades = mt5.get("metrics", {}).get("total_trades", 0)
    if mt5_trades > 0 and close_detected == 0:
        anomalies.append("Fermeture MT5 non détectée dans la console")

    report_stats = mt5.get("report_stats", {}) or {}
    report_nb_trades = report_stats.get("nb_trades")
    if report_nb_trades is not None and int(report_nb_trades) != mt5_trades:
        anomalies.append(
            f"Incohérence MT5: trades parsés={mt5_trades} vs Nb trades rapport={int(report_nb_trades)}"
        )

    return anomalies_high + anomalies


def fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "N/A"


def build_html(
    base_dir: Path,
    out_dir: Path,
    console: Dict[str, Any],
    snaps: Dict[str, Any],
    mt5: Dict[str, Any],
    charts: Dict[str, Any],
    contexts: List[Dict[str, Any]],
    anomalies: List[str],
    association_meta: Dict[str, Any],
) -> str:
    metrics = mt5.get("metrics", {})
    counts = console.get("event_counts", {})
    report_stats = mt5.get("report_stats", {}) or {}
    day_status, day_conclusion = evaluate_day(metrics, counts)

    def rel(path: Path) -> str:
        return str(path.relative_to(out_dir.parent)).replace("\\", "/")

    trade_rows = []
    for t in mt5.get("trades", []):
        dur = ""
        if t.open_time and t.close_time and t.close_time >= t.open_time:
            dur = f"{(t.close_time - t.open_time).total_seconds()/60:.1f} min"
        trade_rows.append(
            f"<tr><td>{t.index}</td><td>{fmt_dt(t.open_time)}</td><td>{fmt_dt(t.close_time)}</td>"
            f"<td>{html.escape(t.symbol)}</td><td>{html.escape(t.side)}</td><td>{t.lot if t.lot is not None else ''}</td>"
            f"<td>{t.entry_price if t.entry_price is not None else ''}</td><td>{t.exit_price if t.exit_price is not None else ''}</td>"
            f"<td>{t.profit if t.profit is not None else ''}</td><td>{dur}</td></tr>"
        )

    context_blocks = []
    for ctx in contexts:
        t: Trade = ctx["trade"]
        snap_debug = ctx.get("snapshot_debug", {})
        closest_before = ctx["snapshots"][0] if ctx["snapshots"] else None
        images = " ".join(
            f'<a href="../{html.escape(rel(c.path))}">{html.escape(c.path.name)}</a>' for c in ctx["charts"]
        ) or "Aucune"
        evs = ", ".join(sorted({e["event"] for e in ctx["console_events"]})) or "Aucun"
        closest_before_line = "Aucun snapshot trouvé avant entrée"
        if closest_before:
            closest_before_line = (
                f"{fmt_dt(closest_before.get('_timestamp'))}"
                f" | décision GPT={html.escape(snapshot_value(closest_before, ('gpt_decision', 'decision_gpt', 'decision', 'gpt')))}"
                f" | side={html.escape(snapshot_side(closest_before))}"
                f" | entry/sl/tp={html.escape(snapshot_value(closest_before, ('entry', 'entry_price')))} /"
                f"{html.escape(snapshot_value(closest_before, ('sl', 'stop_loss')))} /"
                f"{html.escape(snapshot_value(closest_before, ('tp', 'take_profit')))}"
                f" | sniper={html.escape(snapshot_value(closest_before, ('sniper_filters', 'filters', 'entry_filter_skip_reason')))}"
                f" | M15={html.escape(snapshot_value(closest_before, ('m15_context', 'context_m15', 'm15')))}"
            )

        diag_line = ""
        gap = snap_debug.get("gap_minutes")
        entry_diff = snap_debug.get("entry_price_diff")
        diag_line = (
            f"<p><b>Diagnostic association:</b> trade={fmt_dt(snap_debug.get('trade_time'))} | "
            f"snapshot_proche={fmt_dt(snap_debug.get('closest_snapshot_time'))} | "
            f"écart={f'{gap:.2f} min' if gap is not None else 'N/A'} | "
            f"delta_entry={f'{entry_diff:.2f}' if entry_diff is not None else 'N/A'} | "
            f"trade ticket/order/deal={html.escape(str(snap_debug.get('trade_ticket') or 'N/A'))}/"
            f"{html.escape(str(snap_debug.get('trade_order') or 'N/A'))}/"
            f"{html.escape(str(snap_debug.get('trade_deal') or 'N/A'))} | "
            f"snapshot ticket/order/deal={html.escape(str(snap_debug.get('snapshot_ticket') or 'N/A'))}/"
            f"{html.escape(str(snap_debug.get('snapshot_order') or 'N/A'))}/"
            f"{html.escape(str(snap_debug.get('snapshot_deal') or 'N/A'))}</p>"
        )
        context_blocks.append(
            "<div class='card'>"
            f"<h4>Trade #{t.index} — {html.escape(t.side)} — profit={t.profit}</h4>"
            f"<p><b>Heure:</b> {fmt_dt(t.open_time)} | <b>Résultat:</b> {classify_trade_result(t)}</p>"
            f"<p><b>Snapshots associés:</b> {len(ctx['snapshots'])} | <b>Événements console:</b> {html.escape(evs)}</p>"
            f"<p><b>Méthode d'association:</b> {html.escape(ctx.get('association_method', 'N/A'))} | "
            f"<b>Confiance:</b> {html.escape(ctx.get('association_confidence', 'faible'))}</p>"
            f"<p><b>Snapshot avant entrée (plus proche):</b> {closest_before_line}</p>"
            f"<p><b>Charts liées:</b> {images}</p>"
            f"<p><b>Commentaire automatique:</b> {html.escape(ctx['comment'])}</p>"
            f"{diag_line}"
            "</div>"
        )

    priority_skips = [
        "ENTRY_FILTER_SKIP",
        "M5_TOO_FAR",
        "OUT_OF_SESSION",
        "REENTRY_TOO_CLOSE",
        "GPT_BLOCK",
        "WAIT_1",
        "WAIT_2",
    ]
    skip_lines = []
    for key in priority_skips:
        value = counts.get(key, 0)
        if value > 0:
            skip_lines.append((key, value))
    skip_items = "".join(f"<li>{html.escape(k)}: {v}</li>" for k, v in skip_lines) or "<li>Aucune</li>"

    skip_interpretation = []
    if counts.get("M5_TOO_FAR", 0) > 0:
        skip_interpretation.append("beaucoup de M5_TOO_FAR = filtre possiblement strict")
    if counts.get("OUT_OF_SESSION", 0) > 0:
        skip_interpretation.append("beaucoup de OUT_OF_SESSION = normal si logs hors session, sauf si trade hors session")
    if counts.get("REENTRY_TOO_CLOSE", 0) > 0:
        skip_interpretation.append("beaucoup de REENTRY_TOO_CLOSE = pacing / cooldown actif")
    if counts.get("GPT_BLOCK", 0) > 0:
        skip_interpretation.append("beaucoup de GPT_BLOCK = GPT filtre activement")
    if counts.get("ENTRY_FILTER_SKIP", 0) > 0:
        skip_interpretation.append("beaucoup de ENTRY_FILTER_SKIP = filtres techniques très actifs")
    if not skip_interpretation:
        skip_interpretation.append("Aucune interprétation: aucun skip important détecté.")

    anomalies_html = "".join(f"<li>{html.escape(a)}</li>" for a in anomalies) or "<li>Aucune anomalie détectée</li>"

    html_content = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<title>Rapport journalier BTC</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 24px; line-height: 1.4; }}
h1,h2,h3 {{ color: #1f2937; }}
.card {{ border: 1px solid #ddd; padding: 12px; margin-bottom: 10px; border-radius: 6px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 16px; }}
th,td {{ border: 1px solid #ccc; padding: 6px; font-size: 13px; text-align: left; }}
.muted {{ color: #6b7280; font-size: 12px; }}
</style>
</head>
<body>
<h1>Rapport journalier BTC</h1>
<h2>1) Résumé de la journée</h2>
<ul>
<li>Date génération: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</li>
<li>Nombre de trades: {metrics.get('total_trades', 0)}</li>
<li>Résultat net: {metrics.get('profit_net', 0.0):.2f}</li>
<li>TP / SL / BE: {metrics.get('tp_count', 0)} / {metrics.get('sl_count', 0)} / {metrics.get('be_count', 0)}</li>
<li>Journée: <b>{day_status}</b></li>
<li>Conclusion rapide: {html.escape(day_conclusion)}</li>
</ul>

<h2>2) Analyse MT5</h2>
<p class="muted">Lignes MT5 ignorées (vides/inexploitables): {mt5.get('ignored_rows', 0)}</p>
<p class="muted">Format détecté: {mt5.get('source_format', 'unknown')} | Contrôle Résultats: Nb trades={report_stats.get('nb_trades', 'N/A')}, Profit Total Net={report_stats.get('profit_total_net', 'N/A')}</p>
<table>
<tr><th>#</th><th>Ouverture</th><th>Fermeture</th><th>Symbole</th><th>Sens</th><th>Lot</th><th>Entrée</th><th>Sortie</th><th>Profit</th><th>Durée</th></tr>
{''.join(trade_rows) if trade_rows else '<tr><td colspan="10">Aucun trade</td></tr>'}
</table>

<h2>3) Analyse console</h2>
<ul>
{''.join(f'<li>{k}: {v}</li>' for k, v in sorted(counts.items())) or '<li>Aucun événement détecté</li>'}
</ul>
<p class="muted">Mentions techniques SL/TP ignorées pour éviter les faux compteurs.</p>
<p>Erreurs importantes: {len(console.get('errors', []))}</p>

<h2>4) Analyse snapshots</h2>
<ul>
<li>Normalisation temporelle: parsing ISO + conversion UTC si offset présent (sinon heure serveur conservée)</li>
<li>Offsets testés (trade MT5): {association_meta.get('tested_offsets_hours', [])} | offset retenu: {association_meta.get('best_offset_hours', 0)}h</li>
<li>Trades associés via offset retenu: {association_meta.get('matched_trades', 0)}/{metrics.get('total_trades', 0)}</li>
<li>Nombre de snapshots valides: {len(snaps.get('records', []))}</li>
<li>Lignes invalides: {len(snaps.get('invalid_lines', []))}</li>
<li>Champs disponibles: {', '.join(sorted(k for k in snaps.get('fields', {}).keys() if not k.startswith('_'))) or 'N/A'}</li>
<li>Spread moyen: {f"{snaps.get('spread_avg'):.3f}" if snaps.get('spread_avg') is not None else 'N/A'}</li>
<li>Distance M5 moyenne: {f"{snaps.get('m5_distance_avg'):.3f}" if snaps.get('m5_distance_avg') is not None else 'N/A'}</li>
<li>Décisions GPT: {dict(snaps.get('gpt_decisions', {}))}</li>
</ul>

<h2>5) Analyse par trade</h2>
{''.join(context_blocks) if context_blocks else '<p>Aucun trade à analyser.</p>'}

<h2>6) Skips importants</h2>
<ul>{skip_items}</ul>
<ul>{''.join(f'<li>{html.escape(item)}</li>' for item in skip_interpretation)}</ul>

<h2>7) Anomalies</h2>
<ul>{anomalies_html}</ul>

<h2>8) Aide à la décision</h2>
<ul>
<li><b>À changer maintenant:</b> {'rien à changer' if metrics.get('profit_net', 0) > 0 and len(anomalies) == 0 else 'vérifier filtres dominants et anomalies clés'}</li>
<li><b>À observer encore:</b> qualité des entrées vs M5_TOO_FAR, timeouts GPT, contexte session.</li>
<li><b>À ne pas toucher:</b> lot size, horaires de session, SL/TP, trade cap, logique qui semble fonctionner.</li>
<li><b>Priorité numéro 1:</b> {html.escape('Corriger la logique GPT timeout : jamais d’ALLOW automatique sur timeout' if any('DANGER : GPT timeout fallback ALLOW détecté' in a for a in anomalies) else (anomalies[0] if anomalies else 'Confirmer la stabilité sur plusieurs journées avant tout changement.'))}</li>
</ul>

<h2>9) Questions pour ChatGPT</h2>
<ol>
<li>La journée était-elle vraiment tradable ?</li>
<li>Les trades pris étaient-ils propres ou trop tardifs ?</li>
<li>Les SL viennent-ils d’un mauvais marché, d’une mauvaise entrée ou d’un filtre trop permissif ?</li>
<li>Les TP/BE confirment-ils que la logique actuelle est bonne ?</li>
<li>Les skips M5_TOO_FAR étaient-ils justifiés ?</li>
<li>M5_TOO_FAR protège-t-il le bot ou bloque-t-il trop de bonnes entrées ?</li>
<li>GPT a-t-il bloqué les mauvais marchés ?</li>
<li>GPT a-t-il laissé passer une entrée qu’il aurait dû bloquer ?</li>
<li>Y a-t-il eu des GPT_TIMEOUT ?</li>
<li>Un timeout GPT a-t-il été transformé en ALLOW ?</li>
<li>Le bot a-t-il bien respecté les horaires de session ?</li>
<li>Les BE sont-ils bien détectés ?</li>
<li>Quelle est la priorité numéro 1 à corriger ?</li>
<li>Qu’est-ce qu’il ne faut surtout pas modifier ?</li>
<li>Une modification du bot est-elle vraiment justifiée après cette journée ?</li>
</ol>
</body></html>"""
    return html_content


def build_resume(
    mt5: Dict[str, Any], console: Dict[str, Any], snaps: Dict[str, Any], anomalies: List[str], association_meta: Dict[str, Any]
) -> str:
    m = mt5.get("metrics", {})
    counts = console.get("event_counts", {})
    report_stats = mt5.get("report_stats", {}) or {}
    day_status, day_conclusion = evaluate_day(m, counts)

    lines = [
        "=== RÉSUMÉ DÉBRIEF BTC (copier-coller ChatGPT) ===",
        f"Trades={m.get('total_trades', 0)} | Net={m.get('profit_net', 0.0):.2f} | TP/SL/BE={m.get('tp_count', 0)}/{m.get('sl_count', 0)}/{m.get('be_count', 0)}",
        f"Jour: {day_status} | Conclusion: {day_conclusion}",
        f"Console: GPT_TIMEOUT={counts.get('GPT_TIMEOUT', 0)}, M5_TOO_FAR={counts.get('M5_TOO_FAR', 0)}, OUT_OF_SESSION={counts.get('OUT_OF_SESSION', 0)}, ALLOW={counts.get('ALLOW', 0)}, BLOCK={counts.get('BLOCK', 0)}",
        f"Contrôle rapport MT5: Nb trades={report_stats.get('nb_trades', 'N/A')}, Profit Total Net={report_stats.get('profit_total_net', 'N/A')} (sans remplacer le parsing Positions)",
        f"Snapshots: valides={len(snaps.get('records', []))}, invalides={len(snaps.get('invalid_lines', []))}, spread_moyen={snaps.get('spread_avg')}, distance_m5_moyenne={snaps.get('m5_distance_avg')}",
        f"Association snapshots: offsets_testes={association_meta.get('tested_offsets_hours', [])}, offset_retenu={association_meta.get('best_offset_hours', 0)}h, trades_associes={association_meta.get('matched_trades', 0)}/{m.get('total_trades', 0)}",
        "Skips importants (console): "
        + ", ".join(
            f"{k}={counts.get(k, 0)}"
            for k in ["ENTRY_FILTER_SKIP", "M5_TOO_FAR", "OUT_OF_SESSION", "REENTRY_TOO_CLOSE", "GPT_BLOCK", "WAIT_1", "WAIT_2"]
            if counts.get(k, 0) > 0
        )
        if any(counts.get(k, 0) > 0 for k in ["ENTRY_FILTER_SKIP", "M5_TOO_FAR", "OUT_OF_SESSION", "REENTRY_TOO_CLOSE", "GPT_BLOCK", "WAIT_1", "WAIT_2"])
        else "Skips importants (console): N/A",
        "Anomalies: " + (" | ".join(anomalies[:8]) if anomalies else "aucune"),
        "Priorité #1: "
        + (
            "Corriger la logique GPT timeout : jamais d’ALLOW automatique sur timeout"
            if any("DANGER : GPT timeout fallback ALLOW détecté" in a for a in anomalies)
            else (anomalies[0] if anomalies else "Confirmer la stabilité multi-jours avant tout changement.")
        ),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    base_dir = Path.cwd()
    out_dir = ensure_output_dir(base_dir)

    console = parse_console(base_dir / "console.txt")
    snaps = parse_snapshots(base_dir / "snapshots_btc.jsonl")
    mt5 = parse_mt5(base_dir / "mt5_history.csv")
    charts = parse_charts(base_dir / "charts")

    contexts, association_meta = associate_trade_context(mt5, console, snaps, charts)
    anomalies = detect_anomalies(console, snaps, mt5, contexts)

    for src in (console, snaps, mt5, charts):
        if src.get("warning"):
            anomalies.append(src["warning"])

    html_content = build_html(base_dir, out_dir, console, snaps, mt5, charts, contexts, anomalies, association_meta)
    resume = build_resume(mt5, console, snaps, anomalies, association_meta)

    (out_dir / "rapport_journalier.html").write_text(html_content, encoding="utf-8")
    (out_dir / "resume_chatgpt.txt").write_text(resume, encoding="utf-8")
    (out_dir / "anomalies.txt").write_text("\n".join(anomalies) + "\n", encoding="utf-8")

    print("Débrief généré dans OUTPUT/")
    print("- rapport_journalier.html")
    print("- resume_chatgpt.txt")
    print("- anomalies.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
