"""Synthetic causality/cost tests, not market performance evidence."""
from datetime import timedelta
from unittest import TestCase
from .joint_study import evaluate, summarize, matched, replay
from .pressure_study import prepare
from .microstructure_archive import stamp
from .test_stream_analysis import row, NOW


def candle(i, *, delay=1, close=100., high=100.1):
    start = NOW+timedelta(minutes=i)
    return {"id": i+1, "ticker": "SBER", "kind": "candle", "segment": "segment",
            "received_at": stamp(start+timedelta(minutes=1, seconds=delay)),
            "exchange_at": stamp(start), "causal_eligible": 1,
            "payload": {"interval": "SUBSCRIPTION_INTERVAL_ONE_MINUTE", "source": "CANDLE_SOURCE_EXCHANGE",
                        "open": 100., "high": high, "low": 99.9, "close": close, "volume": 100, "lot_size": 10}}


def fixture(count=650):
    market = [r | {"segment": "segment"} for i in range(count) for r in (row(i, "trade"), row(i))]
    extras = [row(i, "info") | {"segment": "segment"} for i in (0, 300, 600)]
    extras += [candle(i, close=100.2 if i == 5 else 100., high=100.3 if i == 5 else 100.1) for i in range(10)]
    return market, extras


class JointStudyTests(TestCase):
    def test_three_variants_share_one_chart_candidate(self):
        rows, extras = fixture()
        result = evaluate(rows, extras, "SBER")
        self.assertEqual(len(result["candidates"]), 1)
        for values in result["outcomes"].values():
            self.assertEqual(len(values), 1)
            self.assertEqual(values[0]["status"], "verified_quote_replay")
            self.assertLess(values[0]["return_pct"], 0)
            self.assertLess(values[0]["stress_return_pct"], values[0]["return_pct"])
            self.assertGreaterEqual(values[0]["signal_at"], stamp(NOW+timedelta(seconds=362)).replace(".000000", ""))

    def test_future_chart_cannot_trigger_early(self):
        rows, extras = fixture()
        result = evaluate([r for r in rows if r["received_at"] < stamp(NOW+timedelta(seconds=360))], extras, "SBER")
        self.assertFalse(result["candidates"])

    def test_missing_minute_and_unclosed_candle_do_not_trigger(self):
        rows, extras = fixture()
        for altered in ([r for r in extras if r.get("exchange_at") != stamp(NOW+timedelta(minutes=2))],
                        [r | {"received_at": stamp(NOW+timedelta(minutes=5, seconds=30))} if r["kind"] == "candle" and r["id"] == 6 else r for r in extras]):
            self.assertFalse(evaluate(rows, altered, "SBER")["candidates"])

    def test_future_buying_cannot_confirm_past_breakout(self):
        rows, extras = fixture()
        rows = [r for r in rows if r["kind"] != "trade" or r["received_at"] > stamp(NOW+timedelta(seconds=370))]
        result = evaluate(rows, extras, "SBER")
        self.assertEqual(len(result["outcomes"]["chart"]), 1)
        self.assertFalse(result["outcomes"]["chart_flow"])

    def test_weak_book_rejects_only_book_filter(self):
        rows, extras = fixture()
        for r in rows:
            if r["kind"] == "book":
                r["payload"]["bids"][0][1] = 10
                r["payload"]["asks"][0][1] = 90
        result = evaluate(rows, extras, "SBER")
        self.assertTrue(result["outcomes"]["chart_flow"])
        self.assertFalse(result["outcomes"]["chart_flow_book"])
        self.assertEqual(matched(result["candidates"], "chart_flow_book")["rejected"]["verified"], 1)

    def test_unknown_status_and_invalid_trade_block_candidates(self):
        rows, extras = fixture()
        self.assertFalse(evaluate(rows, [r for r in extras if r["kind"] != "info"], "SBER")["candidates"])
        next(r for r in rows if r["kind"] == "trade" and r["id"] == 361)["payload"]["direction"] = "UNKNOWN"
        self.assertFalse(evaluate(rows, extras, "SBER")["candidates"])

    def test_future_gap_is_unknown_not_removed(self):
        rows, extras = fixture()
        rows = [r for r in rows if not stamp(NOW+timedelta(seconds=400)) <= r["received_at"] <= stamp(NOW+timedelta(seconds=410))]
        outcome = evaluate(rows, extras, "SBER")["outcomes"]["chart"][0]
        self.assertEqual(outcome["status"], "unverified")
        self.assertNotIn("return_pct", outcome)

    def test_halt_during_holding_is_unknown(self):
        rows, extras = fixture()
        halt = row(450, "info") | {"segment": "segment"}
        halt["payload"]["market_orders_available"] = False
        extras.append(halt)
        self.assertEqual(evaluate(rows, extras, "SBER")["outcomes"]["chart"][0]["reason"], "trading_status_unknown_or_halted")

    def test_reconnect_cannot_complete_earlier_trade(self):
        rows, extras = fixture()
        rows = [r | {"segment": "second"} if r["received_at"] >= stamp(NOW+timedelta(seconds=400)) else r for r in rows]
        extras = [r | {"segment": "second"} if r["received_at"] >= stamp(NOW+timedelta(seconds=400)) else r for r in extras]
        self.assertEqual(evaluate(rows, extras, "SBER")["outcomes"]["chart"][0]["reason"], "segment_ended")

    def test_signal_book_exchange_clock_also_checked(self):
        signal = prepare(row(0))
        future = prepare(row(1))
        future["exchange_at"] = signal["exchange_at"]
        self.assertEqual(replay(NOW, [future], [], signal)["status"], "unverified")

    def test_no_scenarios_is_not_zero_profit_and_no_approval(self):
        result = summarize([])
        self.assertIsNone(result["mean_trade_return_pct"])
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["sufficient_sample"])

    def test_six_positive_candles_required_no_repainting_by_duplicate(self):
        rows, extras = fixture()
        # A delayed conflicting update cannot restore the old pattern.
        altered = candle(5, delay=3, close=99.99, high=100.3)
        extras.append(altered)
        result = evaluate(rows, extras, "SBER")
        self.assertEqual(len(result["candidates"]), 1)
