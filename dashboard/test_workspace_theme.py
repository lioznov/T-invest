"""Template integration checks for the shared high-contrast visual shell."""
from pathlib import Path
from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class WorkspaceThemeTests(SimpleTestCase):
    def test_all_six_sections_share_the_same_stylesheet(self):
        cases=[('signals.html',{'watchlist':False}),('signals.html',{'watchlist':True}),
               ('news.html',{}),('real_market.html',{'page_id':'terminal'}),
               ('radar.html',{'page_id':'radar'}),('portfolio.html',{'page_id':'portfolio'})]
        for template,context in cases:
            with self.subTest(template=template,context=context):
                html=render_to_string(template,context)
                self.assertIn('css/workspace.css?v=5',html)
                self.assertIn('Основная навигация',html)
                self.assertEqual(html.count('class="nav-icon"'),6)

    def test_active_destination_is_announced(self):
        html=render_to_string('_app_sidebar.html',{'active_page':'news'})
        self.assertEqual(html.count('aria-current="page"'),1)
        self.assertIn('class="nav-link active" aria-current="page"',html)
        self.assertIn('aria-hidden="true"',html)

    def test_news_dom_contract_and_cautions_survive_redesign(self):
        html=render_to_string('news.html',{})
        for element in ('news-filters','news-items','news-tone','news-kind','news-ticker','news-calendar','news-sources','news-positive','news-negative'):
            self.assertIn(f'id="{element}"',html)
        self.assertIn('Метка смысла, не прогноз роста',html)
        self.assertIn('не «ничего не повлияет»',html)

    def test_shared_palette_does_not_reintroduce_muted_blue_primary(self):
        css=(Path(settings.BASE_DIR)/'static/css/workspace.css').read_text(encoding='utf-8')
        self.assertIn('--primary:#ffdd2d;',css)
        self.assertNotIn('--primary:#8ca5ff;',css)
        self.assertIn('prefers-reduced-motion:reduce',css)
        self.assertIn('repeat(6,minmax(0,1fr))',css)

    def test_unknown_portfolio_values_remain_unknown(self):
        html=render_to_string('portfolio.html',{'page_id':'portfolio'})
        self.assertIn('id="totalCost">—</div>',html)
        self.assertIn('id="totalYield">—</div>',html)
        self.assertIn('таблица с горизонтальной прокруткой',html)

    def test_chart_helper_loads_before_signal_script(self):
        html=render_to_string('signals.html',{'watchlist':False})
        self.assertLess(html.index('js/chart_ui.js'),html.index('js/signals.js'))

    def test_radar_has_wrapping_control_container(self):
        html=render_to_string('radar.html',{'page_id':'radar'})
        self.assertIn('class="radar-controls"',html)
        self.assertIn('controls-bar radar-toolbar',html)

    def test_empty_portfolio_is_compact_from_first_render(self):
        html=render_to_string('portfolio.html',{'page_id':'portfolio'})
        self.assertIn('chart-card is-empty',html)
        self.assertIn('class="chart-wrapper" hidden',html)
        self.assertLess(html.index('id="portfolioStatus"'),html.index('id="portfolio-overview"'))
