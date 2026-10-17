"""Freeze before reading prices; snapshot completed segments, then replay."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.pressure_study import VERSION, VARIANTS, RULES, evaluate_segment, summary
from dashboard.management.commands.study_pattern_flow import write_immutable


def hashes():
    modules = Path(__file__).resolve().parents[2]
    return {name: hashlib.sha256((modules/name).read_bytes()).hexdigest() for name in
            ("pressure_study.py", "stream_inputs.py", "microstructure_archive.py", "continuous_watch.py",
             "management/commands/study_pressure.py")}


def freeze(directory, session, cutoff):
    spec = {"version": VERSION, "variants": VARIANTS, "rules": RULES,
            "session": session, "cutoff": cutoff.isoformat(), "entry_allowed": False}
    path = directory/"pressure-stream-protocol-v1.json"
    signature = {"spec": spec, "source_sha256": hashes()}
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("source_sha256") != hashes() or old.get("spec", {}).get("session") != session:
            raise CommandError("Frozen stream protocol changed; no prices read. Register a new version.")
        return path, old
    write_immutable(path, signature)
    return path, signature


def database(path, query):
    if not path.is_file() or path.stat().st_size > 25_000_000:
        raise CommandError("Invalid bounded stream archive")
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(query)]


class Command(BaseCommand):
    help = "Four fixed LONG hypotheses on closed observed stream segments; no broker calls/orders."

    def add_arguments(self, parser):
        parser.add_argument('--session', required=True, help='Existing data/watch session basename')

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/"data"
        root = (directory/"watch").resolve()
        session = (root/options['session']).resolve()
        if session.parent != root or not session.is_dir():
            raise CommandError("Specify one existing session, not another path")
        protocol, spec = freeze(directory, session.name, datetime.now(timezone.utc))
        input_path = directory/"pressure-stream-input-v1.json"
        if input_path.exists():
            saved = json.loads(input_path.read_text(encoding="utf-8"))
            if saved.get("protocol_sha256") != hashlib.sha256(protocol.read_bytes()).hexdigest():
                raise CommandError("Frozen input/protocol mismatch")
        else:
            segments = []
            for path in sorted(session.glob("*-book.sqlite3"))[:RULES["max_segments"]]:
                runs = database(path, "SELECT finished_at FROM collection_runs")
                if not runs or any(not r['finished_at'] or r['finished_at'] > spec['spec']['cutoff'] for r in runs):
                    continue  # Never snapshot a live/unfinished segment.
                chart = path.with_name(path.name.replace('-book.sqlite3', '-chart.sqlite3'))
                if not path.resolve().is_relative_to(session) or not chart.resolve().is_relative_to(session):
                    raise CommandError("Segment escapes session")
                segments.append({"name": path.name, "book_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                 "chart_sha256": hashlib.sha256(chart.read_bytes()).hexdigest(),
                                 "rows": database(path, "SELECT * FROM market_events ORDER BY id"),
                                 "infos": database(chart, "SELECT * FROM events WHERE kind='info' ORDER BY id")})
            saved = {"version": VERSION, "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(), "segments": segments}
            write_immutable(input_path, saved)
        outcomes = {key: [] for key in VARIANTS}
        for segment in saved["segments"]:
            for ticker in sorted({r["ticker"] for r in segment["rows"]}):
                identities = {(r["instrument_uid"], json.loads(r["payload"]).get("lot_size")) for r in segment["rows"] if r["ticker"] == ticker}
                if len(identities) != 1:
                    raise CommandError("Instrument/lot identity changed")
                result = evaluate_segment(segment["rows"], segment["infos"], ticker, segment["name"])
                for key in VARIANTS:
                    outcomes[key].extend(result[key])
        summaries = {key: summary(values) for key, values in outcomes.items()}
        at = datetime.now(timezone.utc)
        report = directory/f"pressure-stream-study-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        write_immutable(report, {"version": VERSION, "at": at.isoformat(), "session": session.name,
                               "protocol_sha256": saved["protocol_sha256"], "source_sha256": hashes(),
                               "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                               "segments": len(saved['segments']), "summaries": summaries, "outcomes": outcomes,
                               "entry_allowed": False})
        for key, values in summaries.items():
            self.stdout.write(key+": "+json.dumps(values, ensure_ascii=True))
        self.stdout.write(str(report))
