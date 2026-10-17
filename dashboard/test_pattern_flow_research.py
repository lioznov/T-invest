"""Compact v2 evidence reader must fail closed and never fetch or approve trades."""
import hashlib
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.test import RequestFactory, override_settings

from .pattern_flow_research import load_pattern_flow_research
from .pattern_flow_study import PERIODS, PROTOCOL, TICKERS, VARIANTS
from .signal_views import pattern_research


HASHES = {name: "a"*64 for name in ("pattern_flow_study.py", "price_patterns.py", "risk.py", "signals.py")}


def model(profit=.4):
    return {"status": "ok", "trades": 3, "return_pct": profit, "stress_return_pct": profit-.1,
            "win_rate_pct": 66., "max_drawdown_pct": .7, "unverified_trades": 0}


def quality():
    common = {"passed": True, "malformed_records": 0,
              "overall_coverage_pct": 100., "core_coverage_pct": 100.}
    return {**common, "exchange_requested": True, "exchange_source_verified": True,
            "periods": {name: dict(common) for name in PERIODS}}


def compact_summary(protocol_hash):
    return {"schema_version": 2, "protocol": PROTOCOL, "source_sha256": dict(HASHES),
            "protocol_sha256": protocol_hash, "at": "2026-10-03T14:00:00+00:00",
            "results": [{"ticker": ticker, "status": "ok", "quality": quality(),
                         "source": {"file": f"flow-input-{ticker}-20261003.json", "sha256": "c"*64},
                         "patterns": {key: {name: {"flow": model(.4), "baseline": model(.1),
                                                   "difference_pp": 99999} for name in PERIODS}
                                      for key in VARIANTS}} for ticker in TICKERS]}


class PatternFlowResearchTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.directory = self.base/"data"
        self.directory.mkdir()
        override = override_settings(BASE_DIR=self.base)
        override.enable()
        self.addCleanup(override.disable)
        hashes = patch("dashboard.pattern_flow_study.source_hashes", return_value=dict(HASHES))
        hashes.start()
        self.addCleanup(hashes.stop)
        broker = patch("dashboard.market_provider.open_client")
        self.broker = broker.start()
        self.addCleanup(broker.stop)
        self.protocol_path = self.directory/"pattern-flow-protocol-v2.json"
        self.protocol_path.write_text(json.dumps({"source_sha256": HASHES, "spec": {"version": PROTOCOL}}), encoding="utf-8")
        self.protocol_hash = hashlib.sha256(self.protocol_path.read_bytes()).hexdigest()

    def save(self, saved=None, name="pattern-flow-summary-20261003-140000.json"):
        saved = compact_summary(self.protocol_hash) if saved is None else saved
        path = self.directory/name
        path.write_text(json.dumps(saved), encoding="utf-8")
        return path

    def test_absent_summaries_are_insufficient_and_no_broker_call(self):
        result = load_pattern_flow_research("SBER")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["patterns"], [])
        self.assertFalse(result["entry_allowed"])
        self.broker.assert_not_called()

    def test_three_metrics_derive_difference_and_never_approve_entry(self):
        self.save()
        # No flow-input files exist: this proves the reader does not open raw prices.
        result = load_pattern_flow_research("sber")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["patterns"]), 3)
        for pattern in result["patterns"]:
            self.assertAlmostEqual(pattern["difference_pp"], .3)
            self.assertAlmostEqual(pattern["baseline_return_pct"], .1)
            self.assertEqual(pattern["verdict"], "exploratory")
        self.assertFalse(result["entry_allowed"])
        self.broker.assert_not_called()

    def test_corrupt_latest_falls_back_to_older_compact_report(self):
        self.save(name="pattern-flow-summary-20261002.json")
        (self.directory/"pattern-flow-summary-20261003.json").write_text("broken{", encoding="utf-8")
        self.assertEqual(load_pattern_flow_research("SBER")["status"], "ok")

    def test_stale_source_or_protocol_hash_is_rejected(self):
        for field, value in (("source_sha256", {"stale": "d"*64}),
                             ("protocol_sha256", "d"*64), ("protocol", "pattern_flow_study_old")):
            saved = compact_summary(self.protocol_hash)
            saved[field] = value
            self.save(saved)
            self.assertEqual(load_pattern_flow_research("SBER")["status"], "insufficient")
        self.save()
        self.protocol_path.write_text(json.dumps({"source_sha256": {"stale": "d"*64}, "spec": {"version": PROTOCOL}}), encoding="utf-8")
        self.assertEqual(load_pattern_flow_research("SBER")["status"], "insufficient")

    def test_absent_and_missing_stock_are_not_zero_profit(self):
        self.save()
        outside = load_pattern_flow_research("TATN")
        self.assertEqual(outside["status"], "insufficient")
        self.assertEqual(outside["patterns"], [])
        self.assertIn("не считается нулевой", outside["message"])
        saved = compact_summary(self.protocol_hash)
        saved["results"][0].update(status="insufficient", quality={"passed": False})
        self.save(saved)
        missing = load_pattern_flow_research("SBER")
        self.assertEqual(missing["status"], "insufficient")
        self.assertEqual(missing["patterns"], [])

    def test_nonfinite_metrics_and_boolean_counts_are_rejected(self):
        for which, field, value in (("flow", "return_pct", float("nan")),
                                    ("flow", "stress_return_pct", float("inf")),
                                    ("baseline", "return_pct", True),
                                    ("flow", "trades", True),
                                    ("flow", "unverified_trades", False),
                                    ("baseline", "unverified_trades", False)):
            saved = compact_summary(self.protocol_hash)
            saved["results"][0]["patterns"][next(iter(VARIANTS))]["recent"][which][field] = value
            self.save(saved)
            self.assertEqual(load_pattern_flow_research("SBER")["status"], "insufficient", (which, field, value))

    def test_missing_dealer_and_low_coverage_quality_is_insufficient(self):
        changes = (None, {**quality(), "exchange_source_verified": False},
                   {**quality(), "exchange_requested": False},
                   {**quality(), "passed": False},
                   {**quality(), "core_coverage_pct": 98.99})
        for bad_quality in changes:
            saved = compact_summary(self.protocol_hash)
            saved["results"][0]["quality"] = bad_quality
            self.save(saved)
            result = load_pattern_flow_research("SBER")
            self.assertEqual(result["status"], "insufficient")
            self.assertEqual(result["patterns"], [])

    def test_boolean_quality_count_and_wrong_period_keys_are_rejected(self):
        for bad_quality in ({**quality(), "malformed_records": False},
                            {**quality(), "periods": {"a": quality()["periods"]["recent"],
                                                      "b": quality()["periods"]["recent"],
                                                      "c": quality()["periods"]["recent"]}}):
            saved = compact_summary(self.protocol_hash)
            saved["results"][0]["quality"] = bad_quality
            self.save(saved)
            self.assertEqual(load_pattern_flow_research("SBER")["status"], "insufficient")

    def test_zero_trades_produce_none_metrics_not_profit_claim(self):
        saved = compact_summary(self.protocol_hash)
        for periods in saved["results"][0]["patterns"].values():
            for which in ("flow", "baseline"):
                periods["recent"][which].update(trades=0, return_pct=0., stress_return_pct=0.,
                                                win_rate_pct=None, max_drawdown_pct=0.)
        self.save(saved)
        result = load_pattern_flow_research("SBER")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(len(result["patterns"]), 3)
        for pattern in result["patterns"]:
            self.assertEqual(pattern["verdict"], "insufficient")
            self.assertTrue(all(pattern[field] is None for field in (
                "return_pct", "stress_return_pct", "win_rate_pct", "max_drawdown_pct",
                "baseline_return_pct", "difference_pp")))

    def test_insufficient_status_cannot_smuggle_positive_trade_count(self):
        saved = compact_summary(self.protocol_hash)
        for periods in saved["results"][0]["patterns"].values():
            periods["recent"]["flow"].update(status="insufficient", trades=1, return_pct=None,
                                              stress_return_pct=None, win_rate_pct=None,
                                              max_drawdown_pct=None)
        self.save(saved)
        self.assertEqual(load_pattern_flow_research("SBER")["status"], "insufficient")

    def test_oversized_summary_and_boolean_schema_fail_closed(self):
        self.save()
        with patch("dashboard.pattern_flow_research.MAX_BYTES", 10):
            self.assertEqual(load_pattern_flow_research("SBER")["status"], "insufficient")
        self.save({**compact_summary(self.protocol_hash), "schema_version": True})
        self.assertEqual(load_pattern_flow_research("SBER")["status"], "insufficient")

    def test_endpoint_is_get_only_no_store_and_demo_never_reads_history(self):
        factory = RequestFactory()
        with patch("dashboard.pattern_research.load_pattern_research") as old_reader, \
                patch("dashboard.pattern_flow_research.load_pattern_flow_research") as flow_reader:
            demo = pattern_research(factory.get("/api/pattern-research", {"mode": "demo", "ticker": "SBER"}))
            self.assertEqual(demo.status_code, 200)
            self.assertEqual(demo["Cache-Control"], "no-store")
            old_reader.assert_not_called()
            flow_reader.assert_not_called()
            denied = pattern_research(factory.post("/api/pattern-research", {"mode": "live", "ticker": "SBER"}))
            self.assertEqual(denied.status_code, 405)
            old_reader.assert_not_called()
            flow_reader.assert_not_called()
            old_reader.return_value = {"status": "insufficient", "patterns": []}
            flow_reader.return_value = {"status": "insufficient", "entry_allowed": False}
            live = pattern_research(factory.get("/api/pattern-research", {"mode": "live", "ticker": "SBER"}))
            self.assertEqual(live.status_code, 200)
            self.assertEqual(live["Cache-Control"], "no-store")
            body = json.loads(live.content)
            self.assertFalse(body["historical_flow"]["entry_allowed"])
            old_reader.assert_called_once_with("SBER")
            flow_reader.assert_called_once_with("SBER")
        self.broker.assert_not_called()
