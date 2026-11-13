"""Opt-in six-rule LONG candidates. No orders, demo messages or model overrides."""
import html
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from . import telegram_transport as transport
from .watch_universe import normalize, AnalysisQueue, ANALYSIS_PARALLELISM, COLLECTION_BATCH, ROTATION_SECONDS

COOLDOWN = 1800
LEASE_SECONDS = 120
RULES = {'trend', 'flow', 'volume', 'vwap', 'spread', 'book'}
THREAD_LOCK = threading.Lock()
THREAD = None


def symbols(values):
    return normalize(values)


def collector_status():
    from .continuous_watch import load_health
    health = load_health(Path(settings.BASE_DIR)/'data'/'watch')
    return {'alive': bool(health.get('alive')), 'active_tickers': health.get('tickers', []) if health.get('alive') else [],
            'batch': health.get('batch'), 'batch_count': health.get('batch_count'),
            'watchlist_count': health.get('watchlist_count'), 'status': health.get('status'),
            'unsupported_tickers': health.get('unsupported_tickers', [])}


def fresh(value, now, seconds):
    try:
        at = datetime.fromisoformat(value)
        return at.tzinfo is not None and 0 <= now - at.timestamp() <= seconds
    except (TypeError, ValueError, OverflowError):
        return False


def checked_analysis(payload, ticker, now):
    """Only a fresh, fully verified core result can arm OR reset an episode."""
    if (payload.get('status') != 'ok' or payload.get('mode') != 'live' or payload.get('refreshing')
            or payload.get('instrument', {}).get('ticker') != ticker
            or payload.get('trading_status') != 'SECURITY_TRADING_STATUS_NORMAL_TRADING'):
        return None
    a = payload.get('analysis', {})
    q = a.get('data_quality', {})
    if (a.get('blockers') or q.get('action_blocked') is not False
            or q.get('version') != 'stream_exchange_v1' or q.get('book_consistency_confirmed') is not True
            or not fresh(a.get('updated_at'), now, 25) or not fresh(a.get('data_time'), now, 45)):
        return None
    rules = a.get('rules', [])
    if len(rules) != 6 or {r.get('key') for r in rules} != RULES:
        return None
    if any(type(r.get('pass')) is not bool for r in rules):
        return None
    return a


def buy_candidate(a):
    if a.get('decision') != 'LONG' or a.get('passed') != 6 or not all(r['pass'] for r in a['rules']):
        return False
    p = a.get('plan') or {}
    values = [p.get(k) for k in ('stop', 'entry', 'target')]
    return (all(type(v) in (float, int) and math.isfinite(v) and v > 0 for v in values)
            and values[0] < values[1] < values[2])


def message(ticker, a):
    p = a['plan']
    def price(value):
        return format(value, '.12g')  # Never round a positive sub-rouble quote down to zero.
    at = datetime.fromisoformat(a['data_time']).astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M:%S UTC')
    return (f'<b>VORBY · Кандидат покупки · {html.escape(ticker)}</b>\n'
            'Совпали 6 из 6 условий: тренд, активные покупки, объём, VWAP, узкий спред и устойчивый стакан.\n\n'
            f"Цена покупки по снимку: {price(p['entry'])} ₽\n"
            f"Стоп сценария: {price(p['stop'])} ₽\nЦель сценария: {price(p['target'])} ₽\n"
            f'Снимок: {at}\n\n'
            'Это сигнал правил, не рекомендация нейросети. Доходность не доказана. '
            'Цена уже могла измениться: проверь текущий стакан, шаг цены, комиссии и размер риска. '
            'Заявки автоматически не выставляются.')


class Store:
    transport = transport
    filename = 'telegram.sqlite3'
    setup_message = 'В .env нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID того же бота, что у радара.'

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(settings.BASE_DIR)/'data'/self.filename
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL,
                    tickers TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO config VALUES (1,0,'["SBER"]',0);
                CREATE TABLE IF NOT EXISTS episodes (ticker TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0,
                    last_sent REAL NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    checked REAL NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
                    delivery_detail TEXT NOT NULL DEFAULT '', event TEXT);
                CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY, owner TEXT, until REAL,
                    heartbeat REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '');
                INSERT OR IGNORE INTO worker(id,until) VALUES(1,0);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self):
        with self.db() as db:
            row = db.execute('SELECT * FROM config WHERE id=1').fetchone()
        return {'enabled': bool(row['enabled']), 'tickers': json.loads(row['tickers']), 'revision': row['revision']}

    def save(self, enabled, tickers):
        if type(enabled) is not bool:
            raise ValueError('Укажи, включены ли уведомления.')
        tickers = symbols(tickers)
        if enabled and not self.transport.configured():
            raise ValueError(self.setup_message)
        if enabled and not os.getenv('INVEST_TOKEN'):
            raise ValueError('Для живых сигналов нужен INVEST_TOKEN.')
        with self.db() as db:
            db.execute('UPDATE config SET enabled=?,tickers=?,revision=revision+1 WHERE id=1',
                       (int(enabled), json.dumps(tickers)))
        # Deliberately preserve previous episodes across toggles and restarts.

    def lease(self, owner, now):
        with self.db() as db:
            return bool(db.execute('UPDATE worker SET owner=?,until=?,heartbeat=?,message=? '
                'WHERE id=1 AND (owner=? OR until<=?)',
                (owner, now+LEASE_SECONDS, now, 'Проверяем живые данные.', owner, now)).rowcount)

    def heartbeat(self, owner, now, detail):
        with self.db() as db:
            db.execute('UPDATE worker SET heartbeat=?,until=?,message=? WHERE owner=?',
                       (now, now+LEASE_SECONDS, detail, owner))

    def release(self, owner):
        with self.db() as db:
            db.execute('UPDATE worker SET until=0 WHERE owner=?', (owner,))

    def status(self, now=None):
        now = time.time() if now is None else now
        result = self.config()
        selected = set(result['tickers'])
        with self.db() as db:
            worker = db.execute('SELECT * FROM worker WHERE id=1').fetchone()
            rows = db.execute('SELECT ticker,checked,last_sent,result,decision,detail,delivery_detail FROM episodes').fetchall()
        result.update(configured=self.transport.configured(),
                      worker_running=bool(result['enabled'] and worker['until'] > now
                                          and 0 <= now-worker['heartbeat'] <= 20),
                      heartbeat_at=worker['heartbeat'] or None, message=worker['message'],
                      cooldown_minutes=COOLDOWN//60,
                      watchlist_count=len(selected), analysis_parallelism=ANALYSIS_PARALLELISM,
                      collection_batch=COLLECTION_BATCH, rotation_minutes=ROTATION_SECONDS//60,
                      collector=collector_status(),
                      items=[dict(row) for row in rows if row['ticker'] in selected])
        return result

    def observe(self, ticker, payload, *, now=None, sender=None):
        now = time.time() if now is None else now
        sender = sender or self.transport.send
        cfg = self.config()
        if not cfg['enabled'] or ticker not in cfg['tickers']:
            return 'disabled'
        a = checked_analysis(payload, ticker, now)
        candidate = bool(a and buy_candidate(a))
        decision = a.get('decision') if a else None
        detail = ('Все шесть условий совпали.' if candidate else 'Условия покупки не совпали.') if a else (
            'Нет свежего подтверждения: проверь сессию, поток стакана и ленту. Учебные и непроверенные данные не отправляются.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # A settings update after the initial read must cancel this send.
            current = db.execute('SELECT * FROM config WHERE id=1').fetchone()
            if not current['enabled'] or ticker not in json.loads(current['tickers']):
                return 'disabled'
            db.execute('INSERT OR IGNORE INTO episodes(ticker) VALUES(?)', (ticker,))
            row = db.execute('SELECT * FROM episodes WHERE ticker=?', (ticker,)).fetchone()
            db.execute('UPDATE episodes SET checked=?,decision=?,detail=? WHERE ticker=?',
                       (now, decision or 'UNAVAILABLE', detail, ticker))
            # Gaps, demo, malformed LONG and partial data cannot re-arm an alert.
            reset = a and decision in ('WAIT', 'EXIT') and any(r['pass'] is False for r in a['rules'])
            if reset:
                db.execute('UPDATE episodes SET active=0 WHERE ticker=?', (ticker,))
                return 'waiting'
            if not candidate:
                return 'blocked'
            if row['active'] or now-row['last_sent'] < COOLDOWN or now < row['next_try']:
                return 'suppressed'
            # Record the attempt BEFORE the network call: crash/timeout must not duplicate it.
            event = uuid.uuid4().hex
            db.execute('UPDATE episodes SET active=1,last_sent=?,result=?,delivery_detail=?,event=? WHERE ticker=?',
                       (now, 'unknown', 'Отправка начата, подтверждение ещё не получено.', event, ticker))
        # The payload can expire while waiting for a SQLite lock.
        if checked_analysis(payload, ticker, time.time() if sender is self.transport.send else now) is None:
            result = {'status': 'rejected', 'retry_after': 60, 'message': 'Сигнал устарел до отправки.'}
        else:
            try:
                result = sender(message(ticker, a))
            except Exception:
                result = {'status': 'unknown', 'message': 'Доставка не подтверждена; повтор этого сигнала отключён.'}
        outcome = result.get('status')
        if outcome not in ('ok', 'rejected', 'unknown'):
            outcome = 'unknown'
        with self.db() as db:
            if outcome == 'rejected':
                db.execute('UPDATE episodes SET active=0,last_sent=?,next_try=? WHERE ticker=? AND event=?',
                           (row['last_sent'], now+max(60, result.get('retry_after', 60)), ticker, event))
            db.execute('UPDATE episodes SET result=?,delivery_detail=? WHERE ticker=? AND event=?',
                       (outcome, result.get('message', 'Доставка не подтверждена.'), ticker, event))
        return outcome


def worker_loop(stop=None, store=None):
    """One DB lease across runserver reloads / management-command processes."""
    stop = stop or threading.Event()
    store = store or Store()
    owner = uuid.uuid4().hex
    queue = AnalysisQueue()
    try:
        while not stop.is_set():
            try:
                cfg = store.config()
                if cfg['enabled'] and store.transport.configured() and store.lease(owner, time.time()):
                    from .signal_service import get_signal
                    active = set(collector_status()['active_tickers'])
                    selected = [ticker for ticker in cfg['tickers'] if ticker in active]
                    queue.step(selected, get_signal, store.observe, time.monotonic())
                    store.heartbeat(owner, time.time(),
                        f'В списке {len(cfg["tickers"])}. Сборщик наблюдает {len(selected)} из них. '
                        'Анализ очередью, до 3 акций одновременно; для большого списка обход дольше.')
                elif not cfg['enabled']:
                    store.release(owner)
            except Exception:
                # Fail closed without logging credentials from provider/network exceptions.
                try:
                    store.heartbeat(owner, time.time(), 'Ошибка фоновой проверки. Уведомления не подтверждены; проверь подключение и папку data.')
                except (OSError, sqlite3.Error):
                    pass
            stop.wait(5)
    finally:
        store.release(owner)


def ensure_worker():
    global THREAD
    with THREAD_LOCK:
        if THREAD is None or not THREAD.is_alive():
            THREAD = threading.Thread(target=worker_loop, daemon=True, name='telegram-candidates')
            THREAD.start()
