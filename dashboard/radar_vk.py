"""Private VK delivery for legacy order-book radars, independent from Telegram."""
import hashlib
import queue
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from django.conf import settings
from . import vk_transport

NAMES = {'single': 'Одиночный радар', 'multi': 'Мульти-радар'}
PENDING = queue.Queue(maxsize=50)
THREAD_LOCK = threading.Lock()
THREAD = None


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(settings.BASE_DIR)/'data'/'radar-vk.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS config (
                    scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT 'Уведомления ВК выключены.');
                CREATE TABLE IF NOT EXISTS attempts (
                    scope TEXT NOT NULL, figi TEXT NOT NULL, at REAL NOT NULL,
                    digest TEXT NOT NULL, result TEXT NOT NULL, detail TEXT NOT NULL,
                    PRIMARY KEY(scope,figi));
                INSERT OR IGNORE INTO config(scope) VALUES('single'),('multi');
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self, scope):
        if scope not in NAMES:
            raise ValueError('Неизвестный радар.')
        with self.db() as db:
            row = db.execute('SELECT * FROM config WHERE scope=?', (scope,)).fetchone()
        return {'scope': scope, 'enabled': bool(row['enabled']),
                'configured': vk_transport.configured(), 'result': row['result'],
                'message': row['detail']}

    def set_enabled(self, scope, enabled):
        if scope not in NAMES or type(enabled) is not bool:
            raise ValueError('Укажи радар и положение галочки.')
        if enabled and not vk_transport.configured():
            raise ValueError('ВК не настроен: нужны VK_COMMUNITY_TOKEN и VK_USER_ID в .env и перезапуск сайта.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM config WHERE scope=?', (scope,)).fetchone()
            changed = bool(row['enabled']) != enabled
            revision = row['revision']+1
            if changed:
                # Save the transition before I/O. Repeated POSTs/restarts cannot send it again.
                db.execute('UPDATE config SET enabled=?,revision=?,result=?,detail=? WHERE scope=?',
                    (int(enabled), revision, 'unknown', 'Настройка сохранена. Подтверждение отправки ожидается.', scope))
        if changed:
            state = 'включены' if enabled else 'выключены'
            text = f'VORBY · {NAMES[scope]}\nУведомления ВК {state}.'
            if enabled:
                text += '\nСобытия придут после подключения биржевого потока и появления изменений в стакане. Это не сигнал покупки.'
            outcome = vk_transport.send(text)
            with self.db() as db:
                db.execute('UPDATE config SET result=?,detail=? WHERE scope=? AND revision=?',
                    (outcome['status'], outcome['message'], scope, revision))
        return self.status(scope)

    def observe(self, scope, figi, message, *, now=None, created=None):
        if scope not in NAMES or not vk_transport.configured():
            return 'disabled'
        now = time.time() if now is None else now
        digest = hashlib.sha256(message.encode('utf-8')).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cfg = db.execute('SELECT * FROM config WHERE scope=?', (scope,)).fetchone()
            if not cfg['enabled']:
                return 'disabled'
            if created is not None and not 0 <= time.time()-created <= 20:
                return 'stale'
            previous = db.execute('SELECT * FROM attempts WHERE scope=? AND figi=?', (scope, figi)).fetchone()
            if previous and (now-previous['at'] < 10 or previous['digest'] == digest):
                return 'suppressed'
            # Reserve before the API call, including ambiguous failures and process restarts.
            db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?,?)',
                (scope, figi, now, digest, 'unknown', 'Приём ВК ещё не подтверждён.'))
        text = message+'\n\nСобытие стакана, не рекомендация покупать. Заявка может быть снята.'
        outcome = vk_transport.send(text)
        with self.db() as db:
            db.execute('UPDATE attempts SET result=?,detail=? WHERE scope=? AND figi=? AND at=?',
                (outcome['status'], outcome['message'], scope, figi, now))
            db.execute('UPDATE config SET result=?,detail=? WHERE scope=? AND enabled=1 AND revision=?',
                (outcome['status'], outcome['message'], scope, cfg['revision']))
        return outcome['status']


def _deliver(job):
    scope, figi, message, created = job
    try:
        if not 0 <= time.time()-created <= 20:
            return 'stale'
        store = Store()
        if time.time()-created > 20:
            return 'stale'
        return store.observe(scope, figi, message, created=created)
    except Exception:
        # VK errors must not interrupt the market stream or Telegram delivery.
        return 'unavailable'


def _worker():
    while True:
        job = PENDING.get()
        try:
            _deliver(job)
        finally:
            PENDING.task_done()


def submit(scope, figi, message):
    """Bounded delivery queue: never wait for VK in the order-book stream."""
    global THREAD
    with THREAD_LOCK:
        if THREAD is None or not THREAD.is_alive():
            THREAD = threading.Thread(target=_worker, daemon=True, name='vk-radar-events')
            THREAD.start()
    try:
        PENDING.put_nowait((scope, figi, message, time.time()))
        return 'queued'
    except queue.Full:
        return 'busy'
