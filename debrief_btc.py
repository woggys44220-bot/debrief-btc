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

ENTRY_CONTEXT_BEFORE_MINUTES = 15
ENTRY_CONTEXT_AFTER_MINUTES = 2
WEAK_CONFIDENCE_GAP_MINUTES = 15

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


@dataclass(frozen=True)
class TradeOutcome:
    outcome_detected: str
    reason: str


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
        "SL_PROFIT": "CLOSE_DETECTED_SL_PROFIT",
        "TP": "CLOSE_DETECTED_TP",
        "BE": "CLOSE_DETECTED_BE",
        "SL": "CLOSE_DETECTED_SL",
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
                token_as_suffix = f"CLOSE_DETECTED_{token}"
                if token_as_suffix in line_upper or re.search(rf"(?<![A-Z0-9_]){re.escape(token)}(?![A-Z0-9_])", line_upper):
                    event_counts[close_event] += 1
                    matches.append({"line": i, "timestamp": ts, "event": close_event, "raw": line})
                    break

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
    outcomes = Counter(detect_trade_outcome(t).outcome_detected for t in trades)

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
            "tp_count": outcomes.get("TP", 0),
            "sl_count": outcomes.get("SL", 0),
            "be_count": outcomes.get("BE", 0),
            "sl_profit_count": outcomes.get("SL_PROFIT", 0),
            "unknown_count": outcomes.get("UNKNOWN", 0),
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


def price_near(reference: Optional[float], actual: Optional[float], *, planned_distance: Optional[float] = None) -> bool:
    if reference is None or actual is None:
        return False
    tolerance = 5.0
    if planned_distance is not None and planned_distance > 0:
        tolerance = max(tolerance, planned_distance * 0.05)
    else:
        tolerance = max(tolerance, abs(reference) * 0.0001)
    return abs(actual - reference) <= tolerance


def detect_trade_outcome(trade: Trade) -> TradeOutcome:
    if trade.exit_price is None or trade.profit is None:
        return TradeOutcome("UNKNOWN", "unknown")

    planned_tp_distance = (
        abs(trade.tp - trade.entry_price)
        if trade.tp is not None and trade.entry_price is not None
        else None
    )
    planned_sl_distance = (
        abs(trade.sl - trade.entry_price)
        if trade.sl is not None and trade.entry_price is not None
        else None
    )

    if price_near(trade.tp, trade.exit_price, planned_distance=planned_tp_distance):
        return TradeOutcome("TP", "close_near_tp")
    if price_near(trade.entry_price, trade.exit_price, planned_distance=planned_tp_distance):
        return TradeOutcome("BE", "close_near_entry")
    if trade.profit <= 0 and price_near(trade.sl, trade.exit_price, planned_distance=planned_sl_distance):
        return TradeOutcome("SL", "close_near_sl")
    if trade.profit > 0:
        return TradeOutcome("SL_PROFIT", "sl_moved_in_profit")
    return TradeOutcome("UNKNOWN", "unknown")


def classify_trade_result(trade: Trade) -> str:
    return detect_trade_outcome(trade).outcome_detected




def is_bot_lot(lot: Optional[float]) -> bool:
    return lot is not None and abs(lot - 0.05) < 1e-9


def classify_trade_origin(trade: Trade, snapshots: List[Dict[str, Any]]) -> str:
    if not is_bot_lot(trade.lot):
        return "manuel/inconnu"

    trade_ids = {"ticket": trade.ticket, "order": trade.order, "deal": trade.deal}
    for snap in snapshots:
        if not snapshot_is_order_ok(snap):
            continue
        for family, value in trade_ids.items():
            if value and snapshot_identifier(snap, family) == str(value):
                return "bot"

    # fallback: lot bot + ticket présent côté trade => bot probable
    if trade.ticket:
        return "bot"
    return "manuel/inconnu"


def summarize_trade_detection(mt5: Dict[str, Any], snaps: Dict[str, Any]) -> Dict[str, Any]:
    trades: List[Trade] = mt5.get("trades", [])
    snapshots: List[Dict[str, Any]] = snaps.get("records", [])

    bot_trades = []
    manual_unknown_trades = []
    open_trades = []
    closed_trades = []

    matched_snapshot_keys = set()
    matching_ticket = False

    for t in trades:
        origin = classify_trade_origin(t, snapshots)
        if origin == "bot":
            bot_trades.append(t)
        else:
            manual_unknown_trades.append(t)

        if t.close_time is None:
            open_trades.append(t)
        else:
            closed_trades.append(t)

        if is_bot_lot(t.lot):
            for s in snapshots:
                if not snapshot_is_order_ok(s):
                    continue
                for family, value in (("ticket", t.ticket), ("order", t.order), ("deal", t.deal)):
                    if value and snapshot_identifier(s, family) == str(value):
                        matching_ticket = True
                        matched_snapshot_keys.add(id(s))
                        break

    order_ok_snaps = [s for s in snapshots if snapshot_is_order_ok(s)]
    unmatched_order_ok = [s for s in order_ok_snaps if id(s) not in matched_snapshot_keys]

    unknown_closed_snapshots = []
    for s in snapshots:
        status = str(s.get("status") or s.get("event") or "").upper()
        if "UNKNOWN" in status and any(k in status for k in ("CLOSE", "CLOSED", "FERME")):
            unknown_closed_snapshots.append(s)

    return {
        "bot_trades": bot_trades,
        "manual_unknown_trades": manual_unknown_trades,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "unmatched_order_ok": unmatched_order_ok,
        "unknown_closed_snapshots": unknown_closed_snapshots,
        "matching_ticket": matching_ticket,
        "mt5_read_ok": bool(trades) or not bool(mt5.get("warning")),
        "snapshots_read_ok": bool(snapshots) or not bool(snaps.get("warning")),
    }

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


def has_side_and_entry(snapshot: Optional[Dict[str, Any]]) -> bool:
    if not snapshot:
        return False
    return snapshot_side(snapshot) in {"BUY", "SELL"} and snapshot_price(
        snapshot, ("entry", "entry_price", "price", "open_price")
    ) is not None


def price_delta_ok(mt5_price: Optional[float], snapshot_entry: Optional[float]) -> bool:
    if mt5_price is None or snapshot_entry is None:
        return False
    tolerance = max(20.0, abs(mt5_price) * 0.003)
    return abs(mt5_price - snapshot_entry) <= tolerance


def select_by_time(candidates: List[Dict[str, Any]], anchor: datetime) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs((s.get("_timestamp") - anchor).total_seconds()) if s.get("_timestamp") else 1e18)


def gap_minutes(anchor: datetime, snapshot: Optional[Dict[str, Any]]) -> Optional[float]:
    if not snapshot or not snapshot.get("_timestamp"):
        return None
    return abs((snapshot["_timestamp"] - anchor).total_seconds()) / 60.0


def min_gap_minutes(anchor: datetime, snapshots: List[Dict[str, Any]]) -> Optional[float]:
    timed = [s for s in snapshots if s.get("_timestamp")]
    if not timed:
        return None
    return min(abs((s["_timestamp"] - anchor).total_seconds()) / 60.0 for s in timed)


def best_snapshot_before(anchor: datetime, snapshots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [s for s in snapshots if s.get("_timestamp") and s["_timestamp"] <= anchor]
    return max(candidates, key=lambda s: s["_timestamp"]) if candidates else None


def best_snapshot_after(anchor: datetime, snapshots: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [s for s in snapshots if s.get("_timestamp") and s["_timestamp"] > anchor]
    return min(candidates, key=lambda s: s["_timestamp"]) if candidates else None


def is_entry_context_window(anchor: datetime, snapshot: Optional[Dict[str, Any]]) -> bool:
    if not snapshot or not snapshot.get("_timestamp"):
        return False
    ts = snapshot["_timestamp"]
    lower_bound = anchor - timedelta(minutes=ENTRY_CONTEXT_BEFORE_MINUTES)
    upper_bound = anchor + timedelta(minutes=ENTRY_CONTEXT_AFTER_MINUTES)
    return lower_bound <= ts <= upper_bound


def match_trade_snapshot(trade: Trade, snapshots: List[Dict[str, Any]], anchor: datetime) -> Dict[str, Any]:
    before_snapshot = best_snapshot_before(anchor, snapshots)
    after_snapshot = best_snapshot_after(anchor, snapshots)
    closest_snapshot = select_by_time([s for s in snapshots if s.get("_timestamp")], anchor)
    closest_time = closest_snapshot.get("_timestamp") if closest_snapshot else None
    closest_gap_min = abs((closest_time - anchor).total_seconds()) / 60.0 if closest_time else None
    closest_entry = snapshot_price(closest_snapshot, ("entry", "entry_price", "price", "open_price")) if closest_snapshot else None

    trade_side = normalize_side(trade.side)
    trade_ids = {"ticket": trade.ticket, "order": trade.order, "deal": trade.deal}
    ids_available = any(trade_ids.values())

    windowed_snapshots = [s for s in snapshots if is_entry_context_window(anchor, s)]

    id_candidates = []
    id_incomplete_candidates = []
    if ids_available:
        for snap in windowed_snapshots:
            if not snap.get("_timestamp"):
                continue
            for family, value in trade_ids.items():
                if value and snapshot_identifier(snap, family) == str(value):
                    if has_side_and_entry(snap):
                        id_candidates.append(snap)
                    else:
                        id_incomplete_candidates.append(snap)
                    break

    selected = select_by_time(id_candidates, anchor)
    method = "A:id(ticket/order/deal)"
    confidence = "haute"

    if not selected:
        order_ok_candidates = [s for s in windowed_snapshots if s.get("_timestamp") and snapshot_is_order_ok(s)]
        selected = select_by_time(order_ok_candidates, anchor)
        method = "B:ORDER_OK plus proche"
        confidence = "moyenne"

    if not selected:
        c_candidates = []
        for snap in windowed_snapshots:
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
        for snap in windowed_snapshots:
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

    if not selected and id_incomplete_candidates:
        selected = select_by_time(id_incomplete_candidates, anchor)
        method = "A':matching par ticket seulement, contexte incomplet"
        confidence = "faible"

    if not selected:
        failure_reasons = []
        if not windowed_snapshots:
            failure_reasons.append("Aucun snapshot dans la fenêtre -15/+2 min autour de l'heure normalisée.")
        if windowed_snapshots and trade_side not in {"BUY", "SELL"}:
            failure_reasons.append("Side trade MT5 absent ou non normalisable.")
        if windowed_snapshots and trade.entry_price is None:
            failure_reasons.append("Entry MT5 absente, impossible de valider la proximité de prix.")
        if ids_available and not id_candidates:
            failure_reasons.append("IDs ticket/order/deal présents côté trade mais absents des snapshots proches.")
        if windowed_snapshots and not any(snapshot_is_order_ok(s) for s in windowed_snapshots):
            failure_reasons.append("Aucun statut ORDER_OK dans la fenêtre proche.")
        if windowed_snapshots and trade_side in {"BUY", "SELL"} and trade.entry_price is not None:
            has_side = any(snapshot_side(s) == trade_side for s in windowed_snapshots)
            if not has_side:
                failure_reasons.append("Aucun snapshot proche avec le même side.")
            else:
                has_side_entry = any(
                    price_delta_ok(trade.entry_price, snapshot_price(s, ("entry", "entry_price", "price", "open_price")))
                    for s in windowed_snapshots
                    if snapshot_side(s) == trade_side
                )
                if not has_side_entry:
                    failure_reasons.append("Snapshots avec bon side mais entry trop éloignée.")
        if not failure_reasons:
            failure_reasons.append("Aucun candidat n'a satisfait les règles de matching.")

        return {
            "selected": None,
            "method": "aucune",
            "confidence": "faible",
            "entry_context_usable": False,
            "before_snapshot": before_snapshot,
            "after_snapshot": after_snapshot,
            "diagnostic": {
                "trade_time": anchor,
                "closest_snapshot_time": closest_time,
                "gap_minutes": closest_gap_min,
                "min_gap_minutes": min_gap_minutes(anchor, snapshots),
                "entry_price_diff": abs(trade.entry_price - closest_entry) if trade.entry_price is not None and closest_entry is not None else None,
                "trade_ticket": trade.ticket,
                "trade_order": trade.order,
                "trade_deal": trade.deal,
                "snapshot_ticket": snapshot_identifier(closest_snapshot, "ticket") if closest_snapshot else None,
                "snapshot_order": snapshot_identifier(closest_snapshot, "order") if closest_snapshot else None,
                "snapshot_deal": snapshot_identifier(closest_snapshot, "deal") if closest_snapshot else None,
                "failure_reasons": failure_reasons,
            },
        }

    selected_time = selected.get("_timestamp")
    selected_entry = snapshot_price(selected, ("entry", "entry_price", "price", "open_price"))
    selected_gap = abs((selected_time - anchor).total_seconds()) / 60.0 if selected_time else None
    has_entry_fields = has_side_and_entry(selected)
    entry_context_usable = is_entry_context_window(anchor, selected) and has_entry_fields
    real_confidence = confidence
    if selected_gap is not None and selected_gap > WEAK_CONFIDENCE_GAP_MINUTES:
        real_confidence = "faible"
    if real_confidence == "haute" and (selected_gap is None or selected_gap > WEAK_CONFIDENCE_GAP_MINUTES):
        real_confidence = "moyenne"
    if not has_entry_fields:
        real_confidence = "faible"

    prior_buy_close = None
    if trade.entry_price is not None:
        prior_buy_candidates = []
        for snap in windowed_snapshots:
            ts = snap.get("_timestamp")
            if not ts or ts > anchor:
                continue
            if snapshot_side(snap) != "BUY":
                continue
            snap_entry = snapshot_price(snap, ("entry", "entry_price", "price", "open_price"))
            if price_delta_ok(trade.entry_price, snap_entry):
                prior_buy_candidates.append(snap)
        prior_buy_close = select_by_time(prior_buy_candidates, anchor)

    prior_buy_not_retained_reason = ""
    if prior_buy_close and selected is not prior_buy_close:
        prior_buy_not_retained_reason = (
            f"Un snapshot BUY avant entrée ({fmt_dt(prior_buy_close.get('_timestamp'))}) avec entry proche existe, "
            f"mais non retenu car la méthode prioritaire a sélectionné '{method}'."
        )

    return {
        "selected": selected,
        "method": method,
        "confidence": real_confidence,
        "entry_context_usable": entry_context_usable,
        "before_snapshot": before_snapshot,
        "after_snapshot": after_snapshot,
        "diagnostic": {
            "trade_time": anchor,
            "closest_snapshot_time": closest_time,
            "gap_minutes": selected_gap,
            "min_gap_minutes": min_gap_minutes(anchor, snapshots),
            "entry_price_diff": abs(trade.entry_price - selected_entry) if trade.entry_price is not None and selected_entry is not None else None,
            "trade_ticket": trade.ticket,
            "trade_order": trade.order,
            "trade_deal": trade.deal,
            "snapshot_ticket": snapshot_identifier(selected, "ticket"),
            "snapshot_order": snapshot_identifier(selected, "order"),
            "snapshot_deal": snapshot_identifier(selected, "deal"),
            "selected_has_side_entry": has_entry_fields,
            "ticket_match_found": bool(id_candidates or id_incomplete_candidates),
            "ticket_only_incomplete": method == "A':matching par ticket seulement, contexte incomplet",
            "prior_buy_not_retained_reason": prior_buy_not_retained_reason,
            "failure_reasons": [],
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
                        "entry_context_usable": False,
                        "best_snapshot_before": None,
                        "best_snapshot_after": None,
                        "mt5_raw_time": None,
                        "normalized_trade_time": None,
                        "offset_hours": offset_hours,
                    }
                )
                continue

            shifted_anchor = anchor + timedelta(hours=offset_hours)
            assoc = match_trade_snapshot(t, snapshots, shifted_anchor)
            selected = assoc["selected"]
            near_console = [e for e in console_events if e.get("timestamp") and abs(e["timestamp"] - shifted_anchor) <= timedelta(minutes=20)]
            near_charts = [c for c in chart_items if c.timestamp and abs(c.timestamp - shifted_anchor) <= timedelta(minutes=20)]

            confidence = assoc["confidence"]
            entry_context_usable = assoc.get("entry_context_usable", False)
            comment = "Contexte non exploitable — analyse graphique nécessaire"

            items.append(
                {
                    "trade": t,
                    "snapshots": [selected] if selected else [],
                    "console_events": near_console[:8],
                    "charts": near_charts,
                    "comment": comment,
                    "snapshot_debug": assoc["diagnostic"],
                    "association_method": assoc["method"],
                    "association_confidence": confidence,
                    "entry_context_usable": entry_context_usable,
                    "best_snapshot_before": assoc.get("before_snapshot"),
                    "best_snapshot_after": assoc.get("after_snapshot"),
                    "mt5_raw_time": anchor,
                    "normalized_trade_time": shifted_anchor,
                    "offset_hours": offset_hours,
                }
            )
        return items

    best_contexts = []
    best_offset = 0
    best_score = (-1, -1, -1, -999)
    for offset in SNAPSHOT_ASSOCIATION_OFFSETS_HOURS:
        contexts = build_with_offset(offset)
        matched = sum(1 for c in contexts if c.get("entry_context_usable"))
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
        "weak_trades": sum(1 for c in best_contexts if c.get("association_confidence") == "faible"),
        "ticket_only_incomplete_trades": sum(
            1
            for c in best_contexts
            if c.get("association_method") == "A':matching par ticket seulement, contexte incomplet"
        ),
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
        anomalies_high.append("GPT_TIMEOUT_FALLBACK_ALLOW détecté : corrigé dans V12C, mais dangereux dans les anciens bots.")
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
        detection = summarize_trade_detection(mt5, snaps)
        if len(detection.get("bot_trades", [])) == 0:
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


def snapshot_summary_line(snapshot: Optional[Dict[str, Any]], anchor: Optional[datetime], label: str) -> str:
    if not snapshot:
        return f"{label}: aucun"
    ts = snapshot.get("_timestamp")
    gap = abs((ts - anchor).total_seconds()) / 60.0 if (ts and anchor) else None
    entry_val = snapshot_price(snapshot, ("entry", "entry_price", "price", "open_price"))
    return (
        f"{label}: {fmt_dt(ts)}"
        f" | écart={f'{gap:.2f} min' if gap is not None else 'N/A'}"
        f" | side={html.escape(snapshot_side(snapshot))}"
        f" | entry={entry_val if entry_val is not None else 'N/A'}"
        f" | décision GPT={html.escape(snapshot_value(snapshot, ('gpt_decision', 'decision_gpt', 'decision', 'gpt')))}"
    )


def is_matching_reliable(trade: Trade, snapshot: Optional[Dict[str, Any]], diagnostic: Dict[str, Any]) -> Tuple[str, str]:
    if not snapshot:
        return "NON", "Aucun snapshot retenu."
    reasons = []
    trade_side = normalize_side(trade.side)
    snap_side = snapshot_side(snapshot)
    same_side = trade_side in {"BUY", "SELL"} and snap_side == trade_side

    trade_entry = trade.entry_price
    snap_entry = snapshot_price(snapshot, ("entry", "entry_price", "price", "open_price"))
    delta_entry = abs(trade_entry - snap_entry) if trade_entry is not None and snap_entry is not None else None

    gap = diagnostic.get("gap_minutes")
    reasons.append(f"écart après offset={gap:.2f} min" if gap is not None else "écart après offset=N/A")
    reasons.append("side identique" if same_side else "side différent/absent")
    reasons.append(f"delta_entry={delta_entry:.2f}" if delta_entry is not None else "delta_entry=N/A")

    if gap is not None and gap > WEAK_CONFIDENCE_GAP_MINUTES:
        return "NON", ", ".join(reasons + ["écart > 15 min"])
    if gap is not None and gap <= 2.0 and same_side and delta_entry is not None:
        if delta_entry <= 5:
            return "OUI", ", ".join(reasons + ["règle <=2min + side + delta_entry<=5"])
        if delta_entry <= 15:
            return "MOYEN", ", ".join(reasons + ["règle <=2min + side + delta_entry<=15"])
    return "NON", ", ".join(reasons + ["conditions de matching fiable non atteintes"])



def fmt_value(value: Any, decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{decimals}f}"
    return str(value)


def trade_net_result(trade: Trade) -> Optional[float]:
    if trade.profit is None:
        return None
    return trade.profit + (trade.commission or 0.0) + (trade.swap or 0.0)


def classify_day_quality(metrics: Dict[str, Any]) -> str:
    pnl = metrics.get("profit_net", 0.0) or 0.0
    sl_count = metrics.get("sl_count", 0) or 0
    tp_count = metrics.get("tp_count", 0) or 0
    sl_profit_count = metrics.get("sl_profit_count", 0) or 0
    if abs(pnl) < 0.01:
        return "JOURNÉE_NEUTRE"
    if pnl < 0:
        return "JOURNÉE_ROUGE"
    if sl_profit_count > 0 and tp_count == 0 and sl_count == 0:
        return "JOURNÉE_PROTÉGÉE"
    if pnl > 0 and sl_count == 0:
        return "JOURNÉE_PROPRE_VERTE"
    if pnl > 0 and sl_count > 0:
        return "JOURNÉE_VERTE_AVEC_SL"
    return "JOURNÉE_NEUTRE"


def analyze_stop_after_gain(trades: List[Trade]) -> Dict[str, Any]:
    real_profit = sum((trade_net_result(t) or 0.0) for t in trades)
    outcomes = [detect_trade_outcome(t).outcome_detected for t in trades]
    first_trade = trades[0] if trades else None

    first_tp_idx = next((i for i, outcome in enumerate(outcomes) if outcome == "TP"), None)
    first_winner_idx = next(
        (
            i
            for i, (trade, outcome) in enumerate(zip(trades, outcomes))
            if outcome in {"TP", "SL_PROFIT"} and (trade_net_result(trade) or 0.0) > 0
        ),
        None,
    )

    after_first_winner = trades[first_winner_idx + 1 :] if first_winner_idx is not None else []
    after_first_winner_profit = sum((trade_net_result(t) or 0.0) for t in after_first_winner)
    stop_after_tp_profit = (
        sum((trade_net_result(t) or 0.0) for t in trades[: first_tp_idx + 1])
        if first_tp_idx is not None
        else None
    )
    stop_after_winner_profit = (
        sum((trade_net_result(t) or 0.0) for t in trades[: first_winner_idx + 1])
        if first_winner_idx is not None
        else None
    )
    diff_vs_real = stop_after_tp_profit - real_profit if stop_after_tp_profit is not None else None

    if first_tp_idx is None or len(trades) <= first_tp_idx + 1:
        conclusion = "PAS_ASSEZ_DE_DONNÉES"
    elif diff_vs_real is not None and diff_vs_real > 0.01:
        conclusion = "STOP_APRES_TP_AURAIT_AIDÉ"
    elif diff_vs_real is not None and diff_vs_real < -0.01:
        conclusion = "STOP_APRES_TP_AURAIT_COÛTÉ"
    else:
        conclusion = "PAS_ASSEZ_DE_DONNÉES"

    return {
        "premier_trade_result": detect_trade_outcome(first_trade).outcome_detected if first_trade else "N/A",
        "premier_trade_profit": trade_net_result(first_trade) if first_trade else None,
        "trades_apres_premier_gain": len(after_first_winner),
        "resultat_trades_apres_premier_gain": after_first_winner_profit,
        "profit_si_stop_apres_premier_tp": stop_after_tp_profit,
        "profit_si_stop_apres_premier_trade_gagnant": stop_after_winner_profit,
        "difference_vs_reel": diff_vs_real,
        "difference_stop_gagnant_vs_reel": stop_after_winner_profit - real_profit if stop_after_winner_profit is not None else None,
        "conclusion": conclusion,
    }


def snapshot_field(snapshot: Optional[Dict[str, Any]], keys: Tuple[str, ...]) -> Any:
    if not snapshot:
        return None
    for key in keys:
        if key in snapshot and snapshot.get(key) not in (None, ""):
            return snapshot.get(key)
    return None


def snapshot_bool(snapshot: Optional[Dict[str, Any]], keys: Tuple[str, ...]) -> Optional[bool]:
    value = snapshot_field(snapshot, keys)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper()
    if text in {"1", "TRUE", "YES", "OUI", "ALLOW", "OK"}:
        return True
    if text in {"0", "FALSE", "NO", "NON", "NONE", "N/A"}:
        return False
    return bool(text)


def snapshot_float_field(snapshot: Optional[Dict[str, Any]], keys: Tuple[str, ...]) -> Optional[float]:
    return safe_float(snapshot_field(snapshot, keys))


def gpt_allows(decision: str, status: str) -> bool:
    text = f"{decision} {status}".upper()
    return "ALLOW" in text or "ORDER_OK" in text or "GPT_ALLOW" in text


def compute_entry_quality(ctx: Dict[str, Any]) -> Dict[str, Any]:
    trade: Trade = ctx["trade"]
    snapshot = ctx["snapshots"][0] if ctx.get("snapshots") else None
    console_events = {e.get("event") for e in ctx.get("console_events", [])}

    setup_type = snapshot_field(snapshot, ("setup_type", "setup", "pattern"))
    route_name = snapshot_field(snapshot, ("route_name", "route", "strategy_route"))
    decision = snapshot_value(snapshot, ("gpt_decision", "decision_gpt", "decision", "gpt")) if snapshot else "N/A"
    status = snapshot_value(snapshot, ("status", "result", "event", "gpt_status")) if snapshot else "N/A"
    distance_ema20 = snapshot_float_field(snapshot, ("distance_ema20_m5", "ema20_m5_distance", "dist_ema20_m5", "distance_m5_ema20"))
    distance_ema50 = snapshot_float_field(snapshot, ("distance_ema50_m5", "ema50_m5_distance", "dist_ema50_m5", "distance_m5_ema50"))
    atr5 = snapshot_float_field(snapshot, ("atr5", "atr_m5", "atr_5", "m5_atr"))
    atr15 = snapshot_float_field(snapshot, ("atr15", "atr_m15", "atr_15", "m15_atr"))
    market_dirty = snapshot_bool(snapshot, ("market_dirty", "dirty_market", "is_market_dirty"))
    market_too_tight = snapshot_bool(snapshot, ("market_too_tight", "too_tight", "range_too_tight"))
    too_extended = snapshot_bool(snapshot, ("too_extended", "market_too_extended", "after_big_move", "big_move"))
    pullback_near_ema = snapshot_bool(snapshot, ("pullback_near_ema", "near_ema_pullback", "pullback_ema"))
    ema_reject = snapshot_bool(snapshot, ("ema_reject", "reject_ema", "ema_rejection"))
    bearish_resume = snapshot_bool(snapshot, ("bearish_resume", "resume_bearish"))
    bullish_resume = snapshot_bool(snapshot, ("bullish_resume", "resume_bullish"))
    same_side_reentry = snapshot_bool(snapshot, ("same_side_reentry", "same_side_reentry_detected"))

    side = normalize_side(trade.side) if normalize_side(trade.side) != "N/A" else (snapshot_side(snapshot) if snapshot else "N/A")
    coherent_resume = bullish_resume if side == "BUY" else bearish_resume if side == "SELL" else None
    coherent_signal = any(value is True for value in (pullback_near_ema, ema_reject, coherent_resume))
    ema_values = [v for v in (distance_ema20, distance_ema50) if v is not None]
    elevated_ema_distance = any(abs(v) >= 150 for v in ema_values)
    reentry_risk = same_side_reentry is True or "REENTRY_TOO_CLOSE" in console_events

    if not snapshot:
        verdict = "INCONNU"
    elif too_extended is True or reentry_risk or elevated_ema_distance:
        verdict = "RISQUÉ"
    elif gpt_allows(decision, status) and market_dirty is not True and too_extended is not True and coherent_signal:
        verdict = "PROPRE"
    elif gpt_allows(decision, status):
        verdict = "LIMITE"
    else:
        verdict = "INCONNU"

    return {
        "trade": trade,
        "setup_type": setup_type,
        "route_name": route_name,
        "side": side,
        "decision_gpt": decision,
        "status_gpt": status,
        "entry": (snapshot_price(snapshot, ("entry", "entry_price", "price", "open_price")) if snapshot else None) or trade.entry_price,
        "sl": (snapshot_price(snapshot, ("sl", "stop_loss", "stoploss")) if snapshot else None) or trade.sl,
        "tp": (snapshot_price(snapshot, ("tp", "take_profit", "takeprofit")) if snapshot else None) or trade.tp,
        "distance_ema20_m5": distance_ema20,
        "distance_ema50_m5": distance_ema50,
        "atr5": atr5,
        "atr15": atr15,
        "market_dirty": market_dirty,
        "market_too_tight": market_too_tight,
        "too_extended": too_extended,
        "pullback_near_ema": pullback_near_ema,
        "ema_reject": ema_reject,
        "bearish_resume": bearish_resume,
        "bullish_resume": bullish_resume,
        "verdict_entree": verdict,
    }


def report_confidence_label(overall_confidence: str) -> str:
    return {"bonne": "BON", "moyenne": "MOYEN", "faible": "FAIBLE"}.get(overall_confidence, "FAIBLE")

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
    final_close_counts = {
        "CLOSE_DETECTED_TP": metrics.get("tp_count", 0),
        "CLOSE_DETECTED_SL": metrics.get("sl_count", 0),
        "CLOSE_DETECTED_BE": metrics.get("be_count", 0),
        "CLOSE_DETECTED_SL_PROFIT": metrics.get("sl_profit_count", 0),
    }
    day_status, day_conclusion = evaluate_day(metrics, counts)
    day_quality_verdict = classify_day_quality(metrics)
    stop_gain = analyze_stop_after_gain(mt5.get("trades", []))
    detection = summarize_trade_detection(mt5, snaps)

    matching_context_count = 0
    matching_final_count = 0
    confidence_levels = []
    for ctx in contexts:
        trade = ctx.get("trade")
        selected_snapshot = ctx["snapshots"][0] if ctx.get("snapshots") else None
        reliable_status, _ = is_matching_reliable(trade, selected_snapshot, ctx.get("snapshot_debug", {}))
        if ctx.get("entry_context_usable"):
            matching_context_count += 1
        if reliable_status in {"OUI", "MOYEN"}:
            matching_final_count += 1
        confidence_levels.append(ctx.get("association_confidence", "faible"))

    parsed_mt5_trades = len(mt5.get("trades", []))
    report_nb_trades = report_stats.get("nb_trades")
    report_nb_trades_int = int(report_nb_trades) if report_nb_trades is not None else None
    mt5_total_trades = max(parsed_mt5_trades, report_nb_trades_int or 0)

    bot_retained_trades = len(detection.get("bot_trades", []))
    manual_unknown_excluded = max(
        len(detection.get("manual_unknown_trades", [])),
        mt5_total_trades - bot_retained_trades,
    )

    confidence_map = {"haute": "bonne", "moyenne": "moyenne", "faible": "faible"}
    confidence_rank = {"bonne": 3, "moyenne": 2, "faible": 1}
    overall_confidence = "faible"
    if confidence_levels:
        mapped = [confidence_map.get(level, "faible") for level in confidence_levels]
        overall_confidence = min(mapped, key=lambda value: confidence_rank[value])

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
    timed_snapshots = [s for s in snaps.get("records", []) if s.get("_timestamp")]
    first_snapshot = min(timed_snapshots, key=lambda s: s["_timestamp"]) if timed_snapshots else None
    last_snapshot = max(timed_snapshots, key=lambda s: s["_timestamp"]) if timed_snapshots else None
    for ctx in contexts:
        t: Trade = ctx["trade"]
        trade_anchor = ctx.get("normalized_trade_time") or (t.open_time or t.close_time)
        snap_debug = ctx.get("snapshot_debug", {})
        selected_snapshot = ctx["snapshots"][0] if ctx["snapshots"] else None
        best_before = ctx.get("best_snapshot_before")
        best_after = ctx.get("best_snapshot_after")
        offset_hours = ctx.get("offset_hours", association_meta.get("best_offset_hours", 0))
        images = " ".join(
            f'<a href="../{html.escape(rel(c.path))}">{html.escape(c.path.name)}</a>' for c in ctx["charts"]
        ) or "Aucune"
        evs = ", ".join(sorted({e["event"] for e in ctx["console_events"]})) or "Aucun"

        before_line = snapshot_summary_line(best_before, trade_anchor, "Meilleur snapshot AVANT entrée")
        after_line = snapshot_summary_line(best_after, trade_anchor, "Meilleur snapshot APRÈS entrée")
        reliable_status, reliable_reason = is_matching_reliable(t, selected_snapshot, snap_debug)
        outcome = detect_trade_outcome(t)
        context_exploitable = ctx.get("entry_context_usable", False) and reliable_status in {"OUI", "MOYEN"}
        real_gap = snap_debug.get("gap_minutes")
        gpt_decision = (
            snapshot_value(selected_snapshot, ("gpt_decision", "decision_gpt", "decision", "gpt"))
            if selected_snapshot
            else "N/A"
        )
        auto_comment = (
            "Snapshot exploitable — analyse graphique recommandée"
            if context_exploitable
            else "Contexte non exploitable — analyse graphique nécessaire"
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
            f"{html.escape(str(snap_debug.get('snapshot_deal') or 'N/A'))} | "
            f"ticket_trouvé={snap_debug.get('ticket_match_found', False)} | "
            f"contexte_entrée_fiable={snap_debug.get('selected_has_side_entry', False)}</p>"
        )
        if snap_debug.get("prior_buy_not_retained_reason"):
            diag_line += (
                f"<p><b>Pourquoi un BUY avant n'est pas retenu:</b> "
                f"{html.escape(snap_debug.get('prior_buy_not_retained_reason'))}</p>"
            )

        unmatched_details = ""
        if not selected_snapshot:
            failure_reasons = snap_debug.get("failure_reasons", [])
            min_gap = snap_debug.get("min_gap_minutes")
            unmatched_details = (
                "<p><b>Matching échoué:</b> "
                + html.escape(" | ".join(failure_reasons) if failure_reasons else "Raison non disponible")
                + "</p>"
                + f"<p><b>Premier snapshot du fichier:</b> {fmt_dt(first_snapshot.get('_timestamp')) if first_snapshot else 'Aucun'}"
                + f" | <b>Dernier snapshot du fichier:</b> {fmt_dt(last_snapshot.get('_timestamp')) if last_snapshot else 'Aucun'}</p>"
                + f"<p><b>Écart minimal après offset:</b> {f'{min_gap:.2f} min' if min_gap is not None else 'N/A'}</p>"
            )

        context_blocks.append(
            "<div class='card'>"
            f"<h4>Trade #{t.index} — {html.escape(t.side)} — profit={t.profit}</h4>"
            f"<p><b>Heure MT5 brute:</b> {fmt_dt(ctx.get('mt5_raw_time'))} | "
            f"<b>Offset appliqué:</b> {offset_hours:+d}h | "
            f"<b>Heure normalisée (matching):</b> {fmt_dt(trade_anchor)} | "
            f"<b>Heure snapshot retenu:</b> {fmt_dt(selected_snapshot.get('_timestamp')) if selected_snapshot else 'Aucun'} | "
            f"<b>Écart réel après offset:</b> {f'{real_gap:.2f} min' if real_gap is not None else 'N/A'}</p>"
            f"<p><b>Résultat:</b> {outcome.outcome_detected} | "
            f"<b>outcome_detected=</b>{outcome.outcome_detected} | "
            f"<b>reason=</b>{outcome.reason}</p>"
            f"<p><b>Snapshots associés:</b> {len(ctx['snapshots'])} | <b>Événements console:</b> {html.escape(evs)}</p>"
            f"<p><b>Méthode d'association:</b> {html.escape(ctx.get('association_method', 'N/A'))} | "
            f"<b>Confiance:</b> {html.escape(ctx.get('association_confidence', 'faible'))}</p>"
            f"<p><b>{before_line}</b></p>"
            f"<p><b>{after_line}</b></p>"
            f"<p><b>Snapshot retenu pour contexte d'entrée:</b> {fmt_dt(selected_snapshot.get('_timestamp')) if selected_snapshot else 'Aucun'} | "
            f"<b>Contexte exploitable:</b> {'contexte snapshot exploitable' if context_exploitable else 'contexte non exploitable'}</p>"
            f"<p><b>Matching fiable:</b> {reliable_status} | <b>Raison:</b> {html.escape(reliable_reason)} | "
            f"<b>Décision GPT snapshot:</b> {html.escape(gpt_decision)}</p>"
            f"<p><b>Charts liées:</b> {images}</p>"
            f"<p><b>Commentaire automatique:</b> {html.escape(auto_comment)}</p>"
            f"{diag_line}"
            f"{unmatched_details}"
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

    entry_quality_rows = []
    entry_quality_items = [compute_entry_quality(ctx) for ctx in contexts]
    for item in entry_quality_items:
        t = item["trade"]
        entry_quality_rows.append(
            "<tr>"
            f"<td>{t.index}</td>"
            f"<td>{html.escape(str(item['setup_type'] or 'N/A'))}</td>"
            f"<td>{html.escape(str(item['route_name'] or 'N/A'))}</td>"
            f"<td>{html.escape(str(item['side'] or 'N/A'))}</td>"
            f"<td>{html.escape(str(item['decision_gpt']))}</td>"
            f"<td>{html.escape(str(item['status_gpt']))}</td>"
            f"<td>{fmt_value(item['entry'])}</td>"
            f"<td>{fmt_value(item['sl'])}</td>"
            f"<td>{fmt_value(item['tp'])}</td>"
            f"<td>{fmt_value(item['distance_ema20_m5'])}</td>"
            f"<td>{fmt_value(item['distance_ema50_m5'])}</td>"
            f"<td>{fmt_value(item['atr5'])}</td>"
            f"<td>{fmt_value(item['atr15'])}</td>"
            f"<td>{fmt_value(item['market_dirty'])}</td>"
            f"<td>{fmt_value(item['market_too_tight'])}</td>"
            f"<td>{fmt_value(item['too_extended'])}</td>"
            f"<td>{fmt_value(item['pullback_near_ema'])}</td>"
            f"<td>{fmt_value(item['ema_reject'])}</td>"
            f"<td>{fmt_value(item['bearish_resume'])} / {fmt_value(item['bullish_resume'])}</td>"
            f"<td><b>{html.escape(item['verdict_entree'])}</b></td>"
            "</tr>"
        )

    confidence_label = report_confidence_label(overall_confidence)
    keep_items = [
        "Distinction TP / SL / BE / SL_PROFIT",
        "Matching existant temporel/side/entry",
        "Lecture MT5 et snapshots en analyse seule",
    ]
    watch_items = [
        "Résultat des trades après premier gain",
        "Entrées LIMITE/RISQUÉ avec distance EMA élevée ou signaux incomplets",
        "Écarts console CLOSE_DETECTED_* vs outcome final MT5",
    ]
    candidate_change = (
        "Tester en observation la règle 1 vrai TP = stop journée"
        if stop_gain["conclusion"] == "STOP_APRES_TP_AURAIT_AIDÉ"
        else "Aucune modification immédiate; accumuler plusieurs journées comparables"
    )

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
<li>Date génération: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</li>
<li>Nombre de trades: {metrics.get('total_trades', 0)}</li>
<li>Résultat net: {metrics.get('profit_net', 0.0):.2f}</li>
<li>TP / SL / BE / SL_PROFIT: {metrics.get('tp_count', 0)} / {metrics.get('sl_count', 0)} / {metrics.get('be_count', 0)} / {metrics.get('sl_profit_count', 0)}</li>
<li>Journée: <b>{day_status}</b></li>
<li>Conclusion rapide: {html.escape(day_conclusion)}</li>
</ul>

<h2>1bis) Résumé qualité de journée</h2>
<ul>
<li>Nombre de trades: {metrics.get('total_trades', 0)}</li>
<li>Résultat net: {metrics.get('profit_net', 0.0):.2f}</li>
<li>TP: {metrics.get('tp_count', 0)}</li>
<li>SL: {metrics.get('sl_count', 0)}</li>
<li>BE: {metrics.get('be_count', 0)}</li>
<li>SL_PROFIT: {metrics.get('sl_profit_count', 0)}</li>
<li>Profit moyen par trade: {fmt_value((metrics.get('profit_net', 0.0) / metrics.get('total_trades', 1)) if metrics.get('total_trades', 0) else None)}</li>
<li>Meilleure position: {fmt_value(metrics.get('best_trade'))}</li>
<li>Pire position: {fmt_value(metrics.get('worst_trade'))}</li>
<li>verdict_journée: <b>{day_quality_verdict}</b></li>
</ul>

<h2>1ter) Analyse stop après gain</h2>
<ul>
<li>premier_trade_result: {html.escape(str(stop_gain['premier_trade_result']))}</li>
<li>premier_trade_profit: {fmt_value(stop_gain['premier_trade_profit'])}</li>
<li>trades_après_premier_gain: {stop_gain['trades_apres_premier_gain']}</li>
<li>résultat_des_trades_après_premier_gain: {fmt_value(stop_gain['resultat_trades_apres_premier_gain'])}</li>
<li>profit_si_stop_après_premier_TP: {fmt_value(stop_gain['profit_si_stop_apres_premier_tp'])}</li>
<li>profit_si_stop_après_premier_trade_gagnant: {fmt_value(stop_gain['profit_si_stop_apres_premier_trade_gagnant'])}</li>
<li>différence_vs_réel: {fmt_value(stop_gain['difference_vs_reel'])}</li>
<li>différence_stop_gagnant_vs_réel: {fmt_value(stop_gain['difference_stop_gagnant_vs_reel'])}</li>
<li>conclusion automatique: <b>{html.escape(stop_gain['conclusion'])}</b></li>
</ul>
<p class="muted">Un trade gagnant inclut TP ou SL_PROFIT positif. Un vrai TP reste séparé de SL_PROFIT.</p>

<h2>1quater) Fiabilité du rapport</h2>
<ul>
<li>MT5 lu : {'OUI' if detection.get('mt5_read_ok') else 'NON'}</li>
<li>snapshots lus : {'OUI' if detection.get('snapshots_read_ok') else 'NON'}</li>
<li>matching ticket : {'OUI' if detection.get('matching_ticket') else 'NON'}</li>
<li>matching temporel + side + entry : {'OUI' if matching_context_count > 0 else 'NON'} ({matching_context_count}/{len(contexts)})</li>
<li>matching final : {'OUI' if matching_final_count > 0 else 'NON'} ({matching_final_count}/{len(contexts)})</li>
<li>confiance : {overall_confidence}</li>
<li>trades manuels détectés : {manual_unknown_excluded}</li>
<li>trades bot détectés : {bot_retained_trades}</li>
</ul>

<h2>1quinquies) Distinction des trades</h2>
<ul>
<li>Trades MT5 totaux: {mt5_total_trades}</li>
<li>Trades bot retenus: {bot_retained_trades}</li>
<li>Trades manuels / inconnus exclus: {manual_unknown_excluded}</li>
<li>Trades manuels / inconnus: {manual_unknown_excluded}</li>
<li>Trades ouverts: {len(detection.get('open_trades', []))}</li>
<li>Trades fermés: {len(detection.get('closed_trades', []))}</li>
<li>Snapshots order_ok non retrouvés dans MT5: {len(detection.get('unmatched_order_ok', []))}</li>
<li>Positions fermées UNKNOWN dans snapshots: {len(detection.get('unknown_closed_snapshots', []))}</li>
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
<p><b>Compteurs fermeture ajustés sur résultat final MT5:</b> {', '.join(f'{k}={v}' for k, v in final_close_counts.items())}</p>
<p class="muted">Ces compteurs ajustés évitent de compter une mention console CLOSE_DETECTED_SL comme vraie perte lorsque l'outcome final MT5 est SL_PROFIT.</p>
<p class="muted">Mentions techniques SL/TP ignorées pour éviter les faux compteurs.</p>
<p>Erreurs importantes: {len(console.get('errors', []))}</p>

<h2>4) Analyse snapshots</h2>
<ul>
<li>Normalisation temporelle: parsing ISO + conversion UTC si offset présent (sinon heure serveur conservée)</li>
<li>Offsets testés (trade MT5): {association_meta.get('tested_offsets_hours', [])} | offset retenu: {association_meta.get('best_offset_hours', 0)}h</li>
<li>Trades avec contexte snapshot exploitable (fenêtre -15/+2 min): {association_meta.get('matched_trades', 0)}/{metrics.get('total_trades', 0)}</li>
<li>Matching par ticket seulement, contexte incomplet: {association_meta.get('ticket_only_incomplete_trades', 0)}</li>
<li>Associations faibles: {association_meta.get('weak_trades', 0)}</li>
<li>Nombre de snapshots valides: {len(snaps.get('records', []))}</li>
<li>Lignes invalides: {len(snaps.get('invalid_lines', []))}</li>
<li>Champs disponibles: {', '.join(sorted(k for k in snaps.get('fields', {}).keys() if not k.startswith('_'))) or 'N/A'}</li>
<li>Spread moyen: {f"{snaps.get('spread_avg'):.3f}" if snaps.get('spread_avg') is not None else 'N/A'}</li>
<li>Distance M5 moyenne: {f"{snaps.get('m5_distance_avg'):.3f}" if snaps.get('m5_distance_avg') is not None else 'N/A'}</li>
<li>Décisions GPT: {dict(snaps.get('gpt_decisions', {}))}</li>
</ul>

<h2>5) Qualité d’entrée par trade</h2>
<table>
<tr><th>#</th><th>setup_type</th><th>route_name</th><th>side</th><th>décision GPT</th><th>status GPT</th><th>entry</th><th>sl</th><th>tp</th><th>distance EMA20 M5</th><th>distance EMA50 M5</th><th>atr5</th><th>atr15</th><th>market_dirty</th><th>market_too_tight</th><th>too_extended</th><th>pullback_near_ema</th><th>ema_reject</th><th>bearish_resume / bullish_resume</th><th>verdict entrée</th></tr>
{''.join(entry_quality_rows) if entry_quality_rows else '<tr><td colspan="20">Aucun trade à analyser.</td></tr>'}
</table>
<p class="muted">PROPRE = GPT_ALLOW/ORDER_OK + pas too_extended + pas market_dirty + signal pullback/reject/resume cohérent. LIMITE = GPT_ALLOW avec signaux incomplets. RISQUÉ = gros mouvement, same_side_reentry ou distance EMA élevée. INCONNU = données absentes.</p>

<h2>6) Analyse détaillée par trade</h2>
{''.join(context_blocks) if context_blocks else '<p>Aucun trade à analyser.</p>'}

<h2>7) Skips importants</h2>
<ul>{skip_items}</ul>
<ul>{''.join(f'<li>{html.escape(item)}</li>' for item in skip_interpretation)}</ul>

<h2>8) Anomalies</h2>
<ul>{anomalies_html}</ul>

<h2>9) Futures décisions</h2>
<ul>
<li><b>À ne pas modifier:</b> {html.escape('; '.join(keep_items))}</li>
<li><b>À surveiller:</b> {html.escape('; '.join(watch_items))}</li>
<li><b>Modification candidate:</b> {html.escape(candidate_change)}</li>
<li><b>Niveau de confiance du rapport:</b> {confidence_label} (matching final {matching_final_count}/{len(contexts)}, confiance brute {overall_confidence})</li>
<li><b>Priorité numéro 1:</b> {html.escape('Corriger la logique GPT timeout : jamais d’ALLOW automatique sur timeout' if any('DANGER : GPT timeout fallback ALLOW détecté' in a for a in anomalies) else (anomalies[0] if anomalies else 'Confirmer la stabilité sur plusieurs journées avant tout changement.'))}</li>
</ul>

<h2>10) Questions pour ChatGPT</h2>
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
<h2>11) Ce que le rapport peut conclure / ne peut pas conclure</h2>
<ul>
<li><b>Peut conclure:</b> qualité du matching temporel (avec offset), cohérence side/entry avec snapshot, et niveau de fiabilité (OUI/MOYEN/NON).</li>
<li><b>Peut conclure:</b> si le snapshot est exploitable pour orienter la revue (analyse graphique recommandée).</li>
<li><b>Ne peut pas conclure:</b> une qualification du marché (ex: "marché sale") sans données M5/M15 détaillées ou validation visuelle graphique.</li>
<li><b>Ne peut pas conclure:</b> la cause exacte d'un SL/TP sans analyse graphique complémentaire.</li>
</ul>
</body></html>"""
    return html_content


def build_resume(
    mt5: Dict[str, Any], console: Dict[str, Any], snaps: Dict[str, Any], anomalies: List[str], association_meta: Dict[str, Any]
) -> str:
    m = mt5.get("metrics", {})
    counts = console.get("event_counts", {})
    report_stats = mt5.get("report_stats", {}) or {}
    day_status, day_conclusion = evaluate_day(m, counts)
    detection = summarize_trade_detection(mt5, snaps)

    lines = [
        "=== RÉSUMÉ DÉBRIEF BTC (copier-coller ChatGPT) ===",
        f"Trades={m.get('total_trades', 0)} | Net={m.get('profit_net', 0.0):.2f} | TP/SL/BE/SL_PROFIT={m.get('tp_count', 0)}/{m.get('sl_count', 0)}/{m.get('be_count', 0)}/{m.get('sl_profit_count', 0)}",
        f"Détection bot/manuels: bot={len(detection.get('bot_trades', []))}, manuels={len(detection.get('manual_unknown_trades', []))}, ouverts={len(detection.get('open_trades', []))}, fermés={len(detection.get('closed_trades', []))}",
        f"Fiabilité: MT5 lu={'OUI' if detection.get('mt5_read_ok') else 'NON'}, snapshots lus={'OUI' if detection.get('snapshots_read_ok') else 'NON'}, matching ticket={'OUI' if detection.get('matching_ticket') else 'NON'}",
        f"Jour: {day_status} | Conclusion: {day_conclusion}",
        f"Console: GPT_TIMEOUT={counts.get('GPT_TIMEOUT', 0)}, M5_TOO_FAR={counts.get('M5_TOO_FAR', 0)}, OUT_OF_SESSION={counts.get('OUT_OF_SESSION', 0)}, ALLOW={counts.get('ALLOW', 0)}, BLOCK={counts.get('BLOCK', 0)}",
        f"Contrôle rapport MT5: Nb trades={report_stats.get('nb_trades', 'N/A')}, Profit Total Net={report_stats.get('profit_total_net', 'N/A')} (sans remplacer le parsing Positions)",
        f"Snapshots: valides={len(snaps.get('records', []))}, invalides={len(snaps.get('invalid_lines', []))}, spread_moyen={snaps.get('spread_avg')}, distance_m5_moyenne={snaps.get('m5_distance_avg')}",
        f"Association snapshots: offsets_testes={association_meta.get('tested_offsets_hours', [])}, offset_retenu={association_meta.get('best_offset_hours', 0)}h, trades_contexte_exploitable={association_meta.get('matched_trades', 0)}/{m.get('total_trades', 0)}, associations_faibles={association_meta.get('weak_trades', 0)}",
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
