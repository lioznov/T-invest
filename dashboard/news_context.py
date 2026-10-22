"""Point-in-time calendar snapshots and descriptive post-receipt price changes.

Never learns or changes a trading signal. Every broker call is read-only.
"""
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from hashlib import sha256

from .news_store import NewsStore
from .signals import Candle, MSK

TICKERS = ('SBER','GAZP','LKOH','NVTK','GMKN','ROSN','YDEX')
CALENDAR_URL = 'https://developer.tbank.ru/invest/services/instruments/methods'
_pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix='news-context')
_lock = threading.Lock()
_pending = {}


class ContextStore(NewsStore):
    def __init__(self,path=None):
        super().__init__(path)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS news_context_jobs (id TEXT PRIMARY KEY, next_at TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS news_calendar_snapshots (id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, observed_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS news_calendar_clock ON news_calendar_snapshots(ticker,observed_at);
                CREATE TABLE IF NOT EXISTS news_price_candles (ticker TEXT NOT NULL, at TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume INTEGER NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(ticker,at));
            ''')

    def job_claim(self,key,now,seconds):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM news_context_jobs WHERE id=?',(key,)).fetchone()
            if row and row['next_at'] > now.isoformat():
                return False
            db.execute('INSERT INTO news_context_jobs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET next_at=excluded.next_at,status=excluded.status,message=excluded.message',
                       (key,(now+timedelta(seconds=seconds)).isoformat(),'loading','Получаем официальные данные…'))
            return True

    def job_finish(self,key,now,status,message,seconds):
        with self.connect() as db:
            db.execute('UPDATE news_context_jobs SET next_at=?,status=?,message=? WHERE id=?',
                       ((now+timedelta(seconds=seconds)).isoformat(),status,message,key))

    def put_calendar(self,ticker,events,now,status='ok',message=''):
        with self.connect() as db:
            db.execute('INSERT INTO news_calendar_snapshots(ticker,observed_at,status,payload) VALUES(?,?,?,?)',
                       (ticker,now.isoformat(),status,json.dumps({'events':events,'message':message},ensure_ascii=False)))

    def calendar(self,now,ticker=''):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM news_calendar_snapshots s WHERE observed_at<=? AND id=(SELECT MAX(id) FROM news_calendar_snapshots x WHERE x.ticker=s.ticker AND x.observed_at<=?)',
                              (now.isoformat(),now.isoformat())).fetchall()
            jobs = {r['id']:dict(r) for r in db.execute('SELECT * FROM news_context_jobs WHERE id LIKE "calendar:%"')}
        events,coverage = [],[]
        for row in rows:
            if ticker and row['ticker'] != ticker:
                continue
            data = json.loads(row['payload'])
            stale = now-datetime.fromisoformat(row['observed_at']) > timedelta(days=1)
            coverage.append({'ticker':row['ticker'],'status':row['status'],'observed_at':row['observed_at'],'stale':stale,'message':data['message']})
            if row['status']=='unavailable':
                with self.connect() as db:
                    saved = db.execute("SELECT * FROM news_calendar_snapshots WHERE ticker=? AND observed_at<=? AND status IN ('ok','partial') ORDER BY id DESC LIMIT 1",
                                       (row['ticker'],now.isoformat())).fetchone()
                if saved:
                    row = saved
                    data = json.loads(row['payload'])
                    stale = True  # last refresh failed, even if the saved dates are recent
            events.extend({**e,'observed_at':row['observed_at'],'stale':stale} for e in data['events'] if e['date'] >= now.astimezone(MSK).date().isoformat())
        return {'events':sorted(events,key=lambda e:(e['date'],e['ticker'],e['type']))[:80],
                'coverage':coverage,'supported_tickers':TICKERS,'jobs':jobs,'entry_allowed':False,
                'source':'Т-Банк Invest API','source_url':CALENDAR_URL,
                'note':'Даты предоставлены API, могут измениться. Пустой ответ не гарантирует отсутствие событий. Время внутри дня не выдумывается.'}

    def put_prices(self,ticker,candles,now):
        for c in candles:
            if (c.time.tzinfo is None or c.time.second or c.time.microsecond or c.volume < 0
                    or not all(math.isfinite(p) and p>0 for p in (c.open,c.high,c.low,c.close))
                    or not c.low <= min(c.open,c.close) <= max(c.open,c.close) <= c.high):
                raise ValueError('Invalid exchange candles')
        with self.connect() as db:
            for c in candles:
                at = c.time.astimezone(timezone.utc).isoformat()
                if c.time+timedelta(minutes=1)>now:
                    continue
                old = db.execute('SELECT open,high,low,close,volume FROM news_price_candles WHERE ticker=? AND at=?',(ticker,at)).fetchone()
                values = (c.open,c.high,c.low,c.close,c.volume)
                if old and tuple(old) != values:
                    raise ValueError('Exchange history correction: do not silently overwrite archived quotes')
                db.execute('INSERT OR IGNORE INTO news_price_candles VALUES(?,?,?,?,?,?,?,?)',
                           (ticker,at,*values,now.isoformat()))

    def prices(self,ticker):
        with self.connect() as db:
            return [Candle(datetime.fromisoformat(r['at']),r['open'],r['high'],r['low'],r['close'],r['volume'])
                    for r in db.execute('SELECT * FROM news_price_candles WHERE ticker=? ORDER BY at',(ticker,))]

    def original(self,item_id):
        with self.connect() as db:
            row = db.execute('SELECT i.*,r.title,r.observed_at FROM news_items i JOIN news_revisions r ON r.item_id=i.id '
                             'WHERE i.id=? ORDER BY r.revision LIMIT 1',(item_id,)).fetchone()
            return dict(row) if row else None


def event(ticker,kind,at,title,observed,**extra):
    if not isinstance(at,datetime) or at.tzinfo is None or at.year < 2000:
        return None
    day = at.astimezone(MSK).date().isoformat()
    return {'id':sha256(f'{ticker}:{kind}:{day}'.encode()).hexdigest()[:24],
            'ticker':ticker,'type':kind,'date':day,'title':title,'precision':'day',
            'source_url':CALENDAR_URL,'known_at':observed.isoformat(),**extra}


def fetch_calendar(ticker,store=None,now=None):
    if ticker not in TICKERS:
        raise ValueError('Unsupported calendar ticker')
    store = store or ContextStore()
    now = now or datetime.now(timezone.utc)
    key = 'calendar:'+ticker
    if not store.job_claim(key,now,180):
        return
    from .market_provider import open_client,error_message,number
    from t_tech.invest.schemas import GetAssetReportsRequest
    events,errors = [],[]
    start = now.astimezone(MSK).replace(hour=0,minute=0,second=0,microsecond=0)
    try:
        with open_client() as client:
            try:
                dividends = client.instruments.get_dividends(instrument_id=ticker+'_TQBR',from_=start-timedelta(days=90),to=now+timedelta(days=90)).dividends
                for dividend in dividends:
                    for field,kind,title in (('last_buy_date','last_buy','Последний день покупки под дивиденды'),('record_date','record','Закрытие дивидендного реестра'),('payment_date','payment','Выплата дивидендов')):
                        row = event(ticker,kind,getattr(dividend,field,None),title,now,
                                    amount=number(dividend.dividend_net),currency=dividend.dividend_net.currency)
                        if row: events.append(row)
            except Exception as exc:
                errors.append(error_message(exc))
            try:
                reports = client.instruments.get_asset_reports(GetAssetReportsRequest(instrument_id=ticker+'_TQBR',from_=start,to=now+timedelta(days=90))).events
                for report in reports:
                    row = event(ticker,'report',report.report_date,f'Публикация отчётности · {report.period_year}, период {report.period_num}',now)
                    if row: events.append(row)
            except Exception as exc:
                errors.append(error_message(exc))
    except Exception as exc:
        errors.append(error_message(exc))
    done = datetime.now(timezone.utc)
    # Errors do not replace a previously successful calendar with a fake empty
    # success. Coverage and partial data remain visibly qualified.
    status = 'partial' if errors and events else 'unavailable' if errors else 'ok'
    store.put_calendar(ticker,events,done,status,' '.join(dict.fromkeys(errors)))
    store.job_finish(key,done,status,' '.join(dict.fromkeys(errors)),600 if errors else 21600)
    return {'ticker':ticker,'events':len(events),'status':status}


def fetch_prices(ticker,store=None,now=None):
    if ticker not in TICKERS:
        raise ValueError('Unsupported quote ticker')
    store = store or ContextStore()
    now = now or datetime.now(timezone.utc)
    key = 'prices:'+ticker
    if not store.job_claim(key,now,180):
        return
    from .market_provider import open_client,error_message
    from .market_stream_provider import exchange_candles
    from t_tech.invest.schemas import CandleInterval
    try:
        with open_client() as client:
            candles = exchange_candles(client,ticker+'_TQBR',now-timedelta(days=3),now,CandleInterval.CANDLE_INTERVAL_1_MIN,1)
        if not candles:
            raise ValueError('No verified exchange candles')
        store.put_prices(ticker,candles,datetime.now(timezone.utc))
        store.job_finish(key,datetime.now(timezone.utc),'ok','Биржевые минутные свечи сохранены.',900)
        return {'ticker':ticker,'candles':len(candles),'status':'ok'}
    except Exception as exc:
        store.job_finish(key,datetime.now(timezone.utc),'unavailable',error_message(exc),600)
        return {'ticker':ticker,'status':'unavailable'}


def schedule(kind,tickers):
    def work():
        for ticker in tickers:
            (fetch_calendar if kind=='calendar' else fetch_prices)(ticker)
    key = (kind,tuple(tickers))
    with _lock:
        for old in list(_pending):
            if _pending[old].done(): del _pending[old]
        if key not in _pending and len(_pending)<4:
            _pending[key] = _pool.submit(work)


def reaction(candles,observed,now):
    """Descriptive OPEN-to-OPEN changes, not simulated orders or causal effects."""
    anchor = observed.replace(second=0,microsecond=0)
    if anchor < observed: anchor += timedelta(minutes=1)
    quotes = {c.time:c for c in candles if c.time+timedelta(minutes=1)<=now and c.volume>0}
    first = quotes.get(anchor)
    result = []
    for minutes,label in ((15,'15 минут'),(60,'1 час'),(1440,'24 часа')):
        until = anchor+timedelta(minutes=minutes)
        status = 'pending' if until+timedelta(minutes=1)>now else 'unavailable'
        last = quotes.get(until)
        continuous = minutes==1440 or all(anchor+timedelta(minutes=j) in quotes for j in range(minutes+1))
        row = {'label':label,'minutes':minutes,'status':status,'change_pct':None,'after_assumed_cost_pct':None}
        if first and last and continuous:
            row.update(status='ok',change_pct=(last.open/first.open-1)*100,
                       after_assumed_cost_pct=(last.open*.9995*.9995/(first.open*1.0005*1.0005)-1)*100)
        result.append(row)
    return {'anchor':anchor.isoformat(),'horizons':result,'entry_allowed':False,
            'note':'Цена открытия первой полной минуты после получения → цена открытия через указанный срок. Нет интерполяции. 24 часа могут включать ночь. Это не доказательство влияния новости и не исполненные сделки. Расходы — допущение 0,05% комиссии + 0,05% проскальзывания с каждой стороны.'}


def reaction_payload(item_id,ticker,now=None,refresh=True):
    now = now or datetime.now(timezone.utc)
    store = ContextStore()
    original = store.original(item_id)
    if original is None:
        return {'status':'not_found','entry_allowed':False}
    observed = datetime.fromisoformat(original['first_seen_at'])
    if refresh: schedule('prices',[ticker])
    result = reaction(store.prices(ticker),observed,now)
    # This is explicitly CURRENT context. It is not inserted into a past
    # headline cohort as though the historical book were known.
    from .signal_service import get_signal
    live = get_signal('live',ticker) if refresh else {}
    analysis = live.get('analysis',{})
    book_time = analysis.get('data_time')
    fresh = False
    try:
        fresh = bool(book_time and 0<=(now-datetime.fromisoformat(book_time)).total_seconds()<=45)
    except (ValueError,TypeError): pass
    quality = analysis.get('data_quality',{})
    current = {'status':'unverified','message':'Свежесть и источник текущего стакана не подтверждены. Это не стакан на момент новости.'}
    if live.get('mode')=='live' and fresh and quality.get('action_blocked') is False:
        current = {'status':'ok','spread_bps':analysis.get('spread_bps'),'buy_share':analysis.get('buy_share'),
                   'at':book_time,'message':'Текущий стакан, не исторический. Сам по себе не подтверждает влияние новости.'}
    return {'status':'ok','ticker':ticker,'original_title':original['title'],'observed_at':observed.isoformat(),
            'reaction':result,'current_market':current,'entry_allowed':False}
