"""Freeze three factor comparisons before reading a bounded market snapshot."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard.joint_study import VERSION, VARIANTS, RULES, evaluate, summarize, matched
from dashboard.management.commands.study_pressure import database
from dashboard.management.commands.study_pattern_flow import write_immutable


def hashes():
    root = Path(__file__).resolve().parents[2]
    names = ("joint_study.py", "pressure_study.py", "stream_inputs.py", "microstructure_archive.py",
             "continuous_watch.py", "management/commands/study_joint.py",
             "management/commands/study_pressure.py", "management/commands/study_pattern_flow.py")
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}


def freeze(directory, session):
    path = directory/"joint-stream-protocol-v1.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (saved.get("source_sha256") != hashes() or saved.get("session") != session
                or saved.get("version") != VERSION or saved.get("rules") != RULES
                or saved.get("variants") != list(VARIANTS) or saved.get("entry_allowed") is not False):
            raise CommandError("Frozen source/protocol changed. Register a new version; no prices read.")
        return path, saved
    now = datetime.now(timezone.utc)
    saved = {"version": VERSION, "at": now.isoformat(), "session": session,
             "research_cutoff": now.isoformat(), "forward_start": (now+timedelta(minutes=5)).isoformat(),
             "forward_end": (now+timedelta(days=7, minutes=5)).isoformat(),
             "rules": RULES, "variants": list(VARIANTS), "tickers": ["SBER", "GAZP", "LKOH"],
             "source_sha256": hashes(), "entry_allowed": False,
             "forward_policy": "fixed rules; all qualifying closed segments in watch sessions inside the registered future window; no source/window/ticker optimisation"}
    directory.mkdir(parents=True, exist_ok=True)
    write_immutable(path, saved)
    return path, saved


def snapshot(session, saved, phase):
    cutoff = datetime.fromisoformat(saved["research_cutoff"])
    start = datetime.fromisoformat(saved["forward_start"]) if phase == "forward" else None
    end = datetime.fromisoformat(saved["forward_end"]) if phase == "forward" else cutoff
    now = datetime.now(timezone.utc)
    segments = []
    paths = sorted(session.parent.glob("*/*-book.sqlite3")) if phase == "forward" else sorted(session.glob("*-book.sqlite3"))
    if len(paths) > 1000:
        raise CommandError("Archive inventory too large for bounded inspection")
    for path in paths:
        chart = path.with_name(path.name.replace("-book.sqlite3", "-chart.sqlite3"))
        permitted = path.resolve().parent.parent == session.parent if phase == "forward" else path.resolve().parent == session
        if not permitted or chart.resolve().parent != path.resolve().parent:
            raise CommandError("Archive escapes session")
        runs = database(path, "SELECT started_at,finished_at FROM collection_runs")
        if not runs or any(not r["finished_at"] for r in runs):
            continue
        if any(datetime.fromisoformat(r["finished_at"]) > min(end, now) for r in runs):
            continue
        if start and any(datetime.fromisoformat(r["started_at"]) < start for r in runs):
            continue
        # Closed segments are immutable; read from original paths, not live DBs.
        if len(segments) >= RULES["max_segments"]:
            raise CommandError("Too many eligible segments: refusing to truncate/select a convenient subset")
        segments.append({"name": path.parent.name+"/"+path.name, "book_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "chart_sha256": hashlib.sha256(chart.read_bytes()).hexdigest(),
                         "rows": database(path, "SELECT * FROM market_events ORDER BY id"),
                         "extras": database(chart, "SELECT * FROM events ORDER BY id")})
    return {"version": VERSION, "phase": phase, "segments": segments,
            "window_start": start.isoformat() if start else None, "window_end": end.isoformat()}


class Command(BaseCommand):
    help = "Three fixed chart/tape/book LONG comparisons on observed streams; no orders."

    def add_arguments(self, parser):
        parser.add_argument("--session", required=True)
        parser.add_argument("--phase", choices=("replay", "forward"), default="replay")

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/"data"
        root = (directory/"watch").resolve()
        session = (root/options["session"]).resolve()
        if session.parent != root or not session.is_dir():
            raise CommandError("Specify one existing watch session")
        protocol, saved = freeze(directory, session.name)
        signature = hashlib.sha256(protocol.read_bytes()).hexdigest()
        phase = options["phase"]
        input_path = directory/f"joint-stream-input-v1-{phase}.json"
        if phase == "replay" and input_path.exists():
            if input_path.stat().st_size > RULES["max_input_bytes"]:
                raise CommandError("Input too large")
            inputs = json.loads(input_path.read_text(encoding="utf-8"))
            if inputs.get("protocol_sha256") != signature:
                raise CommandError("Input/protocol mismatch")
        else:
            inputs = snapshot(session, saved, phase) | {"protocol_sha256": signature}
            size = len(json.dumps(inputs, ensure_ascii=False).encode("utf-8"))
            if size > RULES["max_input_bytes"]:
                raise CommandError("Bounded snapshot too large")
            if phase == "forward":
                input_path = directory/f"joint-stream-input-v1-forward-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}.json"
            write_immutable(input_path, inputs)
        rows, extras, coverage = [], [], {}
        for seg in inputs["segments"]:
            for name, target in (("rows", rows), ("extras", extras)):
                target.extend(r | {"segment": seg["name"]} for r in seg[name])
        known = set(saved["tickers"])
        if any(r["ticker"] not in known for r in rows+extras):
            raise CommandError("Unregistered ticker")
        outcomes, candidates = {k: [] for k in VARIANTS}, []
        for ticker in saved["tickers"]:
            identities = {(r["instrument_uid"], json.loads(r["payload"]).get("lot_size")) for r in rows if r["ticker"] == ticker}
            if len(identities) > 1 or any(type(lot) is not int or lot < 1 for uid, lot in identities):
                raise CommandError("Instrument/lot identity changed")
            result = evaluate(rows, extras, ticker)
            for key in VARIANTS:
                outcomes[key].extend(result["outcomes"][key])
            candidates.extend(result["candidates"])
            coverage[ticker] = {"books": sum(r["ticker"] == ticker and r["kind"] == "book" for r in rows),
                                "trades": sum(r["ticker"] == ticker and r["kind"] == "trade" for r in rows),
                                "candle_messages": sum(r["ticker"] == ticker and r["kind"] == "candle" for r in extras),
                                "diagnostics": result["diagnostics"]}
        at = datetime.now(timezone.utc)
        report = {"version": VERSION, "at": at.isoformat(), "session": session.name, "phase": phase,
                  "protocol_sha256": signature, "source_sha256": hashes(),
                  "input_file": input_path.name, "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                  "segments": len(inputs["segments"]), "tickers": saved["tickers"], "coverage": coverage,
                  "outcomes": outcomes, "candidates": candidates,
                  "summaries": {k: summarize(v) for k, v in outcomes.items()},
                  "matched": {k: matched(candidates, k) for k in VARIANTS[1:]},
                  "forward_complete": phase == "forward" and at >= datetime.fromisoformat(saved["forward_end"]),
                  "entry_allowed": False}
        path = directory/f"joint-stream-study-{phase}-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        write_immutable(path, report)
        for k, metrics in report["summaries"].items():
            self.stdout.write(k+": "+json.dumps(metrics, ensure_ascii=True))
        self.stdout.write(f"Candidates: {len(candidates)}; segments: {report['segments']}")
        self.stdout.write(str(path))
