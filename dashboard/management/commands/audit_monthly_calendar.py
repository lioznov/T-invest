"""Supplementary evidence review; prices, trades, rankings and old reports stay fixed."""
import copy
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import monthly_momentum as model
from dashboard.management.commands.study_monthly_momentum import hashes
from dashboard.management.commands.study_ranges_full import load_input
from dashboard.management.commands.study_pattern_flow import write_immutable

SOURCE = 'https://www.moex.com/n95604'
CLOSED = tuple((date(2025, 12, 31)+timedelta(days=i)).isoformat() for i in range(5))


def review_result(result, prepared):
    reviewed = copy.deepcopy(result)
    remaining, resolved = [], []
    for incident in result['quality_incidents']:
        ticker, day = incident['ticker'], date.fromisoformat(incident['day'])
        data = prepared[ticker]
        i = data['by_date'].get(day)
        justified = False
        if incident['reason'] == 'gap_or_corporate_action' and i is not None and i > 0:
            previous, current = data['dates'][i-1], data['bars'][i]
            missing = [(previous+timedelta(days=k)).isoformat() for k in range(1, (day-previous).days)]
            justified = (bool(missing) and all(d in CLOSED for d in missing) and current.volume > 0
                         and .6 <= current.open/data['bars'][i-1].close <= 1.4)
        (resolved if justified else remaining).append(incident)
    for trade in reviewed['trade_log']:
        if (not trade['verified'] and any(i['ticker'] == trade['ticker'] and trade['entry_day'] <= i['day'] <= trade['exit_day'] for i in resolved)
                and not any(i['ticker'] == trade['ticker'] and trade['entry_day'] <= i['day'] <= trade['exit_day'] for i in remaining)):
            trade['verified'] = True
    reviewed['unknown_trades'] = sum(not t['verified'] for t in reviewed['trade_log'])
    reviewed['quality_incidents'] = remaining
    reviewed['calendar_resolved'] = resolved
    reviewed['verified'] = bool(result['curve']) and not remaining and not reviewed['unknown_trades']
    return reviewed


class Command(BaseCommand):
    help = 'Review only documented 2025/2026 New Year closure flags; never alter trade calculations.'

    def add_arguments(self, parser):
        parser.add_argument('--report', required=True)

    def handle(self, *args, **options):
        root = (Path(settings.BASE_DIR)/'data').resolve()
        path = (root/options['report']).resolve()
        if path.parent != root or not path.is_file() or path.stat().st_size > 20_000_000:
            raise CommandError('Specify one bounded local report')
        audit_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        spec = {'version': 'monthly_calendar_review_v1', 'source': SOURCE, 'closed_days': CLOSED,
                'report_file': path.name, 'report_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'source_sha256': audit_hash, 'rule': 'only documented closed dates; nonzero volume; no opening discontinuity; no change to trades or selection',
                'entry_allowed': False}
        registration = root/'monthly-calendar-review-protocol-v1.json'
        serial_spec = json.loads(json.dumps(spec))
        if registration.exists():
            if json.loads(registration.read_text(encoding='utf-8'))['spec'] != serial_spec:
                raise CommandError('Audit protocol differs')
        else:
            write_immutable(registration, {'at': datetime.now(timezone.utc).isoformat(), 'spec': serial_spec})
        original = json.loads(path.read_text(encoding='utf-8'))
        if original['version'] != model.VERSION or original['source_sha256'] != hashes():
            raise CommandError('Research source mismatch')
        prepared = {}
        for ticker in model.TICKERS:
            source = root/original['inputs'][ticker]['file']
            if source.resolve().parent != root or hashlib.sha256(source.read_bytes()).hexdigest() != original['inputs'][ticker]['sha256']:
                raise CommandError('Input mismatch')
            data = load_input(source, ticker)
            prepared[ticker] = model.prepare(data['candles'], data['lot'])
        reviewed = {k: {p: {s: review_result(r, prepared) for s, r in scenarios.items()}
                       for p, scenarios in periods.items()} for k, periods in original['results'].items()}
        bootstrap = model.bootstrap_family({k: v['full']['double_cost'] for k, v in reviewed.items()})
        summaries = []
        for rule in model.VARIANTS:
            periods = reviewed[rule.key]
            summaries.append({'key': rule.key, 'passes_screen': model.screen(periods),
                'periods': {p: {s: {k: v for k, v in r.items() if k not in ('curve', 'trade_log', 'decisions')} for s, r in scenarios.items()}
                            for p, scenarios in periods.items()}})
        at = datetime.now(timezone.utc)
        output = root/f'monthly-calendar-review-{at.strftime("%Y%m%d-%H%M%S-%f")}.json'
        write_immutable(output, {'version': spec['version'], 'at': at.isoformat(), 'protocol_sha256': hashlib.sha256(registration.read_bytes()).hexdigest(),
                                'original_report': path.name, 'original_sha256': spec['report_sha256'], 'source': SOURCE,
                                'source_sha256': audit_hash, 'summaries': summaries, 'bootstrap': bootstrap,
                                'trade_numbers_unchanged': True, 'selection_unchanged': True, 'entry_allowed': False,
                                'note': 'Resolved documented closure flags only; source candle accuracy, actual execution, dividends and survivor bias remain limitations.'})
        self.stdout.write(json.dumps({'bootstrap': bootstrap, 'passed': [r['key'] for r in summaries if r['passes_screen']],
                                     'resolved_full_base': sum(len(r['periods']['full']['base']['calendar_resolved']) for r in summaries),
                                     'report': str(output)}, ensure_ascii=True))
