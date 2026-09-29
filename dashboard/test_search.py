from unittest.mock import patch

from django.test import SimpleTestCase

from . import views


INSTRUMENTS = [
    {'figi': 'preferred', 'ticker': 'SBERP', 'name': 'Сбербанк привилегированная', 'type': 'share', 'class_code': 'TQBR'},
    {'figi': 'fund', 'ticker': 'SBERF', 'name': 'Фонд Сбер', 'type': 'etf', 'class_code': 'TQTF'},
    {'figi': 'ordinary', 'ticker': 'SBER', 'name': 'Сбербанк', 'type': 'share', 'class_code': 'TQBR'},
    {'figi': 'name-only', 'ticker': 'ABCD', 'name': 'Sber related', 'type': 'share', 'class_code': 'TQBR'},
]


class InstrumentSearchTests(SimpleTestCase):
    def test_prefix_results_precede_name_matches_and_shares_filter_works(self):
        with patch.object(views, 'INVEST_TOKEN', 'test-token'), patch.object(views, 'CACHE_READY', True), patch.object(views, 'CACHED_INSTRUMENTS', INSTRUMENTS):
            result = self.client.get('/api/search/?q=sbe').json()['results']
            self.assertEqual([item['ticker'] for item in result[:3]], ['SBER', 'SBERP', 'SBERF'])
            shares = self.client.get('/api/search/?q=sbe&kind=share').json()['results']
            self.assertEqual([item['ticker'] for item in shares], ['SBER', 'SBERP', 'ABCD'])

    def test_legacy_pages_have_shared_sidebar_and_current_link(self):
        for path, label in (('/terminal/', 'Стакан'), ('/radar/', 'Радар'), ('/portfolio/', 'Портфель')):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'class="sidebar"')
            self.assertContains(response, f'Рабочее пространство <span>/</span> {label}')
            self.assertNotContains(response, 'class="nav-links"')
