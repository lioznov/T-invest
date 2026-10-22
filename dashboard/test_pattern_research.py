"""Summary safety and research protocol tests; synthetic results are not alpha."""
import io
import json
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from .pattern_research import PROTOCOL, SOURCE_MODULES, load_pattern_research
from .management.commands.study_patterns import (PERIODS, TICKERS, compact_pattern,
                                                eligible, freeze_protocol, read_input)
from .signals import Candle


HASHES = {name: "a"*64 for name in SOURCE_MODULES}
VARIANTS = {f"{family}_{int(volume)}": {"family": family, "volume": volume, "name": f"{family} {volume}"}
            for family in ("reclaim", "orb_retest", "flag") for volume in (False, True)}


def summary(ticker="SBER"):
    return {"schema_version": 1, "protocol": PROTOCOL, "at": "2026-10-03T12:00:00+00:00",
            "protocol_sha256": "b"*64, "source_sha256": dict(HASHES),
            "source": {"file": "input.json", "sha256": "c"*64},
            "results": [{"ticker": ticker, "status": "ok", "patterns": [
                {"key": key, "name": variant["name"], "trades": 3,
                 "return_pct": .2, "stress_return_pct": .1, "win_rate_pct": 66.,
                 "max_drawdown_pct": .5, "verdict": "exploratory"}
                for key, variant in VARIANTS.items()]}]}


class PatternResearchReaderTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)/"data"
        self.directory.mkdir()
        override = override_settings(BASE_DIR=Path(self.temporary.name))
        override.enable()
        self.addCleanup(override.disable)
        hashes = patch("dashboard.pattern_research._current_hashes", return_value=dict(HASHES))
        hashes.start()
        self.addCleanup(hashes.stop)

    def save(self, saved, filename="pattern-study-summary-20261003-120000-000000.json"):
        path = self.directory/filename
        path.write_text(json.dumps(saved), encoding="utf-8")
        return path

    def test_absent_file_is_insufficient(self):
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_successful_compact_reader_does_not_read_raw_report(self):
        self.save(summary())
        (self.directory/"pattern-study-20261003.json").write_text("not raw-readable", encoding="utf-8")
        result = load_pattern_research("sber")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["patterns"]), 6)
        self.assertIn("не независимая", result["note"])
        self.assertNotIn("trade_log", result["patterns"][0])

    def test_invalid_json_is_not_a_result(self):
        (self.directory/"pattern-study-summary-20261003.json").write_text("{", encoding="utf-8")
        self.assertEqual(load_pattern_research("SBER")["patterns"], [])

    def test_corrupt_newest_falls_back_to_valid_older(self):
        self.save(summary(), "pattern-study-summary-20261002.json")
        (self.directory/"pattern-study-summary-20261003.json").write_text("bad", encoding="utf-8")
        self.assertEqual(load_pattern_research("SBER")["status"], "ok")

    def test_missing_ticker_is_not_zero_return(self):
        self.save(summary("SBER"))
        result = load_pattern_research("GAZP")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["patterns"], [])
        self.assertIn("не считается нулевым", result["message"])

    def test_unknown_source_protocol_is_rejected(self):
        saved = summary()
        saved["protocol"] = "made_up_profitable_v99"
        self.save(saved)
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_float_schema_version_is_rejected(self):
        self.save({**summary(), "schema_version": 1.0})
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_source_change_invalidates_archived_result(self):
        saved = summary()
        saved["source_sha256"]["risk.py"] = "d"*64
        self.save(saved)
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_missing_hash_is_rejected(self):
        saved = summary()
        del saved["source_sha256"]["signals.py"]
        self.save(saved)
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_nonfinite_metrics_are_rejected(self):
        saved = summary()
        saved["results"][0]["patterns"][0]["return_pct"] = float("nan")
        self.save(saved)
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_boolean_metric_is_rejected(self):
        saved = summary()
        saved["results"][0]["patterns"][0]["stress_return_pct"] = True
        self.save(saved)
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_confirmed_verdict_cannot_be_invented(self):
        saved = summary()
        saved["results"][0]["patterns"][0]["verdict"] = "confirmed"
        self.save(saved)
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_malformed_root_and_naive_timestamp_fail_closed(self):
        for invalid in ([], {"schema_version": 1}, {**summary(), "at": "2026-10-03T12:00:00"}):
            self.save(invalid)
            self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_oversized_summary_is_skipped(self):
        self.save(summary())
        with patch("dashboard.pattern_research.MAX_SUMMARY_BYTES", 20):
            self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_no_trades_is_insufficient_not_recommendation(self):
        saved = summary()
        for item in saved["results"][0]["patterns"]:
            item.update(trades=0, return_pct=0, stress_return_pct=0, win_rate_pct=None,
                        verdict="insufficient")
        self.save(saved)
        self.assertEqual(load_pattern_research("SBER")["status"], "insufficient")

    def test_invalid_ticker_fails_closed(self):
        self.assertEqual(load_pattern_research("../SBER")["status"], "insufficient")


class PatternResearchProtocolTests(TestCase):
    def test_gate_requires_all_periods_and_seven_stocks(self):
        good = {name: {"accounts": 7, "trades": 50, "return_pct": 1,
                      "stress_return_pct": .1, "unverified_trades": 0,
                      "max_drawdown_pct": 1, "positive_accounts": 4} for name in PERIODS}
        self.assertTrue(eligible(good))
        self.assertFalse(eligible({"development": good["development"]}))
        good["selection"]["accounts"] = 6
        self.assertFalse(eligible(good))

    def test_positive_normal_result_cannot_pass_negative_stress(self):
        good = {name: {"accounts": 7, "trades": 50, "return_pct": 1,
                      "stress_return_pct": .1, "unverified_trades": 0,
                      "max_drawdown_pct": 1, "positive_accounts": 4} for name in PERIODS}
        good["recent"]["stress_return_pct"] = -.01
        self.assertFalse(eligible(good))

    def test_freeze_replays_and_preserves_source_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first, first_hash = freeze_protocol(directory, VARIANTS)
            again, again_hash = freeze_protocol(directory, VARIANTS)
            self.assertEqual(first, again)
            self.assertEqual(first_hash, again_hash)
            changed = {key: dict(value) for key, value in VARIANTS.items()}
            changed[next(iter(changed))]["name"] += " modified"
            with self.assertRaises(CommandError):
                freeze_protocol(directory, changed)
            self.assertEqual(json.loads((directory/"pattern-study-protocol-v1.json").read_text(encoding="utf-8")), first)
            self.assertEqual(len(list(directory.glob("pattern-study-protocol-mismatch-*.json"))), 1)

    def test_input_snapshot_path_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            directory = base/"data"
            directory.mkdir()
            outside = base/"outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(CommandError):
                read_input(directory, str(outside))

    def test_invalid_ohlc_is_excluded_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            row = {"ticker": "SBER", "instrument": {"lot": 10}, "candles": [
                {"time": "2026-04-01T07:00:00+00:00", "open": 100, "high": 90,
                 "low": 100, "close": 100, "volume": 1}]}
            source = directory/"input.json"
            source.write_text(json.dumps({"results": [row]}), encoding="utf-8")
            datasets, errors, identity = read_input(directory, str(source))
            self.assertEqual(datasets, {})
            self.assertIn("SBER", errors)
            self.assertEqual(len(identity["sha256"]), 64)

    def test_duplicate_ticker_cannot_change_weight(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            row = {"ticker": "SBER", "instrument": {"lot": 10}, "candles": [
                {"time": "2026-04-01T07:00:00+00:00", "open": 100, "high": 100,
                 "low": 100, "close": 100, "volume": 1}]}
            source = directory/"input.json"
            source.write_text(json.dumps({"results": [row, row]}), encoding="utf-8")
            datasets, errors, _ = read_input(directory, str(source))
            self.assertNotIn("SBER", datasets)
            self.assertIn("Duplicate", errors["SBER"])

    def test_unverified_positive_result_stays_failed(self):
        model = {"status": "ok", "trades": 1, "return_pct": 1., "stress_return_pct": .5,
                 "win_rate_pct": 100., "max_drawdown_pct": 0., "unverified_trades": 1}
        result = compact_pattern("k", {"name": "n"}, model)
        self.assertEqual(result["verdict"], "failed")

    def test_command_keeps_missing_stocks_out_of_aggregate(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            directory = base/"data"
            directory.mkdir()
            start = datetime(2026, 3, 30, 7, tzinfo=timezone.utc)
            candles = [asdict(Candle(start+timedelta(minutes=15*i), 100, 100, 100, 100, 10)) for i in range(110)]
            source = directory/"input.json"
            source.write_text(json.dumps({"results": [{"ticker": "SBER", "instrument": {"lot": 10},
                                                       "candles": candles}]}, default=lambda value: value.isoformat()), encoding="utf-8")
            with override_settings(BASE_DIR=base):
                call_command("study_patterns", input=str(source), stdout=io.StringIO())
            saved = json.loads(next(directory.glob("pattern-study-summary-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(len(saved["results"]), len(TICKERS))
            missing = next(row for row in saved["results"] if row["ticker"] == "GAZP")
            self.assertEqual(missing["status"], "insufficient")
            self.assertIsNone(missing["patterns"][0]["return_pct"])
            self.assertFalse(any(row["eligible"] for row in saved["summaries"]))
            self.assertIsNone(saved["candidate"])
            self.assertFalse((directory/"pattern-study-choice-v1.json").exists())
