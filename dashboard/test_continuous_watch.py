import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from .continuous_watch import (ExtraArchive, Journal, RESERVE_BYTES, exclusive_watch,
                               load_health, options, requests, supervise)
from .microstructure_archive import StreamControl
from .test_microstructure_archive import NOW, INSTRUMENT, book, trade


def candle(**changes):
    return NS(**({"instrument_uid": INSTRUMENT["uid"], "interval": "SUBSCRIPTION_INTERVAL_ONE_MINUTE",
                 "time": NOW-timedelta(minutes=1), "open": 100, "high": 101, "low": 99, "close": 100.5,
                 "volume": 10, "volume_buy": 6, "volume_sell": 4,
                 "candle_source_type": "CANDLE_SOURCE_EXCHANGE"} | changes))


class ContinuousWatchTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"watch"

    def test_limits_and_no_short_symbol_input(self):
        self.assertEqual(options(["sber", "SBER"], 86400, 300, RESERVE_BYTES), ["SBER"])
        for args in ((["SBER"], 86401, 300, RESERVE_BYTES), (["SBER"], True, 300, RESERVE_BYTES),
                     (["SBER"], 10, 4, RESERVE_BYTES), (["SBER"], 10, 5, RESERVE_BYTES-1),
                     (["SBER;cmd"], 10, 5, RESERVE_BYTES)):
            with self.assertRaises(ValueError):
                options(*args)

    def test_exclusive_lock_and_cleanup_preserve_other_files(self):
        self.root.mkdir()
        other = self.root/"old.sqlite3"
        other.write_bytes(b"old")
        with exclusive_watch(self.root):
            with self.assertRaises(ValueError):
                with exclusive_watch(self.root):
                    self.fail("Second collector started")
        self.assertFalse((self.root/"active.lock").exists())
        self.assertEqual(other.read_bytes(), b"old")

    def test_subscription_contains_only_readonly_exchange_closed_data(self):
        control = StreamControl(5)
        generator = requests([INSTRUMENT], control)
        first = [next(generator) for _ in range(4)]
        self.assertEqual(len(first[0].subscribe_order_book_request.instruments), 1)
        self.assertEqual(len(first[1].subscribe_trades_request.instruments), 1)
        candles = first[2].subscribe_candles_request
        self.assertTrue(candles.waiting_close)
        self.assertEqual(candles.candle_source_type.name, "CANDLE_SOURCE_EXCHANGE")
        self.assertEqual(len(candles.instruments), 2)
        self.assertEqual(len(first[3].subscribe_info_request.instruments), 1)
        control.cancel()
        self.assertEqual(list(generator), [])

    def test_only_confirmed_closed_exchange_candle_is_eligible(self):
        archive = ExtraArchive(Path(self.temp.name)/"chart.sqlite3")
        self.addCleanup(archive.close)
        acked = {(INSTRUMENT["uid"], "SUBSCRIPTION_INTERVAL_ONE_MINUTE")}
        for item, ack in ((candle(), acked), (candle(), set()), (candle(time=NOW), acked),
                          (candle(low=102), acked), (candle(volume=True), acked),
                          (candle(candle_source_type="CANDLE_SOURCE_DEALER"), acked)):
            archive.append("candle", item, INSTRUMENT, NOW, ack)
        rows = archive.db.execute("SELECT causal_eligible FROM events ORDER BY id").fetchall()
        self.assertEqual(rows, [(1,), (0,), (0,), (0,), (0,), (0,)])

    def test_health_never_authorizes_entry_and_detects_dead_process(self):
        self.assertEqual(load_health(self.root)["status"], "not_started")
        session = self.root/"20261006-test"
        session.mkdir(parents=True)
        journal = Journal(session/"health.sqlite3", ["SBER"], 60)
        journal.change(status="collecting")
        saved = json.loads(journal.db.execute("SELECT payload FROM health").fetchone()[0])
        from datetime import datetime
        clock = datetime.fromisoformat(saved["heartbeat_at"])
        self.assertTrue(load_health(self.root, clock)["alive"])
        self.assertFalse(load_health(self.root, clock)["entry_allowed"])
        self.assertEqual(load_health(self.root, clock+timedelta(seconds=21))["status"], "stale")
        journal.change(status="deadline")
        self.assertFalse(load_health(self.root)["alive"])
        journal.close()

    def test_reconnects_have_distinct_files_and_explicit_boundaries(self):
        @contextmanager
        def client(token, control):
            yield NS(market_data_stream=NS(market_data_stream=lambda _: iter([
                NS(orderbook=book(), trade=trade())])))

        with patch("dashboard.continuous_watch.bounded_client", client), \
                patch("dashboard.continuous_watch.resolve", return_value=INSTRUMENT), \
                patch("dashboard.continuous_watch.time.sleep"):
            session, status = supervise(self.root, "fixture", ["SBER"], 60, 5)
        self.assertEqual(status, "retries_exhausted")
        self.assertEqual(len(list(session.glob("*-book.sqlite3"))), 8)
        with closing(sqlite3.connect(session/"health.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 8)
        self.assertTrue(load_health(self.root)["gaps_possible"])
        self.assertFalse((self.root/"active.lock").exists())

    def test_quota_stops_without_opening_broker_and_preserves_files(self):
        with patch("dashboard.continuous_watch.disk_usage", return_value=1_000_000_000), \
                patch("dashboard.continuous_watch.bounded_client") as client:
            _, status = supervise(self.root, "fixture", ["SBER"], 5, 5)
        self.assertEqual(status, "disk_limit")
        client.assert_not_called()

    def test_interruption_closes_session_and_lock(self):
        def interrupted(_):
            raise KeyboardInterrupt

        @contextmanager
        def client(token, control):
            yield NS(market_data_stream=NS(market_data_stream=interrupted))

        with patch("dashboard.continuous_watch.bounded_client", client), \
                patch("dashboard.continuous_watch.resolve", return_value=INSTRUMENT):
            _, status = supervise(self.root, "fixture", ["SBER"], 5, 5)
        self.assertEqual(status, "interrupted")
        self.assertFalse((self.root/"active.lock").exists())
