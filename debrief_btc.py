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
from datetime import datetime, timedelta
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
    "TP",
    "SL",
    "BE",
    "REENTRY_TOO_CLOSE",
    "ENTRY_FILTER_SKIP",
    "GPT_BLOCK",
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
    text = text.replace("Z", "")
    for fmt in TIME_PATTERNS:
        try:
            return datetime.strptime(text, fmt)
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

    for i, line in enumerate(lines, start=1):
        line_upper = line.upper()
        ts = parse_datetime(line)
        for event in CONSOLE_EVENTS:
            if event in line_upper:
                event_counts[event] += 1
                matches.append({"line": i, "timestamp": ts, "event": event, "raw": line})
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

    trades: List[Trade] = []
    headers: List[str] = []

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
            col_comment = detect_column(headers, ["comment", "commentaire", "note"])

            for idx, row in enumerate(reader, start=1):
                trades.append(
                    Trade(
                        index=idx,
                        open_time=parse_datetime(row.get(col_open)) if col_open else None,
                        close_time=parse_datetime(row.get(col_close)) if col_close else None,
                        symbol=str(row.get(col_symbol, "")).strip() if col_symbol else "",
                        side=str(row.get(col_side, "")).strip().upper() if col_side else "",
                        lot=safe_float(row.get(col_lot)) if col_lot else None,
                        entry_price=safe_float(row.get(col_entry)) if col_entry else None,
                        exit_price=safe_float(row.get(col_exit)) if col_exit else None,
                        profit=safe_float(row.get(col_profit)) if col_profit else None,
                        commission=safe_float(row.get(col_comm)) if col_comm else None,
                        swap=safe_float(row.get(col_swap)) if col_swap else None,
                        comment=str(row.get(col_comment, "")).strip() if col_comment else "",
                    )
                )
    except OSError as exc:
        return {"warning": f"Impossible de lire {path.name}: {exc}", "trades": [], "headers": []}

    profits = [t.profit for t in trades if t.profit is not None]
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
        "warning": None,
        "headers": headers,
        "trades": trades,
        "metrics": {
            "total_trades": len(trades),
            "tp_count": len(positives),
            "sl_count": len(negatives),
            "be_count": len(zeros),
            "profit_net": sum(profits) if profits else 0.0,
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


def nearest_by_time(items: List[Any], target: datetime, max_minutes: int, time_attr: str) -> List[Any]:
    out = []
    max_delta = timedelta(minutes=max_minutes)
    for item in items:
        ts = getattr(item, time_attr, None) if not isinstance(item, dict) else item.get(time_attr)
        if ts and abs(ts - target) <= max_delta:
            out.append(item)
    return out


def associate_trade_context(mt5: Dict[str, Any], console: Dict[str, Any], snaps: Dict[str, Any], charts: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    trades: List[Trade] = mt5.get("trades", [])
    console_events = console.get("events", [])
    snapshots = snaps.get("records", [])
    chart_items: List[ChartImage] = charts.get("charts", [])

    for t in trades:
        anchor = t.open_time or t.close_time
        if not anchor:
            result.append({"trade": t, "snapshots": [], "console_events": [], "charts": [], "comment": "contexte incomplet"})
            continue

        near_snapshots = [s for s in snapshots if s.get("_timestamp") and abs(s["_timestamp"] - anchor) <= timedelta(minutes=20)]
        near_console = [e for e in console_events if e.get("timestamp") and abs(e["timestamp"] - anchor) <= timedelta(minutes=20)]
        near_charts = [c for c in chart_items if c.timestamp and abs(c.timestamp - anchor) <= timedelta(minutes=20)]

        comment = "entrée propre"
        if not near_snapshots:
            comment = "contexte incomplet"
        elif any("M5_TOO_FAR" in str(e.get("event", "")) for e in near_console):
            comment = "entrée tardive possible"
        elif any("OUT_OF_SESSION" in str(e.get("event", "")) for e in near_console):
            comment = "marché sale possible"
        elif len(near_snapshots) < 2:
            comment = "entrée sans respiration possible"

        result.append(
            {
                "trade": t,
                "snapshots": near_snapshots[:5],
                "console_events": near_console[:8],
                "charts": near_charts,
                "comment": comment,
            }
        )
    return result


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
    anomalies = []
    counts = console.get("event_counts", {})
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

    return anomalies


def fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "N/A"


def build_html(base_dir: Path, out_dir: Path, console: Dict[str, Any], snaps: Dict[str, Any], mt5: Dict[str, Any], charts: Dict[str, Any], contexts: List[Dict[str, Any]], anomalies: List[str]) -> str:
    metrics = mt5.get("metrics", {})
    counts = console.get("event_counts", {})
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
        images = " ".join(
            f'<a href="../{html.escape(rel(c.path))}">{html.escape(c.path.name)}</a>' for c in ctx["charts"]
        ) or "Aucune"
        evs = ", ".join(sorted({e["event"] for e in ctx["console_events"]})) or "Aucun"
        context_blocks.append(
            "<div class='card'>"
            f"<h4>Trade #{t.index} — {html.escape(t.side)} — profit={t.profit}</h4>"
            f"<p><b>Heure:</b> {fmt_dt(t.open_time)} | <b>Résultat:</b> {classify_trade_result(t)}</p>"
            f"<p><b>Snapshots proches:</b> {len(ctx['snapshots'])} | <b>Événements console:</b> {html.escape(evs)}</p>"
            f"<p><b>Charts liées:</b> {images}</p>"
            f"<p><b>Commentaire automatique:</b> {html.escape(ctx['comment'])}</p>"
            "</div>"
        )

    top_skips = snaps.get("skip_reasons", Counter()).most_common(8)
    skip_items = "".join(f"<li>{html.escape(k)}: {v}</li>" for k, v in top_skips) or "<li>Aucune</li>"

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
<table>
<tr><th>#</th><th>Ouverture</th><th>Fermeture</th><th>Symbole</th><th>Sens</th><th>Lot</th><th>Entrée</th><th>Sortie</th><th>Profit</th><th>Durée</th></tr>
{''.join(trade_rows) if trade_rows else '<tr><td colspan="10">Aucun trade</td></tr>'}
</table>

<h2>3) Analyse console</h2>
<ul>
{''.join(f'<li>{k}: {v}</li>' for k, v in sorted(counts.items())) or '<li>Aucun événement détecté</li>'}
</ul>
<p>Erreurs importantes: {len(console.get('errors', []))}</p>

<h2>4) Analyse snapshots</h2>
<ul>
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
<p class="muted">Interprétation: skip probablement justifié / à vérifier / trop strict selon fréquence et contexte trade.</p>

<h2>7) Anomalies</h2>
<ul>{anomalies_html}</ul>

<h2>8) Aide à la décision</h2>
<ul>
<li><b>À changer maintenant:</b> {'rien à changer' if metrics.get('profit_net', 0) > 0 and len(anomalies) == 0 else 'vérifier filtres dominants et anomalies clés'}</li>
<li><b>À observer encore:</b> qualité des entrées vs M5_TOO_FAR, timeouts GPT, contexte session.</li>
<li><b>À ne pas toucher:</b> lot size, horaires de session, SL/TP, trade cap, logique qui semble fonctionner.</li>
<li><b>Priorité numéro 1:</b> {html.escape(anomalies[0] if anomalies else 'Confirmer la stabilité sur plusieurs journées avant tout changement.')}</li>
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


def build_resume(mt5: Dict[str, Any], console: Dict[str, Any], snaps: Dict[str, Any], anomalies: List[str]) -> str:
    m = mt5.get("metrics", {})
    counts = console.get("event_counts", {})
    day_status, day_conclusion = evaluate_day(m, counts)

    lines = [
        "=== RÉSUMÉ DÉBRIEF BTC (copier-coller ChatGPT) ===",
        f"Trades={m.get('total_trades', 0)} | Net={m.get('profit_net', 0.0):.2f} | TP/SL/BE={m.get('tp_count', 0)}/{m.get('sl_count', 0)}/{m.get('be_count', 0)}",
        f"Jour: {day_status} | Conclusion: {day_conclusion}",
        f"Console: GPT_TIMEOUT={counts.get('GPT_TIMEOUT', 0)}, M5_TOO_FAR={counts.get('M5_TOO_FAR', 0)}, OUT_OF_SESSION={counts.get('OUT_OF_SESSION', 0)}, ALLOW={counts.get('ALLOW', 0)}, BLOCK={counts.get('BLOCK', 0)}",
        f"Snapshots: valides={len(snaps.get('records', []))}, invalides={len(snaps.get('invalid_lines', []))}, spread_moyen={snaps.get('spread_avg')}, distance_m5_moyenne={snaps.get('m5_distance_avg')}",
        "Top skip reasons: " + ", ".join(f"{k}={v}" for k, v in snaps.get("skip_reasons", Counter()).most_common(5)) if snaps.get("skip_reasons") else "Top skip reasons: N/A",
        "Anomalies: " + (" | ".join(anomalies[:8]) if anomalies else "aucune"),
        "Priorité #1: " + (anomalies[0] if anomalies else "Confirmer la stabilité multi-jours avant tout changement."),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    base_dir = Path.cwd()
    out_dir = ensure_output_dir(base_dir)

    console = parse_console(base_dir / "console.txt")
    snaps = parse_snapshots(base_dir / "snapshots_btc.jsonl")
    mt5 = parse_mt5(base_dir / "mt5_history.csv")
    charts = parse_charts(base_dir / "charts")

    contexts = associate_trade_context(mt5, console, snaps, charts)
    anomalies = detect_anomalies(console, snaps, mt5, contexts)

    for src in (console, snaps, mt5, charts):
        if src.get("warning"):
            anomalies.append(src["warning"])

    html_content = build_html(base_dir, out_dir, console, snaps, mt5, charts, contexts, anomalies)
    resume = build_resume(mt5, console, snaps, anomalies)

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
