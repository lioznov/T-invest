"""Causality, source quality and immutable acquisition fixtures; no real API calls."""
import io
import json
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from .pattern_flow_study import (END, START, NAMES, PERIODS, TICKERS, VARIANTS, classify_flow,
                                 compare_flow, filter_flow_events, flow_coverage, passes_flow_screen)
from .management.commands.study_pattern_flow import acquire, freeze_protocol, load_input, write_immutable
from .signals import Candle, MSK


def candles_for_periods():
    starts = (datetime(2026, 4, 6, 7, tzinfo=timezone.utc),
              datetime(2026, 8, 3, 7, tzinfo=timezone.utc),
              datetime(2026, 9, 1, 7, tzinfo=timezone.utc))
    return [Candle(start+timedelta(minutes=15*i), 100, 100, 100, 100, 100)
            for start in starts for i in range(100)]


def snapshot(ticker="SBER", candles=None):
    candles = candles or candles_for_periods()
    return {"schema_version": 1, "source": "T-Bank SDK GetCandles", "fetched_at": END.isoformat(),
            "instrument": {"ticker": ticker, "uid": "fixture", "name": ticker, "lot": 10},
            "request": {"from_utc": START.isoformat(), "to_utc_exclusive": END.isoformat(),
                        "interval_minutes": 15, "maximum_chunk_days": 7,
                        "candle_source_type": "CANDLE_SOURCE_EXCHANGE", "chunks": []},
            "bars": [{"time": c.time.isoformat(), "open": c.open, "high": c.high,
                      "low": c.low, "close": c.close, "volume": c.volume,
                      "volume_buy": 60, "volume_sell": 40, "is_complete": True,
                      "candle_source": "CANDLE_SOURCE_EXCHANGE"} for c in candles]}


def fake_event(candles, i=100):
    return {"key": "reclaim", "time": candles[i].time.isoformat(), "level": 99.9,
            "floor": 99, "atr": .1, "price": 100, "rvol": 1.5}


def execution_fixture():
    at = datetime(2026, 4, 6, 7, 15, tzinfo=timezone.utc)  # Confirm 10:15, fill 10:30.
    candles = [Candle(at+timedelta(minutes=15*(i-100)), 100, 100.2, 99.8, 100, 100)
               for i in range(105)]
    c = candles[102]
    candles[102] = Candle(c.time, 100, 103, 99.8, 102, 100)
    events = {family: [None]*len(candles) for family in NAMES}
    events["reclaim"][100] = fake_event(candles)
    return candles, events


class FlowClassificationTests(TestCase):
    def test_exactly_three_registered_hypotheses(self):
        self.assertEqual(len(VARIANTS), 3)
        self.assertEqual({v["family"] for v in VARIANTS.values()}, set(NAMES))
        self.assertTrue(all(v["volume"] is True and v["buy_fraction_min"] == .6 for v in VARIANTS.values()))

    def test_classified_buy_fraction_is_broker_split_not_inferred_price(self):
        self.assertEqual(classify_flow(100, 60, 40), ("classified", .6))
        self.assertEqual(classify_flow(100, 0, 100), ("classified", 0))
        self.assertEqual(classify_flow(100, 100, 0), ("classified", 1))

    def test_missing_and_unpopulated_are_unknown_not_sell(self):
        self.assertEqual(classify_flow(100, None, 100), ("unknown_missing", None))
        self.assertEqual(classify_flow(100, 0, 0), ("unknown_unpopulated", None))
        self.assertEqual(classify_flow(0, 0, 0), ("unknown_zero_volume", None))

    def test_negative_float_boolean_and_inconsistent_sum_are_malformed(self):
        for values in ((100, -1, 101), (100, 60., 40), (100, True, 99),
                       (100, 70, 40), (100, 30, 40), (0, 1, 0), (-1, 0, 0), (True, 0, 1)):
            self.assertEqual(classify_flow(*values), ("malformed", None))

    def test_filter_threshold_is_inclusive_and_input_is_not_mutated(self):
        candles, events = execution_fixture()
        original = deepcopy(events)
        buys, sells = [60]*len(candles), [40]*len(candles)
        filtered = filter_flow_events(events, candles, buys, sells)
        self.assertIsNotNone(filtered["reclaim"][100])
        filtered["reclaim"][100]["rvol"] = 99
        self.assertEqual(events, original)
        buys[100], sells[100] = 59, 41
        self.assertIsNone(filter_flow_events(events, candles, buys, sells)["reclaim"][100])

    def test_future_flow_does_not_change_confirmation_filter(self):
        candles, events = execution_fixture()
        buys, sells = [60]*len(candles), [40]*len(candles)
        before = filter_flow_events(events, candles, buys, sells)["reclaim"][100]
        buys[101:], sells[101:] = [0]*4, [100]*4
        self.assertEqual(filter_flow_events(events, candles, buys, sells)["reclaim"][100], before)

    def test_unknown_confirmation_never_enters(self):
        candles, events = execution_fixture()
        buys, sells = [60]*len(candles), [40]*len(candles)
        buys[100], sells[100] = None, None
        self.assertIsNone(filter_flow_events(events, candles, buys, sells)["reclaim"][100])

    def test_mismatched_arrays_raise_instead_of_truncating_zip(self):
        candles, events = execution_fixture()
        with self.assertRaises(ValueError):
            filter_flow_events(events, candles, [60], [40])
        with self.assertRaises(ValueError):
            flow_coverage(candles, [60], [40])

    def test_same_source_baseline_and_accepted_flow_have_identical_trades(self):
        candles, events = execution_fixture()
        with patch("dashboard.pattern_flow_study.scan_patterns", return_value=events):
            result = compare_flow(candles, [60]*len(candles), [40]*len(candles), 10)
        model = result["reclaim_buy60"]["development"]
        self.assertEqual(model["baseline"]["trades"], 1)
        self.assertEqual(model["flow"]["trade_log"], model["baseline"]["trade_log"])
        self.assertEqual(model["flow"]["stress_return_pct"], model["baseline"]["stress_return_pct"])
        self.assertEqual(model["difference_pp"], 0)

    def test_filter_changes_only_entry_availability_not_exit_engine(self):
        candles, events = execution_fixture()
        with patch("dashboard.pattern_flow_study.scan_patterns", return_value=events):
            result = compare_flow(candles, [50]*len(candles), [50]*len(candles), 10)
        model = result["reclaim_buy60"]["development"]
        self.assertEqual(model["baseline"]["trades"], 1)
        self.assertEqual(model["flow"]["trades"], 0)


class FlowQualityTests(TestCase):
    def test_complete_coverage_passes_all_preinspected_periods(self):
        candles = candles_for_periods()
        quality = flow_coverage(candles, [60]*len(candles), [40]*len(candles))
        self.assertTrue(quality["passed"])
        self.assertEqual(quality["overall_coverage_pct"], 100)
        self.assertEqual(quality["core_coverage_pct"], 100)

    def test_all_zero_fields_are_unknown_and_do_not_pass(self):
        candles = candles_for_periods()
        quality = flow_coverage(candles, [0]*len(candles), [0]*len(candles))
        self.assertFalse(quality["passed"])
        self.assertEqual(quality["overall_coverage_pct"], 0)
        self.assertEqual(quality["states"]["unknown_unpopulated"], len(candles))

    def test_below_99_percent_core_coverage_fails(self):
        candles = candles_for_periods()
        buys, sells = [60]*len(candles), [40]*len(candles)
        for i in (0, 1):
            buys[i], sells[i] = None, None
        quality = flow_coverage(candles, buys, sells)
        self.assertFalse(quality["passed"])
        self.assertLess(quality["periods"]["development"]["core_coverage_pct"], 99)

    def test_one_malformed_record_invalidates_otherwise_high_coverage(self):
        candles = candles_for_periods()
        buys, sells = [60]*len(candles), [40]*len(candles)
        buys[-1] = -1
        quality = flow_coverage(candles, buys, sells)
        self.assertGreater(quality["overall_coverage_pct"], 99)
        self.assertEqual(quality["malformed_records"], 1)
        self.assertFalse(quality["passed"])

    def test_zero_volume_not_divided_or_counted_as_a_sell(self):
        candles = [Candle(datetime(2026, 4, 6, 7, tzinfo=timezone.utc), 100, 100, 100, 100, 0)]
        quality = flow_coverage(candles, [0], [0])
        self.assertIsNone(quality["overall_coverage_pct"])
        self.assertFalse(quality["passed"])

    def test_missing_stock_and_bad_quality_veto_profitable_arithmetic(self):
        good = {name: {"accounts": 7, "trades": 50, "return_pct": 1., "stress_return_pct": .1,
                       "unverified_trades": 0, "max_drawdown_pct": 1., "positive_accounts": 5}
                for name in PERIODS}
        qualities = {ticker: {"passed": True} for ticker in TICKERS}
        self.assertTrue(passes_flow_screen(good, qualities))
        qualities["SBER"]["passed"] = False
        self.assertFalse(passes_flow_screen(good, qualities))
        qualities.pop("SBER")
        self.assertFalse(passes_flow_screen(good, qualities))


class FlowInputAndProtocolTests(TestCase):
    def test_protocol_freezes_before_missing_input_and_no_api_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with override_settings(BASE_DIR=base), patch("dashboard.market_provider.open_client") as client:
                with self.assertRaises(CommandError):
                    call_command("study_pattern_flow", stdout=io.StringIO(), stderr=io.StringIO())
            client.assert_not_called()
            self.assertTrue((base/"data"/"pattern-flow-protocol-v2.json").exists())
            self.assertFalse(list((base/"data").glob("pattern-flow-study-*.json")))

    def test_freeze_replay_and_source_mismatch_preserve_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original, digest = freeze_protocol(directory)
            self.assertEqual(freeze_protocol(directory), (original, digest))
            with patch("dashboard.management.commands.study_pattern_flow.source_hashes", return_value={"new": "f"*64}):
                with self.assertRaises(CommandError):
                    freeze_protocol(directory)
            self.assertEqual(json.loads((directory/"pattern-flow-protocol-v2.json").read_text(encoding="utf-8")), original)
            self.assertEqual(len(list(directory.glob("pattern-flow-protocol-mismatch-*.json"))), 1)

    def test_original_weekend_source_candles_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"flow-input-SBER-20261003.json"
            saved = snapshot()
            weekend = dict(saved["bars"][0], time="2026-04-11T07:00:00+00:00")
            saved["bars"].append(weekend)
            saved["bars"].sort(key=lambda row: row["time"])
            write_immutable(path, saved)
            loaded = load_input(path, "SBER")
            self.assertEqual(len(loaded["candles"]), 301)
            self.assertTrue(any(c.time.astimezone(MSK).weekday() == 5 for c in loaded["candles"]))

    def test_unknown_and_dealer_sources_veto_eligibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            for index, source in enumerate((None, "CANDLE_SOURCE_DEALER_WEEKEND")):
                path = Path(temporary)/f"input-{index}.json"
                saved = snapshot()
                saved["bars"][0]["candle_source"] = source
                write_immutable(path, saved)
                self.assertFalse(load_input(path, "SBER")["quality"]["passed"])

    def test_unsupported_source_request_is_not_exchange_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"input.json"
            saved = snapshot()
            saved["request"]["candle_source_type"] = "unsupported_by_sdk"
            write_immutable(path, saved)
            self.assertFalse(load_input(path, "SBER")["quality"]["passed"])

    def test_cached_replay_makes_no_broker_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_immutable(directory/"flow-input-SBER-20261003.json", snapshot())
            with patch("dashboard.market_provider.open_client") as client:
                result = acquire(directory, "SBER", fetch=True)
            client.assert_not_called()
            self.assertTrue(result["quality"]["passed"])
            self.assertEqual(len(result["source"]["sha256"]), 64)

    def test_malformed_cache_is_never_overwritten_or_refetched(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory/"flow-input-SBER-20261003.json"
            path.write_text("broken", encoding="utf-8")
            with patch("dashboard.market_provider.open_client") as client:
                with self.assertRaises(CommandError):
                    acquire(directory, "SBER", fetch=True)
            client.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_reserved_stock_cannot_be_fetched(self):
        with tempfile.TemporaryDirectory() as temporary, patch("dashboard.market_provider.open_client") as client:
            with self.assertRaises(CommandError):
                acquire(Path(temporary), "TATN", fetch=True)
            client.assert_not_called()

    def test_immutable_writer_does_not_replace_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"input.json"
            write_immutable(path, {"original": True})
            with self.assertRaises(FileExistsError):
                write_immutable(path, {"original": False})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"original": True})

    def test_acquisition_chunks_and_exchange_enum_without_network(self):
        from t_tech.invest.schemas import CandleSource
        calls = []
        class FakeMarket:
            def get_candles(self, *, instrument_id, from_, to, interval, candle_source_type=None):
                calls.append({"from": from_, "to": to, "source": candle_source_type})
                q = SimpleNamespace(units=100, nano=0)
                candle = SimpleNamespace(time=from_, open=q, high=q, low=q, close=q, volume=100,
                                         volume_buy=60, volume_sell=40, is_complete=True,
                                         candle_source=CandleSource.CANDLE_SOURCE_EXCHANGE)
                return SimpleNamespace(candles=[candle])
        class FakeClient:
            market_data = FakeMarket()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        instrument = {"ticker": "SBER", "uid": "fixture", "name": "fixture", "lot": 10}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch("dashboard.market_provider.open_client", return_value=FakeClient()), \
                    patch("dashboard.market_provider.resolve", return_value=instrument):
                result = acquire(directory, "SBER", fetch=True)
            self.assertEqual(calls[0]["from"], START)
            self.assertEqual(calls[-1]["to"], END)
            self.assertTrue(all(c["to"]-c["from"] <= timedelta(days=7) for c in calls))
            self.assertTrue(all(c["source"] == CandleSource.CANDLE_SOURCE_EXCHANGE for c in calls))
            self.assertEqual(len(result["saved"]["bars"]), len(calls))
            self.assertTrue(all(row["volume_buy"] == 60 and row["volume_sell"] == 40 for row in result["saved"]["bars"]))

    def test_command_reports_missing_stock_as_insufficient_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            directory = base/"data"
            directory.mkdir()
            write_immutable(directory/"flow-input-SBER-20261003.json", snapshot())
            with override_settings(BASE_DIR=base), patch("dashboard.market_provider.open_client") as client:
                call_command("study_pattern_flow", stdout=io.StringIO(), stderr=io.StringIO())
            client.assert_not_called()
            saved = json.loads(next(directory.glob("pattern-flow-summary-*.json")).read_text(encoding="utf-8"))
            missing = next(r for r in saved["results"] if r["ticker"] == "GAZP")
            self.assertEqual(missing["status"], "insufficient")
            self.assertIsNone(missing["patterns"]["reclaim_buy60"]["recent"]["flow"]["return_pct"])
            self.assertFalse(any(s["eligible"] for s in saved["summaries"]))
            self.assertIsNone(saved["candidate"])
            self.assertNotIn("trade_log", saved["results"][0]["patterns"]["reclaim_buy60"]["recent"]["flow"])
