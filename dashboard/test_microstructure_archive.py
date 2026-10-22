"""Synthetic stream/archive fixtures, not evidence of market performance."""
import io
import json
import sqlite3
import threading
import time
from collections import namedtuple
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError

from .management.commands.collect_microstructure import consume_stream, subscription_statuses
from .microstructure_archive import (ArchiveLimit, MicrostructureArchive, StreamControl,
                                    subscription_requests, validate_options)


NOW = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
INSTRUMENT = {"ticker": "SBER", "uid": "fixture-uid", "lot": 10}


def book(at=NOW, **kwargs):
    defaults = {"time": at, "instrument_uid": INSTRUMENT["uid"], "depth": 20,
                "is_consistent": True, "order_book_type": "ORDERBOOK_TYPE_EXCHANGE",
                "bids": [NS(price=100-i*.01, quantity=5+i) for i in range(30)],
                "asks": [NS(price=100.05+i*.01, quantity=7+i) for i in range(30)]}
    return NS(**(defaults | kwargs))


def trade(at=NOW, **kwargs):
    defaults = {"time": at, "instrument_uid": INSTRUMENT["uid"], "price": 100,
                "quantity": 2, "direction": "TRADE_DIRECTION_BUY",
                "trade_source": "TRADE_SOURCE_EXCHANGE"}
    return NS(**(defaults | kwargs))


class MicrostructureArchiveTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "microstructure.sqlite3"

    def start(self, **kwargs):
        archive = MicrostructureArchive(self.path, **kwargs)
        archive.begin_run(["SBER"], 30, NOW)
        return archive

    def rows(self, archive):
        return archive.db.execute("SELECT received_at,exchange_at,received_sequence,exchange_sequence,is_consistent,causal_eligible,quality_flags,payload FROM market_events ORDER BY id").fetchall()

    def test_times_sequences_consistency_top20_and_lot_units_are_separate(self):
        with self.start() as archive:
            self.assertEqual(archive.append("book", book(sequence=42), INSTRUMENT, NOW+timedelta(seconds=.5)), "stored")
            row = self.rows(archive)[0]
            self.assertNotEqual(row[0], row[1])
            self.assertEqual(row[2:6], (1, "42", 1, 1))
            payload = json.loads(row[7])
            self.assertEqual(len(payload["bids"]), 20)
            self.assertEqual(len(payload["asks"]), 20)
            self.assertEqual(payload["quantity_unit"], "lots")
            self.assertEqual(payload["lot_size"], 10)
            self.assertEqual(payload["bids"][0], [100, 5])

    def test_book_sampling_is_at_most_once_per_second_per_instrument(self):
        with self.start() as archive:
            self.assertEqual(archive.append("book", book(), INSTRUMENT, NOW), "stored")
            self.assertEqual(archive.append("book", book(NOW+timedelta(seconds=.5)), INSTRUMENT, NOW+timedelta(seconds=.5)), "throttled")
            self.assertEqual(archive.append("book", book(NOW+timedelta(seconds=1)), INSTRUMENT, NOW+timedelta(seconds=1)), "stored")
            self.assertEqual(archive.count, 2)
            self.assertEqual([r[2] for r in self.rows(archive)], [1, 3])

    def test_equal_anonymous_trades_are_preserved_without_guessing_identity(self):
        with self.start() as archive:
            for _ in range(2):
                self.assertEqual(archive.append("trade", trade(), INSTRUMENT, NOW), "stored")
            self.assertEqual(archive.count, 2)
            for row in self.rows(archive):
                self.assertIn("trade_id_unavailable_no_deduplication", json.loads(row[6]))

    def test_known_trade_id_deduplicates_within_same_instrument_source_and_date(self):
        with self.start() as archive:
            self.assertEqual(archive.append("trade", trade(trade_id="id-1"), INSTRUMENT, NOW), "stored")
            self.assertEqual(archive.append("trade", trade(trade_id="id-1"), INSTRUMENT, NOW+timedelta(seconds=1)), "duplicate_id")
            tomorrow = NOW+timedelta(days=1)
            self.assertEqual(archive.append("trade", trade(tomorrow, trade_id="id-1"), INSTRUMENT, tomorrow), "stored")
            self.assertEqual(archive.count, 2)

    def test_future_exchange_timestamp_is_flagged_without_poisoning_next_good_watermark(self):
        with self.start() as archive:
            archive.append("trade", trade(NOW+timedelta(seconds=5)), INSTRUMENT, NOW)
            archive.append("trade", trade(NOW+timedelta(seconds=1)), INSTRUMENT, NOW+timedelta(seconds=2))
            bad, good = self.rows(archive)
            self.assertEqual(bad[5], 0)
            self.assertIn("exchange_timestamp_future", json.loads(bad[6]))
            self.assertEqual(good[5], 1)
            self.assertNotIn("exchange_timestamp_out_of_order", json.loads(good[6]))

    def test_out_of_order_exchange_trades_are_retained_but_not_causal_eligible(self):
        with self.start() as archive:
            archive.append("trade", trade(NOW+timedelta(seconds=1)), INSTRUMENT, NOW+timedelta(seconds=2))
            archive.append("trade", trade(), INSTRUMENT, NOW+timedelta(seconds=3))
            row = self.rows(archive)[1]
            self.assertEqual(row[5], 0)
            self.assertIn("exchange_timestamp_out_of_order", json.loads(row[6]))

    def test_missing_naive_timestamp_inconsistent_book_and_unknown_source_are_flagged(self):
        with self.start() as archive:
            archive.append("book", book(is_consistent=False), INSTRUMENT, NOW)
            archive.append("trade", trade(time=None), INSTRUMENT, NOW+timedelta(seconds=1))
            archive.append("trade", trade(time=NOW.replace(tzinfo=None)), INSTRUMENT, NOW+timedelta(seconds=2))
            archive.append("trade", trade(trade_source="TRADE_SOURCE_DEALER"), INSTRUMENT, NOW+timedelta(seconds=3))
            rows = self.rows(archive)
            self.assertTrue(all(row[5] == 0 for row in rows))
            self.assertIn("book_inconsistent", json.loads(rows[0][6]))
            self.assertIn("source_not_confirmed_exchange", json.loads(rows[-1][6]))

    def test_nonfinite_invalid_levels_are_saved_as_null_without_json_nan(self):
        with self.start() as archive:
            archive.append("book", book(bids=[NS(price=float('nan'), quantity=-1)]), INSTRUMENT, NOW)
            row = self.rows(archive)[0]
            self.assertEqual(row[5], 0)
            self.assertEqual(json.loads(row[7])["bids"], [[None, None]])
            self.assertNotIn("NaN", row[7])

    def test_local_clock_regression_remains_flagged_until_original_high_watermark(self):
        with self.start() as archive:
            archive.append("trade", trade(), INSTRUMENT, NOW+timedelta(seconds=5))
            archive.append("trade", trade(), INSTRUMENT, NOW+timedelta(seconds=1))
            archive.append("trade", trade(), INSTRUMENT, NOW+timedelta(seconds=2))
            self.assertTrue(all("local_clock_regressed" in json.loads(row[6]) for row in self.rows(archive)[1:]))

    def test_row_limit_stops_without_deleting_or_overwriting_old_records(self):
        with self.start(max_records=3) as archive:
            for i in range(3):
                archive.append("trade", trade(), INSTRUMENT, NOW+timedelta(seconds=i))
            before = self.rows(archive)
            with self.assertRaises(ArchiveLimit):
                archive.append("trade", trade(), INSTRUMENT, NOW+timedelta(seconds=4))
            self.assertEqual(archive.count, 3)
            self.assertEqual(self.rows(archive), before)
        with self.assertRaises(ArchiveLimit):
            MicrostructureArchive(self.path, max_records=3)

    def test_sqlite_page_limit_rolls_back_last_record_and_keeps_file_within_budget(self):
        budget = 65_536
        with self.start(max_bytes=budget) as archive:
            with self.assertRaises(ArchiveLimit):
                for i in range(1000):
                    archive.append("trade", trade(NOW+timedelta(seconds=i)), INSTRUMENT, NOW+timedelta(seconds=i))
            self.assertLessEqual(self.path.stat().st_size, budget)
            self.assertGreater(archive.count, 0)
            self.assertEqual(archive.count, archive.db.execute("SELECT COUNT(*) FROM market_events").fetchone()[0])

    def test_foreign_sqlite_file_is_rejected_without_mutation(self):
        self.path.parent.mkdir()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE unrelated (id INTEGER)")
            db.execute("INSERT INTO unrelated VALUES (7)")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            MicrostructureArchive(self.path)
        self.assertEqual(self.path.read_bytes(), before)


class MicrostructureStreamTests(TestCase):
    def test_strict_cli_values_and_maximum_three_tickers(self):
        self.assertEqual(validate_options(["sber", "SBER", "GAZP"], 900), (["SBER", "GAZP"], 900))
        for seconds in (0, -1, 3601, True, 1.5, "30"):
            with self.assertRaises(ValueError):
                validate_options(["SBER"], seconds)
        for symbols in ([], ["SBER"]*4, ["SBER "], ["<script>"], ["СБЕР"]):
            with self.assertRaises(ValueError):
                validate_options(symbols, 1)
        with patch("dashboard.management.commands.collect_microstructure.bounded_client") as create:
            with self.assertRaises(CommandError):
                call_command("collect_microstructure", seconds=0, stdout=io.StringIO(), stderr=io.StringIO())
            create.assert_not_called()

    def test_installed_sdk_subscription_shapes_use_exchange_uid_top20_only(self):
        from t_tech.invest.schemas import OrderBookType, TradeSourceType
        control = StreamControl(1)
        requests = list(islice(subscription_requests([INSTRUMENT], control), 2))
        first = requests[0].subscribe_order_book_request.instruments[0]
        self.assertEqual(first.instrument_id, INSTRUMENT["uid"])
        self.assertEqual(first.depth, 20)
        self.assertEqual(first.order_book_type, OrderBookType.ORDERBOOK_TYPE_EXCHANGE)
        self.assertEqual(requests[1].subscribe_trades_request.trade_source, TradeSourceType.TRADE_SOURCE_EXCHANGE)
        control.cancel()

    def test_deadline_caps_unary_and_stream_and_cancel_closes_underlying_call(self):
        clock = [100.0]
        control = StreamControl(900, clock=lambda: clock[0])
        Details = namedtuple("Details", "method timeout metadata credentials wait_for_ready compression")
        original = Details("market", None, (), None, True, None)
        self.assertEqual(control.details(original, 25).timeout, 25)
        self.assertEqual(control.details(original).timeout, 900)
        clock[0] += 898
        self.assertEqual(control.details(original, 25).timeout, 2)
        stopped = threading.Event()
        control.call = NS(cancel=stopped.set)
        control.cancel()
        self.assertTrue(stopped.is_set())
        self.assertTrue(control.stop.is_set())

    def test_once_cancels_stream_after_first_stored_fixture_and_does_not_send_orders(self):
        with TemporaryDirectory() as directory, MicrostructureArchive(Path(directory)/"micro.sqlite3") as archive:
            archive.begin_run(["SBER"], 30, NOW)
            control = StreamControl(30)
            stopped = threading.Event()
            control.call = NS(cancel=stopped.set)
            client = NS(market_data_stream=NS(market_data_stream=lambda requests: iter([NS(trade=trade())])))
            status, stats, error = consume_stream(client, archive, [INSTRUMENT], control, once=True, now=lambda: NOW)
            self.assertEqual(status, "once")
            self.assertEqual(stats["trades"], 1)
            self.assertIsNone(error)
            self.assertTrue(stopped.is_set())
            archive.finish_run(status, stats, NOW)

    def test_idle_fixture_stream_is_cancelled_on_duration_without_waiting_for_market_message(self):
        with TemporaryDirectory() as directory, MicrostructureArchive(Path(directory)/"micro.sqlite3") as archive:
            archive.begin_run(["SBER"], 1, NOW)
            control = StreamControl(.03)
            stopped = threading.Event()
            control.call = NS(cancel=stopped.set)
            def idle(requests):
                stopped.wait(2)
                yield from ()
            client = NS(market_data_stream=NS(market_data_stream=idle))
            started = time.monotonic()
            status, stats, error = consume_stream(client, archive, [INSTRUMENT], control)
            self.assertLess(time.monotonic()-started, .5)
            self.assertEqual(status, "deadline")
            self.assertEqual(stats["books"]+stats["trades"], 0)
            self.assertTrue(stopped.is_set())

    def test_subscription_rejection_and_ctrl_c_are_explicit_stops(self):
        for interruption in (False, True):
            with TemporaryDirectory() as directory, MicrostructureArchive(Path(directory)/"micro.sqlite3") as archive:
                archive.begin_run(["SBER"], 30, NOW)
                def messages(requests):
                    if interruption:
                        raise KeyboardInterrupt
                    yield NS(subscribe_trades_response=NS(trade_subscriptions=[NS(subscription_status="SUBSCRIPTION_STATUS_LIMIT_IS_EXCEEDED")]))
                client = NS(market_data_stream=NS(market_data_stream=messages))
                status, stats, error = consume_stream(client, archive, [INSTRUMENT], StreamControl(30))
                self.assertEqual(status, "interrupted" if interruption else "subscription_rejected")

    def test_raw_sdk_exception_text_never_reaches_returned_error_message(self):
        with TemporaryDirectory() as directory, MicrostructureArchive(Path(directory)/"micro.sqlite3") as archive:
            archive.begin_run(["SBER"], 30, NOW)
            def broken(requests):
                raise RuntimeError("fixture-secret-token")
            client = NS(market_data_stream=NS(market_data_stream=broken))
            status, stats, error = consume_stream(client, archive, [INSTRUMENT], StreamControl(30))
            self.assertEqual(status, "error")
            self.assertNotIn("fixture-secret-token", error)

    def test_iterator_close_error_does_not_mask_prior_stop_or_leak_exception(self):
        class FailingClose:
            def __iter__(self):
                return self

            def __next__(self):
                return NS(trade=trade())

            def close(self):
                raise RuntimeError("fixture-secret-token")

        with TemporaryDirectory() as directory, MicrostructureArchive(Path(directory)/"micro.sqlite3") as archive:
            archive.begin_run(["SBER"], 30, NOW)
            client = NS(market_data_stream=NS(market_data_stream=lambda requests: FailingClose()))
            status, stats, error = consume_stream(client, archive, [INSTRUMENT], StreamControl(30), once=True, now=lambda: NOW)
            self.assertEqual(status, "once")
            self.assertEqual(stats["iterator_close_failed"], 1)
            self.assertNotIn("fixture-secret-token", error)
