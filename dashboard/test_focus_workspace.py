"""Keep existing analysis targets and forms reachable after the navigation redesign."""
from collections import Counter
from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class WorkspaceMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.depth = 0
        self.active = []
        self.owners = {}
        self.ids = []
        self.tabs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'section':
            self.depth += 1
            if 'data-focus-pane' in attrs:
                self.active.append((self.depth, attrs['data-focus-pane']))
        if 'id' in attrs:
            self.ids.append(attrs['id'])
            self.owners[attrs['id']] = self.active[-1][1] if self.active else None
        if attrs.get('role') == 'tab':
            self.tabs.append(attrs)

    def handle_endtag(self, tag):
        if tag == 'section':
            if self.active and self.active[-1][0] == self.depth:
                self.active.pop()
            self.depth -= 1


class FocusWorkspaceTests(SimpleTestCase):
    def test_single_stock_keeps_every_primary_tool_in_its_intended_section(self):
        page = WorkspaceMarkup(render_to_string('signals.html', {'watchlist': False}))
        expected = {
            'now': ['price-chart', 'decision', 'next-step', 'trade-plan', 'stock-news-items', 'research-helper'],
            'market': ['rules', 'orderbook', 'technical-result', 'structure-status', 'pattern-items'],
            'research': ['ai-result', 'neural-result', 'morning-forecast', 'close-timing-research',
                         'multiyear-research', 'temporal-validation', 'research-form', 'paper-result', 'forward-result'],
            'settings': ['telegram-form', 'risk-form', 'use-plan', 'monitor-health'],
        }
        for pane, ids in expected.items():
            for element in ids:
                with self.subTest(element=element):self.assertEqual(page.owners.get(element), pane)
        self.assertFalse([name for name, count in Counter(page.ids).items() if count > 1])

    def test_watchlist_is_the_default_view_and_does_not_receive_single_stock_forms(self):
        page = WorkspaceMarkup(render_to_string('signals.html', {'watchlist': True}))
        self.assertEqual(page.owners['watch-form'], 'now')
        self.assertEqual(page.owners['watch-grid'], 'now')
        self.assertEqual(page.owners['telegram-form'], 'settings')
        self.assertNotIn('risk-form', page.ids)
        self.assertNotIn('tab-market', page.ids)
        self.assertEqual([t['data-focus-tab'] for t in page.tabs], ['now', 'research', 'settings'])

    def test_tabs_have_unique_targets_and_summary_is_selected_on_first_load(self):
        page = WorkspaceMarkup(render_to_string('signals.html', {'watchlist': False}))
        self.assertEqual([t['data-focus-tab'] for t in page.tabs], ['now', 'market', 'research', 'settings'])
        self.assertEqual([t['id'] for t in page.tabs if t['aria-selected'] == 'true'], ['tab-now'])
        for tab in page.tabs:
            self.assertEqual(page.ids.count(tab['aria-controls']), 1)
