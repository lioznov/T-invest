from datetime import date, timedelta
from types import SimpleNamespace as NS
from unittest.mock import patch

from django.test import Client, SimpleTestCase

from .auto_morning import BAR, clock, estimate, next_morning, samples
from .market_provider import MarketUnavailable
from .signals import Candle


def market_day(day, open=True):
    return NS(date=clock(day, 0), is_trading_day=open, start_time=clock(day, 9, 10),
              end_time=clock(day, 18, 55), clearing_start_time=None, clearing_end_time=None)


def schedule(*days):
    return NS(exchanges=[NS(exchange="MOEX", days=days)])


class MorningCalendarTests(SimpleTestCase):
    def test_skips_weekend_and_holiday(self):
        friday = date(2026, 11, 27)
        monday_holiday = schedule(*(market_day(friday+timedelta(days=i), i == 4) for i in range(1, 5)))
        self.assertEqual(next_morning(monday_holiday, friday), clock(date(2026, 12, 1), 10))

    def test_does_not_cross_gap_in_schedule_or_missing_ten_oclock_bar(self):
        friday = date(2026, 11, 27)
        with self.assertRaises(MarketUnavailable):
            next_morning(schedule(market_day(friday+timedelta(days=3))), friday)
        late = market_day(friday+timedelta(days=1))
        late.start_time = clock(late.date.date(), 11)
        with self.assertRaises(MarketUnavailable):
            next_morning(schedule(late, market_day(friday+timedelta(days=2))), friday)


class MorningEstimateTests(SimpleTestCase):
    @staticmethod
    def series():
        bars = []
        first = date(2025, 1, 1)
        for i in range(190):
            day = first+timedelta(days=i)
            if day.weekday() > 4:
                continue
            for j in range(4):
                p = 100+i*.1+j*.02
                bars.append(Candle(clock(day, 10)+j*BAR,p,p+.1,p-.1,p+.01,100+i+j))
        return bars, bars[-1].time+BAR

    def test_tests_use_later_dates_and_prediction_is_not_anchor(self):
        bars, cutoff = self.series()
        target = clock(cutoff.date()+timedelta(days=1),10)
        result = estimate(bars, cutoff, target)
        self.assertEqual(result["target_at"], target.isoformat())
        self.assertGreaterEqual(result["quality"]["test"], 20)
        self.assertLess(date.fromisoformat(result["quality"]["to"]), target.date())
        self.assertNotEqual(result["predicted_price"], result["anchor_price"])

    def test_invalid_and_missing_data_have_no_price(self):
        bars, cutoff = self.series()
        with self.assertRaises(ValueError):
            samples(bars, (11,0), cutoff-BAR)
        with self.assertRaises(MarketUnavailable):
            estimate(bars[-32:], cutoff, cutoff+timedelta(days=1))


class MorningEndpointTests(SimpleTestCase):
    def test_post_requires_csrf_and_get_never_starts_a_job(self):
        client = Client(enforce_csrf_checks=True)
        with patch("dashboard.auto_morning_views.pool.submit") as submit:
            response = client.get("/api/analysis/morning/?ticker=PLZL")
            self.assertEqual(response.json()["status"], "idle")
            response = client.post("/api/analysis/morning/", {"ticker":"PLZL"}, content_type="application/json")
            self.assertEqual(response.status_code,403)
            submit.assert_not_called()

    def test_valid_ticker_runs_for_plzl(self):
        from .auto_morning_views import active
        self.addCleanup(active.clear)
        with patch("dashboard.auto_morning_views.pool.submit") as submit:
            url = "/api/analysis/morning/"
            response = self.client.post(url, {"ticker":"PLZL"}, content_type="application/json")
            self.assertEqual(response.status_code,202)
            self.assertEqual(response.json()["stage"], "loading")
            self.assertEqual(self.client.get(url+"?ticker=PLZL").json()["status"], "running")
            self.client.post(url, {"ticker":"PLZL"}, content_type="application/json")
            submit.assert_called_once()

    def test_connection_failure_cannot_return_a_previous_forecast(self):
        from django.core.cache import cache
        from django.utils import timezone
        from .auto_morning_views import run
        self.addCleanup(cache.delete,"auto_morning_v1:PLZL")
        cache.set("auto_morning_v1:PLZL", {"status":"ok","forecast":{"predicted_price":100}})
        with patch("dashboard.auto_morning_views.calculate", side_effect=MarketUnavailable("Нет связи")):
            run("PLZL", timezone.now())
        self.assertEqual(self.client.get("/api/analysis/morning/?ticker=PLZL").json(),
                         {"ticker":"PLZL","status":"error","message":"Нет связи"})
