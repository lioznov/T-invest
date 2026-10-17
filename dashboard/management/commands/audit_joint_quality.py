"""Read-only diagnostics of the exact frozen joint input, not a new backtest."""
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard.joint_study import RULES
from dashboard.microstructure_archive import utc_timestamp
from dashboard.management.commands.study_pattern_flow import write_immutable


class Command(BaseCommand):
    help = "Explain flags/latencies in the frozen joint snapshot; no corrections or orders."

    def handle(self, *args, **options):
        path = Path(settings.BASE_DIR)/"data"/"joint-stream-input-v1-replay.json"
        if path.stat().st_size > RULES["max_input_bytes"]:
            raise CommandError("Input too large")
        raw = path.read_bytes()
        saved = json.loads(raw)
        flags, lag_bins = {}, {}
        gaps, durations, future_lags = [], [], []
        for segment in saved["segments"]:
            first, last, previous = None, None, {}
            for r in segment["rows"]:
                key = r["kind"]
                flags.setdefault(key, Counter()).update(json.loads(r["quality_flags"]))
                received = utc_timestamp(r["received_at"])
                first = min(first, received) if first else received
                last = max(last, received) if last else received
                if r["exchange_at"]:
                    lag = (received-utc_timestamp(r["exchange_at"])).total_seconds()
                    bucket = "future" if lag < 0 else "0_to_1s" if lag <= 1 else "1_to_5s" if lag <= 5 else "over_5s"
                    lag_bins.setdefault(key, Counter())[bucket] += 1
                    if lag < 0:
                        future_lags.append(lag)
                if key == "book":
                    if r["ticker"] in previous:
                        gap = (received-previous[r["ticker"]]).total_seconds()
                        if gap > 5:
                            gaps.append(gap)
                    previous[r["ticker"]] = received
            if first and last:
                durations.append((last-first).total_seconds())
        audit = {"at": datetime.now(timezone.utc).isoformat(), "input_sha256": hashlib.sha256(raw).hexdigest(),
                 "flags": {k: dict(v) for k, v in flags.items()},
                 "received_minus_exchange_seconds": {k: dict(v) for k, v in lag_bins.items()},
                 "book_gaps_over_5s": len(gaps), "max_book_gap_seconds": max(gaps, default=None),
                 "segment_seconds_min": min(durations, default=None), "segment_seconds_max": max(durations, default=None),
                 "future_clock_seconds_min": min(future_lags, default=None),
                 "future_clock_seconds_max": max(future_lags, default=None),
                 "note": "Negative latency cannot identify local-clock versus source-clock error. No timestamps/gaps were corrected."}
        report = path.parent/f"joint-quality-audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}.json"
        write_immutable(report, audit)
        self.stdout.write(json.dumps(audit, ensure_ascii=True, indent=2))
        self.stdout.write(str(report))
