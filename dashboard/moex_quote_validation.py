"""Separate public-MOEX validation of the existing retrospective forecast.

No authentication, fitting, order submission, changed forecast or TLS bypass.
Actions: prepare (freeze), fetch (verified HTTPS), score (saved prices).
"""
import argparse
import hashlib
import json
import math
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from . import real_quote_validation as real
from . import nonlinear_study as storage
from . import overnight_decision_study as decision
from . import overnight_decision_metrics as metrics

ROOT = storage.ROOT
DATA = ROOT/'data/moex-real-quotes-v1'
FORECAST_SHA = '26e2e2908a5eb594ba67002b1b73c70bc59b544a3f4520478c9daddb2cb93fcc'
VERSION = 'moex_real_quotes_v1'
BASE = 'https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/'
RULES = {
    'source': 'public MOEX ISS HTTPS TQBR; 1minute exchange candles; no authentication or TLS bypass',
    'period': '2026-10-22 through2026-10-23 inclusive; exact anchorOct22 17:44close, entry18:00open, exitOct23 10:00open',
    'timestamps': 'ISS naive begin/end interpreted as Europe/Moscow UTC+3; minute begins ongrid; end within one minute and before retrieval',
    'pagination': 'start+=actualreturnedcount until cursorTOTAL or empty page; max12pages perstock, .5secgap,20sectimeout',
    'prices': 'positivevolume finiteOHLC only; never nextavailable or dealer/synthetic substitutes',
    'audit': 'freshanchor andknownentry must match cached feature prices under frozen audit_points; mismatch blocks ALL model scoring',
    'lots': 'fresh MOEX TQBR LOTSIZE used for new paperaccount, not proof of historical lot; differences disclosed',
    'models': 'existing fixed7stock historical forecast file, no fits, median primary; mean/unanimous secondary unchanged',
    'economics': '100000RUBcash,max25percent perstock,wholelots;commission/slippage.05percent each per side;stressx2same signals',
    'unknowns': 'missing exactentry/exit stays unknown; selectedunknown poisons account; missinganchor blocks source audit',
    'limits': 'retrospective not prospective; one date, no dividends/taxes/realexecution or guaranteed profit',
    'entry_allowed': False,
}


def source_hashes():
    return {**real.source_hashes(), 'moex_quote_validation.py': hashlib.sha256(
        (ROOT/'dashboard/moex_quote_validation.py').read_bytes()).hexdigest()}


def freeze():
    spec = {'version': VERSION, 'rules': RULES, 'forecast_sha256': FORECAST_SHA,
            'source_sha256': source_hashes()}
    path = DATA/'protocol.json'
    if path.exists():
        if storage.checked(path)['spec'] != spec:
            raise ValueError('Frozen MOEX verification changed; create another version')
    else:
        storage.immutable(path, storage.signed({'registered_at': datetime.now(timezone.utc).isoformat(), 'spec': spec}))
    return real.m.digest(spec)


def forecast():
    value = storage.checked(real.ROOT_DATA/'forecast.json')
    if (value['artifact_sha256'] != FORECAST_SHA or value['protocol_sha256'] != real.registered()
            or value['as_of'] != real.ORIGIN or value['historical'] is not True
            or value['prospective'] is not False or value['entry_allowed'] is not False
            or len(value['records']) != 7
            or {r['instrument']['ticker'] for r in value['records']} != set(real.m.TICKERS)):
        raise ValueError('Registered retrospective forecast differs')
    return value


def table(payload, name):
    section = payload.get(name)
    if not isinstance(section, dict) or not isinstance(section.get('columns'), list) or not isinstance(section.get('data'), list):
        raise ValueError('Missing ISS table '+name)
    columns = section['columns']
    if len(columns) != len(set(columns)) or not all(isinstance(k, str) for k in columns):
        raise ValueError('Invalid ISS columns')
    rows = []
    for values in section['data']:
        if not isinstance(values, list) or len(values) != len(columns):
            raise ValueError('ISS row does not match columns')
        rows.append(dict(zip(columns, values)))
    return rows


def lot_metadata(payload, ticker):
    rows = table(payload, 'securities')
    if len(rows) != 1 or rows[0].get('SECID') != ticker or rows[0].get('CURRENCYID') not in ('SUR','RUB'):
        raise ValueError('MOEX stock/currency metadata mismatch')
    lot = rows[0].get('LOTSIZE')
    if type(lot) is not int or lot <= 0:
        raise ValueError('MOEX lot must be a positive integer')
    return {'ticker': ticker, 'board': 'TQBR', 'currency': 'rub', 'lot': lot}


def _time(value):
    if not isinstance(value, str):
        raise ValueError('ISS candle time must be text')
    at = datetime.fromisoformat(value)
    if at.tzinfo is not None:
        raise ValueError('Expected ISS Moscow wall-clock timestamp')
    return at.replace(tzinfo=real.m.MSK)


def price_points(pages, fetched_at):
    fetched = datetime.fromisoformat(fetched_at)
    if fetched.tzinfo is None:
        raise ValueError('Retrieval timestamp must be aware')
    by_time, previous = {}, None
    for page in pages:
        for row in table(page, 'candles'):
            begin, end = _time(row.get('begin')), _time(row.get('end'))
            if (begin.date().isoformat() not in ('2026-10-22','2026-10-23') or begin.second or begin.microsecond
                    or not begin < end <= begin+timedelta(minutes=1) or end >= fetched
                    or previous is not None and begin <= previous):
                raise ValueError('Unclosed, duplicate, unordered or out-of-period ISS candle')
            prices = [row.get(k) for k in ('open','high','low','close')]
            volume = row.get('volume')
            if (not all(type(p) in (int,float) and math.isfinite(p) and p > 0 for p in prices)
                    or type(volume) not in (int,float) or not math.isfinite(volume) or volume < 0
                    or not prices[2] <= min(prices[0],prices[3]) <= max(prices[0],prices[3]) <= prices[1]):
                raise ValueError('Invalid ISS OHLC/volume')
            if volume > 0:
                by_time[begin] = row
            previous = begin
    origin = datetime.fromisoformat(real.ORIGIN)
    anchor = by_time.get(origin-timedelta(minutes=1))
    entry = by_time.get(real.m.clock(origin.date(),18))
    exit_ = by_time.get(real.m.clock(origin.date()+timedelta(days=1),10))
    return {'anchor_price': anchor['close'] if anchor else None,
            'entry_price': entry['open'] if entry else None, 'actual_price': exit_['open'] if exit_ else None}


def cursor_finished(page, start, count):
    if count == 0:
        return True
    if 'candles.cursor' not in page:
        return False
    rows = table(page, 'candles.cursor')
    if len(rows) != 1 or type(rows[0].get('TOTAL')) is not int or rows[0]['TOTAL'] < 0:
        raise ValueError('Invalid pagination cursor')
    return start+count >= rows[0]['TOTAL']


def get_json(url):
    if urlparse(url).scheme != 'https' or urlparse(url).hostname != 'iss.moex.com':
        raise ValueError('Unexpected quote host')
    request = Request(url, headers={'Accept':'application/json', 'User-Agent':'Vorby-Pro-Research/1.0'})
    with urlopen(request, timeout=20) as response:
        final = urlparse(response.geturl())
        if final.scheme != 'https' or final.hostname != 'iss.moex.com':
            raise ValueError('Unexpected quote redirect')
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000:
        raise ValueError('ISS response exceeds bound')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('ISS response must be an object')
    return value


def url_for(ticker, candles=False, start=0):
    if ticker not in real.m.TICKERS:
        raise ValueError('Unregistered ticker')
    if candles:
        return BASE+ticker+'/candles.json?'+urlencode({'iss.meta':'off','from':'2026-10-22',
            'till':'2026-10-23','interval':1,'start':start})
    return BASE+ticker+'.json?'+urlencode({'iss.meta':'off','iss.only':'securities',
        'securities.columns':'SECID,LOTSIZE,CURRENCYID'})


def fetch():
    protocol = freeze()  # Before any metadata or prices.
    saved_forecast = forecast()
    for record in saved_forecast['records']:
        ticker = record['instrument']['ticker']; path = DATA/(ticker+'.json')
        if path.exists():
            receipt = storage.checked(path)
            if receipt['protocol_sha256'] != protocol or receipt['forecast_sha256'] != FORECAST_SHA or receipt['ticker'] != ticker:
                raise ValueError('Cached ISS receipt mismatch')
            print(ticker, 'verified existing ISS snapshot', flush=True); continue
        time.sleep(.5)
        meta_url = url_for(ticker); meta = get_json(meta_url); lot_metadata(meta,ticker)
        pages, requests, start = [], [], 0
        for _ in range(12):
            time.sleep(.5)
            request_url = url_for(ticker, True, start)
            value = get_json(request_url); count = len(table(value,'candles'))
            pages.append(value); requests.append(request_url)
            if cursor_finished(value,start,count):
                break
            start += count
        else:
            raise ValueError('ISS pagination did not finish within 12 requests')
        fetched_at = datetime.now(timezone.utc).isoformat()
        points = price_points(pages,fetched_at)
        try:
            real.audit_points(record,points); audit = {'status':'matched','message':None}
        except ValueError as exc:
            audit = {'status':'mismatch','message':str(exc)}
        storage.immutable(path, storage.signed({'version': VERSION, 'created_at': fetched_at,
            'protocol_sha256': protocol, 'forecast_sha256':FORECAST_SHA, 'ticker':ticker,
            'metadata_url':meta_url,'metadata_response':meta,'request_urls':requests,'pages':pages,
            'price_points':points,'price_audit':audit,'entry_allowed':False}))
        print(ticker, 'ISS pages',len(pages),'bars',sum(len(table(p,'candles')) for p in pages),'audit',audit['status'],flush=True)


def score():
    protocol = freeze(); prediction = forecast()
    rows, comparisons, sources, blocked = [], [], {}, []
    for record in prediction['records']:
        ticker = record['instrument']['ticker']; receipt = storage.checked(DATA/(ticker+'.json'))
        if receipt['protocol_sha256'] != protocol or receipt['forecast_sha256'] != FORECAST_SHA or receipt['ticker'] != ticker:
            raise ValueError('ISS source identity mismatch')
        metadata = lot_metadata(receipt['metadata_response'],ticker)
        prices = price_points(receipt['pages'],receipt['created_at'])
        if prices != receipt['price_points']:
            raise ValueError('ISS receipt prices differ')
        try:
            real.audit_points(record,prices)
        except ValueError as exc:
            blocked.append({'ticker':ticker,'reason':str(exc)})
        sources[ticker] = receipt['artifact_sha256']
        base = record['base_row']; anchor = prices['anchor_price']
        y = (prices['actual_price']/anchor-1)*100 if prices['actual_price'] is not None and anchor is not None else None
        rows.append({**base, **prices, 'lot':metadata['lot'], 'y':y})
        comparisons.append({'ticker':ticker, **prices, 'cached_anchor':base['anchor_price'],
            'cached_entry':record['cached_entry_for_audit_only'], 'cached_lot':base['lot'], 'moex_lot':metadata['lot'],
            'lot_changed':base['lot'] != metadata['lot'], 'predicted_net_pct':record['median_net_pct'],
            'selected':record['selected_median']})
    policies = report = None
    if not blocked:
        rows = decision.enrich_rows(rows)
        for row, comparison in zip(rows,comparisons):
            comparison['actual_net_pct'] = row['net_y']
        b = prediction['baseline']
        report = metrics.evaluate(rows,[r['median_net_pct'] for r in prediction['records']],None,
            b['train_net_mean'],b['train_profit_prior'],b['calibration_profit_prior'],train_net_median=b['train_net_median'])
        policies = {}
        for name in ('median','mean','unanimous'):
            mask = [float(r['selected_'+name]) for r in prediction['records']]
            policies[name] = {'paper':metrics.paper_predictions(rows,mask,threshold=.5),
                'stress':metrics.paper_predictions(rows,mask,threshold=.5,multiplier=2)}
    result = {'version':VERSION,'created_at':datetime.now(timezone.utc).isoformat(),'rules':RULES,
        'historical':True,'prospective':False,'entry_allowed':False,'status':'blocked' if blocked else 'ok',
        'protocol_sha256':protocol,'source_sha256':source_hashes(),'forecast_sha256':FORECAST_SHA,
        'quote_receipts':sources,'blockers':blocked,'comparisons':comparisons,'report':report,'policies':policies}
    path = ROOT/'dashboard/ai_models'/('moex-real-quotes-v1-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.json')
    storage.immutable(path, storage.signed(result)); print('Saved',path.name, 'status',result['status'],flush=True)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','fetch','score'))
    action = parser.parse_args().action
    try:
        freeze() if action == 'prepare' else globals()[action]()
    except (HTTPError, URLError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
        # No raw proxy/transport detail or credentials are logged.
        raise SystemExit('MOEX ISS HTTPS unavailable: '+type(exc).__name__) from None
