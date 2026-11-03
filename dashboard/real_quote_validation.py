"""Fresh T-Bank quote verification of one previously excluded historical day.

Three separate actions: prepare (research Python), fetch (project/SDK Python),
score (research Python). Historical evaluation, never orders or prospective proof.
"""
import argparse
import hashlib
import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median

from . import morning_model as m
from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import overnight_ensemble_study as ensemble
from . import overnight_decision_metrics as metrics
from .signals import Candle

ROOT = storage.ROOT
VERSION = 'real_quote_validation_v1'
ORIGIN = '2026-10-22T17:45:00+03:00'
SOURCE = 'CANDLE_SOURCE_EXCHANGE'
INPUT_SHA = '990ccf99e5cd378da2505b7309b68115ffdf5201cb93f4c088de57f9416b897b'
PRIOR_SHA = '2b9c00af9768dbe5719e38728150596028be4e93e5c7763ed8c2fc5ec7d5d097'
ROOT_DATA = ROOT/'data'/'real-quotes-validation-v1'
RULES = {
    'day': ORIGIN, 'entry': '2026-10-22T18:00:00+03:00', 'exit': '2026-10-23T10:00:00+03:00',
    'reason': 'this origin was excluded from all old annual tests because its label_end exceeded old endboundary',
    'prediction': 'latest2026 frozen net_ridge/net_boost/peer_boost; x/x_peer only; no fit; median primary',
    'secondary': 'also publish equalmean andunanimity unchanged, no search or thresholdchanges',
    'selection': 'median/mean net>.10percent; unanimous ALL3net>.10percent',
    'source': 'fresh read-only T-Bank SDK GetCandles EXCHANGE15m; all7existing stocks; exact times, complete positivevolume bars',
    'audit': 'cached17:45anchor andknown18entry must match fresh quotes; no silent correction or synthetic fallback',
    'unknowns': 'missing exact18entry/next10exit remains unknown; selectedunknown poisons account',
    'economics': '100000RUB,max25percent/stock,wholelots;fee/slip.05percent each per side;stressx2same selection',
    'timing': 'forecast file created now BEFORE new API outcomes fetched; retrospective origin, NOT a forecast recordedbeforeOct23',
    'limits': 'one historical date/sevenstocks, no prospectiveproof/dividendcash/tax/realexecution/overnightworstdrawdown',
    'entry_allowed': False,
}


def source_hashes():
    return {**ensemble.source_hashes(), 'real_quote_validation.py': hashlib.sha256(
        (ROOT/'dashboard/real_quote_validation.py').read_bytes()).hexdigest()}


def freeze():
    spec = {'version': VERSION, 'rules': RULES, 'input_sha256': INPUT_SHA,
            'prior_sha256': PRIOR_SHA, 'source_sha256': source_hashes()}
    path = ROOT_DATA/'protocol.json'
    if path.exists():
        if storage.checked(path)['spec'] != spec:
            raise ValueError('Frozen real-quote verification changed; create another version')
    else:
        storage.immutable(path, storage.signed({'registered_at': datetime.now(timezone.utc).isoformat(), 'spec': spec}))
    return m.digest(spec)


def registered():
    value = storage.checked(ROOT_DATA/'protocol.json')
    if value['spec']['source_sha256'] != source_hashes():
        raise ValueError('Research sources changed')
    return m.digest(value['spec'])


def actions(values):
    if len(values) != 3 or not all(type(v) in (float, int) and math.isfinite(v) for v in values):
        raise ValueError('Three finite saved forecasts required')
    a, b = mean(values), median(values)
    return {'mean_net_pct': a, 'median_net_pct': b, 'selected_mean': a > .1,
            'selected_median': b > .1, 'selected_unanimous': min(values) > .1}


def prepare():
    import importlib.metadata
    import joblib
    from threadpoolctl import threadpool_limits
    for name, version in storage.PACKAGES.items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f'Requires research dependency {name}=={version}')
    protocol = freeze()
    path = ROOT_DATA/'forecast.json'
    if path.exists():
        old = storage.checked(path)
        if old['protocol_sha256'] != protocol:
            raise ValueError('Forecast protocol mismatch')
        print('Verified existing historical forecast file'); return path
    data = storage.checked(ROOT/'data/nonlinear-morning-inputs-v1.json')
    prior = storage.checked(ROOT/'dashboard/ai_models'/ensemble.PRIOR)
    if (data['artifact_sha256'] != INPUT_SHA or prior['artifact_sha256'] != PRIOR_SHA
            or prior['source_sha256'] != decision.source_hashes()
            or data['source_sha256'] != storage.source_hashes()):
        raise ValueError('Prior source/data chain mismatch')
    original = [r for r in data['rows'] if r['at'] == ORIGIN]
    if (len(original) != 7 or {r['ticker'] for r in original} != set(m.TICKERS)
            or any(r['actual_price'] is not None or r['y'] is not None for r in original)):
        raise ValueError('Expected seven cached features with previously unknown morning outcomes')
    fold = next(f for f in prior['folds'] if f['id'] == 'test_2026')
    if any(p['at'] == ORIGIN for v in fold['variants'] for p in v['predictions']):
        raise ValueError('This day already participated in the old test')
    # The model receives feature vectors exclusively, including no cached future entry.
    redacted = [{**r, 'entry_price': None} for r in original]
    rows = decision.enrich_rows(redacted)
    feature_only = [{'x': r['x'], 'x_peer': r['x_peer']} for r in rows]
    members, weights = [], []
    with threadpool_limits(limits=2):
        for v in fold['variants']:
            path_weights = (ROOT/v['weights_local_file']).resolve()
            if (not path_weights.is_relative_to((ROOT/'data').resolve())
                    or hashlib.sha256(path_weights.read_bytes()).hexdigest() != v['weights_sha256']):
                raise ValueError('Own local weights mismatch')
            bundle = joblib.load(path_weights)
            prediction, _ = decision.predict(bundle, feature_only)
            members.append(prediction)
            weights.append({'model': v['id'], 'sha256': v['weights_sha256']})
    records = []
    for i, (r, original_row) in enumerate(zip(rows, original)):
        meta = storage.checked(ROOT/'data/multiyear'/r['ticker']/'metadata.json')
        values = [p[i] for p in members]
        records.append({'base_row': r, 'instrument': meta['instrument'], 'metadata_sha256': meta['artifact_sha256'],
            'cached_entry_for_audit_only': original_row['entry_price'], 'member_predictions': values, **actions(values)})
    storage.immutable(path, storage.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'historical': True, 'prospective': False, 'entry_allowed': False, 'as_of': ORIGIN,
        'protocol_sha256': protocol, 'input_sha256': INPUT_SHA, 'model_artifact_sha256': PRIOR_SHA,
        'weights': weights, 'records': records, 'baseline': {k: fold['variants'][0][k] for k in (
            'train_net_mean', 'train_net_median', 'train_profit_prior', 'calibration_profit_prior')}}))
    print('Saved retrospective predictions before requesting fresh prices:', path)
    return path


def price_points(records, origin):
    at = datetime.fromisoformat(origin)
    start, end = at-timedelta(minutes=15), m.clock(at.date()+timedelta(days=1), 10, 15)
    candles = []
    for b in records:
        t = datetime.fromisoformat(b['time'])
        if (b['source'] != SOURCE or b['is_complete'] is not True
                or not start <= t or t+timedelta(minutes=15) > end):
            raise ValueError('Unconfirmed, incomplete or out-of-window exchange candle')
        candles.append(Candle(t, *(b[k] for k in ('open','high','low','close')), b['volume']))
    m.validate(candles)
    by_time = {c.time: c for c in candles if c.volume > 0}
    anchor = by_time.get(start)
    entry = by_time.get(m.clock(at.date(), 18))
    target = by_time.get(m.clock(at.date()+timedelta(days=1), 10))
    return {'anchor_price': anchor.close if anchor else None, 'entry_price': entry.open if entry else None,
            'actual_price': target.open if target else None}


def audit_points(record, prices):
    if (prices['anchor_price'] is None or not math.isclose(prices['anchor_price'],
            record['base_row']['anchor_price'], abs_tol=1e-9, rel_tol=1e-10)):
        raise ValueError('Fresh anchor differs from cached feature source; refusing to score')
    cached = record['cached_entry_for_audit_only']
    if cached is not None and prices['entry_price'] is not None and not math.isclose(
            cached, prices['entry_price'], abs_tol=1e-9, rel_tol=1e-10):
        raise ValueError('Fresh entry differs from cache; refusing a silent history revision')


def fetch():
    # This branch alone requires the project Python, Django settings and SDK token.
    from django.conf import settings
    _ = settings.BASE_DIR  # Load configured dotenv before opening the SDK client.
    from .market_provider import open_client, resolve
    from .market_stream_provider import exchange_candles
    from .multiyear_inputs import _rpc, RATE_LIMITER
    from t_tech.invest.schemas import CandleInterval
    protocol = registered()
    forecast = storage.checked(ROOT_DATA/'forecast.json')
    if forecast['protocol_sha256'] != protocol:
        raise ValueError('Forecast registration mismatch')
    path = ROOT_DATA/'quotes.json'
    if path.exists():
        old = storage.checked(path)
        if old['forecast_sha256'] != forecast['artifact_sha256']:
            raise ValueError('Quotes are from another forecast')
        print('Verified existing fresh quote snapshot'); return path
    at = datetime.fromisoformat(ORIGIN)
    start = (at-timedelta(minutes=15)).astimezone(timezone.utc)
    end = m.clock(at.date()+timedelta(days=1), 10, 15).astimezone(timezone.utc)
    results = []
    with open_client() as client:
        for record in forecast['records']:
            expected = record['instrument']
            current = _rpc(lambda: resolve(client, expected['ticker']), RATE_LIMITER)
            if any(current[k] != expected[k] for k in ('ticker','uid','lot','currency')):
                raise ValueError('Instrument identity/lot changed')
            bars = _rpc(lambda: exchange_candles(client, current['uid'], start, end,
                       CandleInterval.CANDLE_INTERVAL_15_MIN, 15), RATE_LIMITER)
            raw = [{**asdict(b), 'time': b.time.isoformat(), 'is_complete': True, 'source': SOURCE} for b in bars]
            points = price_points(raw, ORIGIN); audit_points(record, points)
            results.append({'instrument': current, 'fetched_at': datetime.now(timezone.utc).isoformat(),
                            'bars': raw, 'price_points': points})
            print(current['ticker'], 'verified EXCHANGE candles:', len(raw), flush=True)
    storage.immutable(path, storage.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'source': 'T-Bank SDK GetCandles', 'candle_source': SOURCE, 'interval': '15m', 'entry_allowed': False,
        'protocol_sha256': protocol, 'forecast_sha256': forecast['artifact_sha256'],
        'request': {'from': start.isoformat(), 'to_exclusive': end.isoformat()}, 'stocks': results}))
    print('Saved fresh real exchange prices:', path)
    return path


def score():
    protocol = registered()
    forecast = storage.checked(ROOT_DATA/'forecast.json')
    quotes = storage.checked(ROOT_DATA/'quotes.json')
    if (forecast['protocol_sha256'] != protocol or quotes['protocol_sha256'] != protocol
            or quotes['forecast_sha256'] != forecast['artifact_sha256']
            or datetime.fromisoformat(forecast['created_at']) >= datetime.fromisoformat(quotes['created_at'])):
        raise ValueError('Quote/forecast provenance or ordering mismatch')
    by_ticker = {s['instrument']['ticker']: s for s in quotes['stocks']}
    if len(by_ticker) != 7 or set(by_ticker) != set(m.TICKERS):
        raise ValueError('Expected all seven instruments, including missing outcomes')
    rows, comparisons = [], []
    for record in forecast['records']:
        base = record['base_row']; fresh = by_ticker[base['ticker']]
        if any(fresh['instrument'][k] != record['instrument'][k] for k in ('ticker','uid','lot','currency')):
            raise ValueError('Quote instrument differs')
        prices = price_points(fresh['bars'], ORIGIN); audit_points(record, prices)
        if prices != fresh['price_points'] or actions(record['member_predictions']) != {
                k: record[k] for k in actions(record['member_predictions'])}:
            raise ValueError('Price/selection receipt mismatch')
        y = (prices['actual_price']/base['anchor_price']-1)*100 if prices['actual_price'] is not None else None
        rows.append({**base, **prices, 'y': y})
    rows = decision.enrich_rows(rows)
    for row, record in zip(rows, forecast['records']):
        comparisons.append({'ticker': row['ticker'], 'entry_price': row['entry_price'], 'morning_price': row['actual_price'],
            'predicted_net_pct': record['median_net_pct'], 'actual_net_pct': row['net_y'],
            'selected': record['selected_median']})
    baseline = forecast['baseline']
    report = metrics.evaluate(rows, [r['median_net_pct'] for r in forecast['records']], None,
        baseline['train_net_mean'], baseline['train_profit_prior'], baseline['calibration_profit_prior'],
        train_net_median=baseline['train_net_median'])
    policies = {}
    for name in ('median','mean','unanimous'):
        mask = [float(r['selected_'+name]) for r in forecast['records']]
        policies[name] = {'paper': metrics.paper_predictions(rows, mask, threshold=.5),
                          'stress': metrics.paper_predictions(rows, mask, multiplier=2, threshold=.5)}
    path = ROOT/'dashboard/ai_models'/('real-quotes-validation-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    storage.immutable(path, storage.signed({'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'historical': True, 'prospective': False, 'entry_allowed': False, 'rules': RULES,
        'protocol_sha256': protocol, 'source_sha256': source_hashes(), 'forecast_sha256': forecast['artifact_sha256'],
        'quotes_sha256': quotes['artifact_sha256'], 'comparisons': comparisons, 'report': report, 'policies': policies}))
    print('Saved', path.name, flush=True)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare','fetch','score'))
    option = parser.parse_args().action
    try:
        globals()[option]()
    except Exception as exc:
        if option == 'fetch':
            from .market_provider import error_message
            raise SystemExit(error_message(exc)) from None
        raise
