"""Causality and incomplete-data checks for observed-quote paper trades."""
import json
import sqlite3
import tempfile
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from django.test import override_settings

from .paper_journal import STRATEGY_VERSION, load_paper_journal, review_paper_trades


START = datetime(2026, 9, 25, 9, tzinfo=timezone.utc)


def row(seconds, decision="LONG", bid=99.9, ask=100, **changes):
    at = START + timedelta(seconds=seconds)
    payload = {"strategy_version": STRATEGY_VERSION, "review_ready": True, "decision": decision,
               "book": {"time": at.isoformat(), "bids": [[bid, 10]], "asks": [[ask, 10]]},
               "plan": {"entry": 100, "stop": 98, "target": 104, "risk_per_share": 2},
               "instrument": {"ticker": "SBER", "lot": 10}}
    payload.update(changes)
    return at.isoformat(), payload


def review(rows, seconds, fee=0, slip=0):
    return review_paper_trades(rows, fee, slip, START + timedelta(seconds=seconds))


class PaperJournalTests(TestCase):
    def test_signal_has_no_same_snapshot_fill_or_future_knowledge(self):
        rows = [row(0), row(30), row(60, bid=105, ask=105.1)]
        result = review(rows, 0)
        self.assertEqual(result["summary"]["pending"], 1)
        self.assertIsNone(result["trades"][0]["entry"])
        self.assertIsNone(result["summary"]["net_return_pct"])
        self.assertEqual(result["coverage"]["observations"], 1)
        result = review(rows, 60)
        self.assertEqual(result["trades"][0]["entry_at"], rows[1][0])
        self.assertEqual(result["trades"][0]["exit"], 104)
        self.assertEqual(result["summary"]["closed"], 1)

    def test_stop_uses_observed_worse_bid_and_both_sides_costs(self):
        rows = [row(0), row(30), row(60, bid=97, ask=97.1)]
        trade = review(rows, 60, fee=5, slip=10)["trades"][0]
        self.assertAlmostEqual(trade["entry"], 100 * 1.001)
        self.assertAlmostEqual(trade["exit"], 97 * .999)
        expected = (97 * .999 * .9995 / (100 * 1.001 * 1.0005) - 1) * 100
        self.assertAlmostEqual(trade["net_pct"], expected)
        self.assertEqual(trade["reason"], "stop")

    def test_continuous_long_does_not_repeat_even_after_exit(self):
        rows = [row(0), row(30), row(60, bid=105, ask=105.1), row(90), row(120)]
        self.assertEqual(review(rows, 120)["summary"]["total"], 1)
        rows += [row(150, "WAIT"), row(180), row(210)]
        result = review(rows, 210)
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["summary"]["open"], 1)

    def test_gap_does_not_invent_stop_fill_or_rearm_continuous_long(self):
        rows = [row(0), row(30), row(180, bid=90, ask=90.1), row(210)]
        result = review(rows, 210)
        self.assertEqual(result["summary"]["total"], 1)
        self.assertEqual(result["summary"]["unverified"], 1)
        trade = result["trades"][0]
        self.assertEqual(trade["reason"], "data_gap")
        self.assertIsNone(trade["exit"])
        self.assertIsNone(trade["net_pct"])

    def test_no_followup_and_stale_open_remain_unverified(self):
        self.assertEqual(review([row(0)], 30)["trades"][0]["status"], "pending")
        result = review([row(0)], 91)
        self.assertEqual(result["trades"][0]["reason"], "entry_timeout")
        self.assertIsNone(result["trades"][0]["net_pct"])
        self.assertEqual(review([row(0), row(30)], 60)["trades"][0]["status"], "open")
        self.assertEqual(review([row(0), row(30)], 121)["trades"][0]["status"], "unverified")

    def test_old_version_or_missing_plan_cannot_generate_trade(self):
        old = row(0)
        old[1].pop("strategy_version")
        rows = [old, row(30, plan=None), row(60, strategy_version="unknown")]
        self.assertEqual(review(rows, 60)["summary"]["total"], 0)

    def test_plan_frozen_and_next_price_must_stay_inside_it(self):
        changed = row(30, plan={"entry": 100, "stop": 10, "target": 500, "risk_per_share": 90})
        result = review([row(0), changed, row(60, bid=97, ask=97.1)], 60)
        self.assertEqual(result["trades"][0]["stop"], 98)
        self.assertEqual(result["trades"][0]["reason"], "stop")
        result = review([row(0), row(30, bid=105, ask=105.1)], 30)
        self.assertEqual(result["trades"][0]["reason"], "entry_outside_plan")
        self.assertIsNone(result["trades"][0]["entry"])

    def test_cached_or_future_book_cannot_be_used_for_fill(self):
        unchanged = row(30)
        unchanged[1]["book"]["time"] = START.isoformat()
        future = row(60)
        future[1]["book"]["time"] = (START + timedelta(seconds=61)).isoformat()
        rows = [row(0), unchanged, future]
        self.assertEqual(review(rows, 60)["summary"]["pending"], 1)
        self.assertEqual(review(rows, 91)["trades"][0]["reason"], "entry_timeout")

    def test_duplicate_timestamp_and_unready_quotes_do_not_fill(self):
        rows = [row(0), row(0), row(30, review_ready=False)]
        self.assertEqual(review(rows, 30)["summary"]["pending"], 1)
        result = review(rows + [row(60)], 60)
        self.assertEqual(result["summary"]["open"], 1)
        self.assertEqual(result["trades"][0]["entry_at"], row(60)[0])

    def test_exit_and_time_limit_use_subsequent_bid(self):
        result = review([row(0), row(30), row(60, "EXIT", bid=101, ask=101.1)], 60)
        self.assertEqual(result["trades"][0]["reason"], "exit")
        rows = [row(seconds) for seconds in range(0, 180 * 60 + 31, 30)]
        result = review(rows, 180 * 60 + 30)
        self.assertEqual(result["trades"][0]["reason"], "time")
        self.assertEqual(result["summary"]["total"], 1)

    def test_version_change_invalidates_position(self):
        result = review([row(0), row(30), row(60, strategy_version="six_rules_v2")], 60)
        self.assertEqual(result["trades"][0]["reason"], "version_changed")
        self.assertEqual(result["summary"]["closed"], 0)

    def test_bad_data_is_ignored_and_costs_are_finite(self):
        missing = row(0)
        missing[1]["book"].pop("time")
        invalid = row(30, bid=float("nan"))
        crossed = row(60, bid=110, ask=100)
        result = review([("broken", "not json"), missing, invalid, crossed], 60)
        self.assertEqual(result["summary"]["total"], 0)
        with self.assertRaises(ValueError):
            review([], 0, fee=-1)
        with self.assertRaises(ValueError):
            review([], 0, slip=float("nan"))

    def test_serialized_rows_sort_without_mutation_and_report_all_trades(self):
        rows = []
        for number in range(55):
            offset = number * 120
            rows += [row(offset, "WAIT"), row(offset + 30), row(offset + 60),
                     row(offset + 90, "EXIT", bid=101, ask=101.1)]
        original = deepcopy(rows)
        serialized = [(at, json.dumps(payload)) for at, payload in reversed(rows)]
        result = review(serialized, 55 * 120)
        self.assertEqual(rows, original)
        self.assertEqual(result["summary"]["closed"], 55)
        self.assertEqual(len(result["trades"]), 50)
        self.assertAlmostEqual(result["summary"]["net_return_pct"], (1.01 ** 55 - 1) * 100)
        self.assertEqual(result["summary"]["win_rate"], 100)
        self.assertEqual(result["summary"]["max_drawdown_pct"], 0)
        self.assertIsNone(result["summary"]["profit_factor"])

    def test_summary_drawdown_and_profit_factor_use_closed_trades_only(self):
        rows = [row(0), row(30), row(60, bid=105, ask=105.1), row(90, "WAIT"),
                row(120), row(150), row(180, bid=97, ask=97.1), row(210, "WAIT"),
                row(240)]
        summary = review(rows, 240)["summary"]
        self.assertEqual(summary["closed"], 2)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["win_rate"], 50)
        self.assertAlmostEqual(summary["profit_factor"], 4 / 3)
        self.assertAlmostEqual(summary["max_drawdown_pct"], 3)
        self.assertAlmostEqual(summary["net_return_pct"], (1.04 * .97 - 1) * 100)


class PaperJournalLoadingTests(TestCase):
    def test_missing_archive_and_missing_table_are_empty_and_never_created(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            path = Path(directory) / "data" / "market.sqlite3"
            self.assertEqual(load_paper_journal("SBER")["summary"]["total"], 0)
            self.assertFalse(path.exists())
            path.parent.mkdir()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE unrelated (id INTEGER)")
            self.assertEqual(load_paper_journal("SBER")["coverage"]["observations"], 0)

    def test_loader_reads_only_requested_ticker_and_leaves_archive_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            path = Path(directory) / "data" / "market.sqlite3"
            path.parent.mkdir()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE observations (ticker TEXT, at TEXT, payload TEXT)")
                for ticker in ("SBER", "GAZP"):
                    for at, payload in [row(0), row(30), row(60, bid=105, ask=105.1)]:
                        db.execute("INSERT INTO observations VALUES (?, ?, ?)", (ticker, at, json.dumps(payload)))
            original = path.read_bytes()
            result = load_paper_journal("SBER")
            self.assertEqual(result["summary"]["closed"], 1)
            self.assertEqual(result["coverage"]["observations"], 3)
            self.assertEqual(path.read_bytes(), original)

    def test_compact_archive_is_preferred_over_legacy_observations(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            path = Path(directory) / "data" / "market.sqlite3"
            path.parent.mkdir()
            with closing(sqlite3.connect(path)) as db, db:
                for table in ("observations", "paper_observations"):
                    db.execute(f"CREATE TABLE {table} (ticker TEXT, at TEXT, payload TEXT)")
                at, payload = row(0)
                db.execute("INSERT INTO observations VALUES (?, ?, ?)", ("SBER", at, json.dumps(payload)))
                at, payload = row(30, "WAIT")
                db.execute("INSERT INTO paper_observations VALUES (?, ?, ?)", ("SBER", at, json.dumps(payload)))
            result = load_paper_journal("SBER")
            self.assertEqual(result["summary"]["total"], 0)
            self.assertEqual(result["coverage"]["observations"], 1)
            self.assertEqual(result["coverage"]["last_at"], row(30)[0])
