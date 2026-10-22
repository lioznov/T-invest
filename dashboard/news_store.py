"""Local metadata archive with cross-process fetch leases and title revisions.

No full texts/images, no broker calls, no signal-model mutation. Observation
timestamps are separate from publisher timestamps to avoid future-data leakage.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from django.conf import settings

from .news_sources import SOURCES, INTERVAL
from .news_feed import FeedError, fetch_feed, classify, safe_url

_pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix='vorby-news')
_lock = threading.Lock()
_future = None


class NewsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(settings.BASE_DIR)/'data'/'news.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS news_sources (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, next_at TEXT NOT NULL,
                    lease TEXT, lease_until TEXT);
                CREATE TABLE IF NOT EXISTS news_items (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    latest_revision INTEGER);
                CREATE TABLE IF NOT EXISTS news_revisions (
                    revision INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
                    title TEXT NOT NULL, published_at TEXT, date_status TEXT NOT NULL,
                    observed_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES news_items(id) ON DELETE CASCADE);
                CREATE INDEX IF NOT EXISTS news_observed ON news_revisions(observed_at);
                CREATE INDEX IF NOT EXISTS news_seen ON news_items(first_seen_at);
            ''')
            if 'author' not in {r['name'] for r in db.execute('PRAGMA table_info(news_revisions)')}:
                db.execute("ALTER TABLE news_revisions ADD COLUMN author TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path,timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, source, now):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM news_sources WHERE id=?',(source.key,)).fetchone()
            at = now.isoformat()
            if row and (row['next_at'] > at or row['lease_until'] and row['lease_until'] > at):
                return None
            state = json.loads(row['state']) if row else {}
            token = uuid4().hex
            db.execute('INSERT INTO news_sources(id,state,next_at,lease,lease_until) VALUES(?,?,?,?,?) '
                       'ON CONFLICT(id) DO UPDATE SET lease=excluded.lease,lease_until=excluded.lease_until',
                       (source.key,json.dumps(state),at,token,(now+timedelta(seconds=180)).isoformat()))
            return token,state

    def finish(self, source, token, state, items, now, interval):
        at = now.isoformat()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('SELECT lease FROM news_sources WHERE id=?',(source.key,)).fetchone()
            if not owner or owner['lease'] != token:
                return False
            for item in items or []:
                db.execute('INSERT INTO news_items(id,source,url,first_seen_at,last_seen_at) VALUES(?,?,?,?,?) '
                           'ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at',
                           (item['id'],source.key,item['url'],at,at))
                fingerprint = sha256(json.dumps([item['title'],item['published_at'],item['date_status'],item.get('author','')],
                                               ensure_ascii=False).encode()).hexdigest()
                current = db.execute('SELECT r.revision,r.fingerprint FROM news_items i '
                                     'LEFT JOIN news_revisions r ON r.revision=i.latest_revision WHERE i.id=?',
                                     (item['id'],)).fetchone()
                if current and current['fingerprint'] == fingerprint:
                    revision = current['revision']
                else:
                    revision = db.execute('INSERT INTO news_revisions(item_id,title,published_at,date_status,observed_at,fingerprint,author) '
                        'VALUES(?,?,?,?,?,?,?)',(item['id'],item['title'],item['published_at'],item['date_status'],at,fingerprint,item.get('author',''))).lastrowid
                db.execute('UPDATE news_items SET latest_revision=? WHERE id=?',(revision,item['id']))
            # Metadata-only, bounded local retention. Corrections preserve their
            # own observation times; they never replace an earlier revision.
            db.execute('DELETE FROM news_items WHERE first_seen_at<?',((now-timedelta(days=90)).isoformat(),))
            db.execute('DELETE FROM news_items WHERE id IN (SELECT id FROM news_items '
                       'ORDER BY first_seen_at DESC LIMIT -1 OFFSET 10000)')
            db.execute('UPDATE news_sources SET state=?,next_at=?,lease=NULL,lease_until=NULL WHERE id=?',
                       (json.dumps(state,ensure_ascii=False),(now+timedelta(seconds=max(INTERVAL,interval))).isoformat(),source.key))
            return True

    def source_states(self):
        with self.connect() as db:
            return {r['id']:{**json.loads(r['state']),'next_at':r['next_at'],
                             'fetching':bool(r['lease'])} for r in db.execute('SELECT * FROM news_sources')}

    def items(self, now, *, ticker='', source='', topic='', query='', include_macro=False, market_only=True, limit=80):
        with self.connect() as db:
            records = db.execute('SELECT i.*,r.title,r.published_at,r.date_status,r.observed_at,r.author '
                'FROM news_items i JOIN news_revisions r ON r.revision=(SELECT MAX(v.revision) FROM news_revisions v WHERE v.item_id=i.id AND v.observed_at<=?) '
                'WHERE i.first_seen_at<=? ORDER BY COALESCE(r.published_at,i.first_seen_at) DESC,i.id LIMIT 2000',
                (now.isoformat(),now.isoformat())).fetchall()
        result = []
        for record in records:
            item = dict(record)
            publisher = SOURCES.get(item['source'])
            if not publisher or not publisher.enabled or not safe_url(item['url'],publisher):
                continue
            tags = classify(item['title'])
            if market_only and not publisher.official and not tags['tickers'] and tags['topics'] == ['other']:
                continue
            if source and item['source'] != source or topic and topic not in tags['topics']:
                continue
            if ticker and ticker not in tags['tickers']:
                if not include_macro or not set(tags['topics']) & {'rates','sanctions','macro'}:
                    continue
            if query and query.casefold() not in item['title'].casefold():
                continue
            at = item['published_at'] or item['first_seen_at']
            age = (now-datetime.fromisoformat(at)).total_seconds()
            item.update(tags)
            item.update(source_name=publisher.name, source_home=publisher.home, official=publisher.official,
                        license_url=publisher.license_url,
                        stale=age > 86400, date_unknown=item['published_at'] is None,
                        duplicate_key=sha256(item['title'].casefold().encode()).hexdigest()[:16])
            item.pop('latest_revision',None)
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def as_known(self, at):
        """Historical metadata available BY this observation time, not latest titles."""
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT i.id,i.source,i.url,i.first_seen_at,r.* '
                'FROM news_items i JOIN news_revisions r ON r.item_id=i.id '
                'WHERE r.observed_at<=? AND r.revision=(SELECT MAX(v.revision) FROM news_revisions v '
                'WHERE v.item_id=i.id AND v.observed_at<=?)',(at.isoformat(),at.isoformat()))]


def collect_once(store=None, sources=None, now=None):
    store = store or NewsStore()
    results = []
    for source in sources or SOURCES.values():
        started = now or datetime.now(timezone.utc)
        claim = store.claim(source,started)
        if claim is None:
            continue
        token,cached = claim
        try:
            items,metadata,interval = fetch_feed(source,cached,started)
            observed = now or datetime.now(timezone.utc)  # after receipt, never a publisher/backfilled timestamp
            state = {**cached,**metadata,'last_attempt':observed.isoformat(),
                     'last_success':observed.isoformat(),'status':'ok',
                     'message':'RSS проверен; сохранены только заголовки и ссылки.',
                     'last_count':len(items) if items is not None else cached.get('last_count',0)}
            if metadata.get('deferred'):
                state.update(status='waiting',last_success=cached.get('last_success'),
                             message='robots.txt проверен. RSS будет загружен после разрешённого интервала.')
        except FeedError as exc:
            observed = now or datetime.now(timezone.utc)
            items,interval = None,exc.retry
            state = {**cached,'last_attempt':observed.isoformat(),'status':exc.status,'message':str(exc)}
        except Exception:
            observed = now or datetime.now(timezone.utc)
            items,interval = None,600
            state = {**cached,'last_attempt':observed.isoformat(),'status':'error',
                     'message':'Источник не обработан. Старые новости сохранены, проверка будет повторена позже.'}
        store.finish(source,token,state,items,observed,interval)
        results.append({'source':source.key,'status':state['status'],'items':len(items or [])})
    return results


def schedule():
    global _future
    with _lock:
        if _future is None or _future.done():
            _future = _pool.submit(collect_once)


def payload(*, now=None, refresh=True, **filters):
    now = now or datetime.now(timezone.utc)
    store = NewsStore()
    if refresh:
        schedule()
    states = store.source_states()
    sources = []
    for source in SOURCES.values():
        state = states.get(source.key,{})
        # robots.txt itself is not a copyright licence; display the separate
        # manual review and expiry, regardless of network/cache success.
        public_state = {k:v for k,v in state.items() if k not in ('robots_text','etag','last_modified','robots_checked','robots_delay','deferred')}
        if not source.available(now):
            public_state.update(status='policy_blocked',message=source.basis if not source.enabled
                                else 'Истёк срок проверки правил. Источник приостановлен до повторной проверки.')
            if source.key=='disclosure':
                public_state.update(status='requires_access')
        sources.append({**source.public(),**public_state})
    from .news_analysis import group_events,TONES
    tone,kind = filters.pop('tone',''),filters.pop('kind','')
    items = store.items(now,limit=160,**filters)
    grouped = group_events(items)
    totals = {key:sum(g['semantics']['tone']==key for g in grouped) for key in TONES}
    if tone: grouped = [g for g in grouped if g['semantics']['tone']==tone]
    if kind: grouped = [g for g in grouped if g['semantics']['kind']==kind]
    for item in grouped:
        item['editorial_note']='Заголовок источника; группировка и смысловые метки добавлены Vorby Pro.'
    count = sum(s.get('status') == 'ok' for s in sources)
    active = sum(s.enabled for s in SOURCES.values())
    return {'status':'ok' if count == active else 'partial' if count or items else 'loading' if not states and refresh else 'unavailable',
            'items':grouped[:80],'sources':sources,'now':now.isoformat(),'entry_allowed':False,
            'summary':{'publications':len(items),'events':len(grouped),'tone_counts':totals,'available_sources':count,'configured_sources':active},
            'note':'Только заголовки RSS, не полные статьи. Теги определяются по словам, могут ошибаться. '
                   'Новость не является командой покупки. Старые модели не обучены на этом архиве.'}
