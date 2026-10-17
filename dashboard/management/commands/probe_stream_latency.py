import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard.stream_latency_probe import VERSION, ReceiverProbe, compare
from dashboard.microstructure_archive import StreamControl, bounded_client, enum_name, validate_options
from dashboard.continuous_watch import requests, ACK_FIELDS
from dashboard.market_provider import resolve, error_message
from dashboard.management.commands.collect_microstructure import subscription_statuses
from dashboard.management.commands.study_pattern_flow import write_immutable
from dashboard.stream_inputs import read_rows


class Command(BaseCommand):
    help = "Read-only 10–120s concurrent receiver-only latency probe; no orders, no archive replacement."

    def add_arguments(self, parser):
        parser.add_argument('--seconds', type=int, default=60)
        parser.add_argument('--tickers', nargs='+', default=['SBER', 'GAZP', 'LKOH'])
        parser.add_argument('--identity-session', help='Reuse public instrument UID/lot from a known local watch session; no instrument-search RPC.')

    def handle(self, *args, **options):
        tickers, seconds = validate_options(options['tickers'], options['seconds'])
        if not 10 <= seconds <= 120:
            raise CommandError("Probe duration must be 10–120s")
        token = os.getenv('INVEST_TOKEN', '').strip()
        if not token:
            raise CommandError("INVEST_TOKEN needed for market data only")
        root = Path(settings.BASE_DIR)/'data'
        root.mkdir(parents=True, exist_ok=True)
        identity_session = None
        if options.get('identity_session'):
            watch_root = (root/'watch').resolve()
            identity_session = (watch_root/options['identity_session']).resolve()
            if identity_session.parent != watch_root or not identity_session.is_dir():
                raise CommandError('Identity session must be one existing watch directory')
        at = datetime.now(timezone.utc)
        protocol = root/f"receiver-latency-protocol-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        module = Path(__file__).resolve().parents[2]/'stream_latency_probe.py'
        version = 'receiver_latency_probe_v2_cached_identity' if identity_session else VERSION
        write_immutable(protocol, {"version": version, "at": at.isoformat(), "seconds": seconds, "tickers": tickers,
                                  "identity_session": identity_session.name if identity_session else None,
                                  "source_sha256": {"module": hashlib.sha256(module.read_bytes()).hexdigest(),
                                                    "command": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
                                  "market_only": True, "entry_allowed": False, "writes_during_receiving": False})
        control = StreamControl(seconds)
        timer = threading.Timer(seconds, control.cancel)
        timer.daemon = True
        logger = logging.getLogger('t_tech.invest')
        old_level = logger.level
        logger.setLevel(logging.CRITICAL)
        status, safe_error, probe, stream = 'stream_ended', None, None, None
        self.stdout.write(f"Receiver-only probe: {seconds}s; no orders, no archive changes.")
        timer.start()
        try:
            with bounded_client(token, control) as client:
                if identity_session:
                    instruments = cached_identities(identity_session, tickers)
                else:
                    instruments = [resolve(client, ticker) for ticker in tickers]
                probe = ReceiverProbe(instruments)
                stream = client.market_data_stream.market_data_stream(requests(instruments, control))
                for message in stream:
                    received = datetime.now(timezone.utc)
                    if control.stop.is_set() or control.remaining <= 0:
                        status = 'deadline'
                        break
                    statuses = list(subscription_statuses(message))
                    for field, entries in ACK_FIELDS:
                        statuses.extend(enum_name(s.subscription_status) for s in getattr(getattr(message, field, None), entries, []) or [])
                    if any(s != 'SUBSCRIPTION_STATUS_SUCCESS' for s in statuses):
                        status = 'subscription_rejected'
                        break
                    if not probe.observe(message, received):
                        status = 'record_limit'
                        break
        except KeyboardInterrupt:
            status = 'interrupted'
        except Exception as exc:
            if control.remaining <= 0 or control.stop.is_set():
                status = 'deadline'
            else:
                status, safe_error = 'error', error_message(exc)
        finally:
            control.cancel()
            timer.cancel()
            logger.setLevel(old_level)
        end = datetime.now(timezone.utc)
        comparison_rows, comparison_error = [], None
        try:
            # Committed diagnostic reads are allowed here, including active DBs.
            # This is not strategy replay or evidence of a continuous price path.
            paths = sorted((root/'watch').glob('*/*-book.sqlite3'), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:4]
            for path in paths:
                if not path.resolve().is_relative_to((root/'watch').resolve()):
                    raise ValueError("Archive path escapes root")
                comparison_rows.extend(read_rows(path, 'market_events',
                    "SELECT * FROM market_events WHERE kind='book' AND exchange_at>=? AND received_at<=? ORDER BY id LIMIT 20001",
                    ((at-timedelta(seconds=1)).isoformat(), end.isoformat())))
        except Exception:
            comparison_error = "Concurrent archive could not be read; no comparison fabricated."
        observations = probe.observations if probe else []
        report = {"version": version, "started_at": at.isoformat(), "ended_at": end.isoformat(),
                  "elapsed_seconds": (end-at).total_seconds(), "status": status, "message": safe_error,
                  "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
                  "summary": probe.summary() if probe else {}, "observations": observations,
                  "paired": compare(observations, comparison_rows), "comparison_error": comparison_error,
                  "entry_allowed": False}
        path = root/f"receiver-latency-probe-{end.strftime('%Y%m%d-%H%M%S-%f')}.json"
        write_immutable(path, report)
        self.stdout.write(json.dumps({"status": status, "elapsed_seconds": report['elapsed_seconds'],
                                      "summary": report['summary'], "paired": {k:v for k,v in report['paired'].items() if k != 'pairs'},
                                      "error": safe_error, "comparison_error": comparison_error}, ensure_ascii=True))
        self.stdout.write(str(path))


def cached_identities(session, tickers):
    paths = sorted(session.glob('*-book.sqlite3'), reverse=True)[:4]
    instruments = []
    for ticker in tickers:
        identities = set()
        for path in paths:
            if path.resolve().parent != session:
                raise ValueError('Identity file escapes session')
            records = read_rows(path, 'market_events',
                "SELECT instrument_uid,payload FROM market_events WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,))
            identities.update((r['instrument_uid'], json.loads(r['payload'])['lot_size']) for r in records)
        if len(identities) != 1:
            raise ValueError('Missing or changed cached instrument identity')
        uid, lot = next(iter(identities))
        if not isinstance(uid,str) or not uid or type(lot) is not int or lot < 1:
            raise ValueError('Invalid cached identity')
        instruments.append({'uid':uid,'lot':lot,'ticker':ticker})
    return instruments
