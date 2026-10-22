"""Bounded HTTPS RSS/Atom reader; never requests an article or bypasses a block."""
import re
import ssl
import time
import os
import base64
import json
import subprocess
from pathlib import Path
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from urllib.robotparser import RobotFileParser

import certifi
from defusedxml.ElementTree import fromstring

from .news_sources import USER_AGENT, ROBOT_AGENT

MAX_BYTES = 1_500_000
MAX_ITEMS = 150
TOPICS = {'dividends':'Дивиденды', 'results':'Отчётность', 'rates':'Ставка и инфляция',
          'sanctions':'Санкции и ограничения', 'trading':'Торги и листинг',
          'commodities':'Нефть, газ и металлы', 'macro':'Экономика', 'other':'Другие новости'}
VERSION = 'headline_tags_v1'
ALIASES = {
    'SBER': ('sber','сбер','сбера','сбербанк'), 'GAZP': ('gazp','газпром'),
    'LKOH': ('lkoh','лукойл'), 'NVTK': ('nvtk','новатэк'), 'GMKN': ('gmkn','норникел','норильск'),
    'ROSN': ('rosn','роснефт'), 'YDEX': ('ydex','яндекс'), 'VTBR': ('vtbr','втб'),
    'TATN': ('tatn','татнефт'), 'CHMF': ('chmf','северстал'), 'PHOR': ('phor','фосагро'),
    'MTSS': ('mtss','мтс'), 'NLMK': ('nlmk','нлмк'), 'MAGN': ('magn','ммк'),
    'PLZL': ('plzl','полюс'), 'AFLT': ('aflt','аэрофлот'), 'MOEX': ('moex','мосбирж','московская биржа'),
    'SNGS': ('sngs','сургутнефтегаз'), 'ALRS': ('alrs','алрос'), 'IRAO': ('irao','интер рао'),
}
PATTERNS = {
    'dividends': r'дивиденд|отсечк', 'results': r'мсфо|рсбу|выручк|чист\w* прибыл|отч[её]тност|убыт',
    'rates': r'ключев\w* ставк|банк\w* росси|инфляц|\bцб\b|процентн\w* ставк',
    'sanctions': r'санкци|эмбарго|ограничени\w* (?:экспорт|импорт)',
    'trading': r'листинг|делистинг|приостанов\w* торг|возобнов\w* торг|сплит|байбэк|ipo|spo',
    'commodities': r'нефт|\bгаз\b|\bгаза\b|золот|металл|brent|opec|опек',
    'macro': r'ввп|бюджет|минфин|экономик|безработ|валют|рубл|налог|облигац|рын\w* акци|доллар|юан',
}


class FeedError(Exception):
    def __init__(self, status, message, retry=600):
        super().__init__(message)
        self.status, self.retry = status, max(600,min(86400,int(retry)))


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script','style'):
            self.hidden = max(0,self.hidden-1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(text):
    parser = TextOnly()
    parser.feed(unescape(text or '')[:10000])
    value = ''.join(parser.parts)
    value = ''.join(c for c in value if unicodedata.category(c) not in ('Cc','Cf') or c.isspace())
    return ' '.join(value.split())[:500]


def safe_url(url, source, *, feed=False):
    try:
        p = urlsplit(urljoin(source.home,url.strip()))
        if (p.scheme not in ('https','http') or p.hostname not in source.hosts or p.username or p.password
                or p.port not in (None,80,443) or '\\' in url or any(ord(c)<32 for c in url)):
            return None
        query = [(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True,max_num_fields=100)
                 if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid','yclid')]
        return urlunsplit(('https',p.hostname,p.path or '/',urlencode(query),''))
    except (ValueError,AttributeError):
        return None


def classify(title):
    text = unicodedata.normalize('NFKC',title).casefold().replace('ё','е')
    tokens = re.findall(r'[a-zа-я0-9]+',text)
    tickers = []
    for ticker,aliases in ALIASES.items():
        for alias in aliases:
            endings = ('','а','у','ом','е','ы','и','ов','ам','ами','ах','ь','ью','ей','ю','я','ем')
            if (' ' in alias and alias in text) or any(
                    token == alias if alias.isascii() or len(alias)<5 else
                    token.startswith(alias) and token[len(alias):] in endings for token in tokens):
                tickers.append(ticker)
                break
    topics = [key for key,pattern in PATTERNS.items() if re.search(pattern,text)] or ['other']
    return {'tickers':sorted(tickers), 'topics':topics, 'classifier_version':VERSION,
            'entry_allowed':False}


def publication(value, now):
    if not value:
        return None,'missing'
    try:
        try:
            at = parsedate_to_datetime(value.strip())
        except (ValueError,TypeError,IndexError):
            at = datetime.fromisoformat(value.strip().replace('Z','+00:00'))
        if at.tzinfo is None:
            return None,'timezone_missing'
        at = at.astimezone(timezone.utc)
        if at < datetime(2000,1,1,tzinfo=timezone.utc) or at > now:
            return None,'untrusted'
        return at.isoformat(),'publisher'
    except (ValueError,TypeError,OverflowError):
        return None,'invalid'


def local(tag):
    return tag.rsplit('}',1)[-1].lower()


def parse_feed(raw, source, now):
    if not source.available(now):
        raise FeedError('policy_blocked','Источник отключён или требуется повторная проверка условий.')
    if len(raw) > MAX_BYTES:
        raise FeedError('invalid_feed','RSS превышает лимит размера.')
    try:
        root = fromstring(raw,forbid_dtd=True,forbid_entities=True,forbid_external=True)
    except Exception:
        raise FeedError('invalid_feed','RSS не прошёл безопасную проверку XML.') from None
    if local(root.tag) not in ('rss','rdf','feed'):
        raise FeedError('invalid_feed','Сервер вернул не RSS/Atom. Обход защиты не выполняется.')
    result, seen = [], set()
    for node in root.iter():
        if local(node.tag) not in ('item','entry'):
            continue
        values, link = {}, None
        for child in node:
            key = local(child.tag)
            if key == 'link' and child.attrib.get('rel','alternate') == 'alternate':
                link = child.attrib.get('href') or ''.join(child.itertext())
            elif key in ('title','pubdate','published','date','updated','author','creator'):
                values[key] = ''.join(child.itertext())
        title = plain(values.get('title'))
        url = safe_url(link or '',source) if link else None
        if not title or not url or url in seen:
            continue
        published, status = publication(values.get('pubdate') or values.get('published') or values.get('date'),now)
        seen.add(url)
        result.append({'id':sha256((source.key+'\n'+url).encode()).hexdigest(), 'source':source.key,
                       'title':title, 'url':url, 'published_at':published, 'date_status':status,
                       'author':plain(values.get('creator') or values.get('author'))[:150],
                       'license_url':source.license_url,**classify(title)})
        if len(result) >= MAX_ITEMS:
            break
    return result


class RestrictedRedirect(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def __init__(self, source):
        self.source = source

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https' or not safe_url(newurl,self.source):
            raise FeedError('redirect_blocked','Переход на другой сайт или небезопасный адрес запрещён.')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def request_bytes(source, url, headers=None, limit=MAX_BYTES):
    if urlsplit(url).scheme != 'https' or not safe_url(url,source):
        raise FeedError('address_blocked','Разрешены только HTTPS-адреса зарегистрированного источника.')
    opener = build_opener(RestrictedRedirect(source),HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    request = Request(url,headers={'User-Agent':USER_AGENT,'Accept-Encoding':'identity',**(headers or {})})
    started = time.monotonic()
    try:
        with opener.open(request,timeout=12) as response:
            if response.headers.get('Content-Encoding','identity').lower() != 'identity':
                raise FeedError('invalid_feed','Неожиданное сжатие ответа; обход не выполняется.')
            blocks, size = [], 0
            while True:
                if time.monotonic()-started > 25:
                    raise FeedError('timeout','Превышено время загрузки источника.')
                chunk = response.read(min(32768,limit+1-size))
                if not chunk:
                    break
                blocks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise FeedError('invalid_feed','Ответ источника превышает лимит размера.')
            return b''.join(blocks),dict(response.headers)
    except HTTPError as exc:
        if exc.code == 304:
            return None,dict(exc.headers)
        retry = 600
        value = exc.headers.get('Retry-After','')
        try:
            retry = int(value)
        except ValueError:
            try:
                retry = int((parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
            except (ValueError,TypeError,IndexError):
                pass
        if exc.code in (401,403):
            raise FeedError('access_denied','Источник ограничил доступ. Обход блокировки не выполняется.',86400) from None
        if exc.code == 404:
            raise FeedError('not_found','Адрес RSS/robots.txt не найден.',3600) from None
        raise FeedError('rate_limited' if exc.code == 429 else 'http_error',
                        f'Источник временно недоступен (HTTP {exc.code}).',retry) from None
    except FeedError:
        raise
    except Exception as exc:
        if os.name=='nt' and isinstance(getattr(exc,'reason',exc),ssl.SSLCertVerificationError):
            return windows_request(source,url,headers or {},limit)
        raise FeedError('unavailable','Не удалось установить защищённое соединение с источником.') from None


def windows_request(source,url,headers,limit,redirects=0):
    """Use normal Windows trust-chain building, never verification=False."""
    if not safe_url(url,source) or urlsplit(url).scheme!='https' or redirects>3:
        raise FeedError('redirect_blocked','Адрес вне разрешённого источника.')
    try:
        script = Path(__file__).with_name('windows_https.ps1').read_text(encoding='utf-8')
        process = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            input=json.dumps({'url':url,'headers':{'User-Agent':USER_AGENT,'Accept-Encoding':'identity',**headers},'limit':limit}).encode(),
            capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        result = json.loads(process.stdout.decode('utf-8-sig'))
        if result.get('error'):
            raise ValueError('Windows HTTPS failed')
        status = result['status']; meta={k.lower():v for k,v in result['headers'].items()}
        if 300<=status<400 and status!=304:
            target = urljoin(url,meta.get('location',''))
            if not meta.get('location'): raise ValueError('Missing redirect location')
            return windows_request(source,target,headers,limit,redirects+1)
        if status==304: return None,result['headers']
        if status in (401,403):
            raise FeedError('access_denied','Источник ограничил доступ. Обход не выполняется.',86400)
        if status==404: raise FeedError('not_found','Адрес не найден.',3600)
        if status==429:
            delay = int(meta.get('retry-after','600')) if meta.get('retry-after','600').isdigit() else 3600
            raise FeedError('rate_limited','Ограничение частоты запросов источника.',delay)
        if status!=200 or meta.get('content-encoding','identity').lower()!='identity':
            raise ValueError('Unsupported HTTP response')
        raw = base64.b64decode(result['body'],validate=True)
        if len(raw)>limit: raise ValueError('Oversized response')
        return raw,result['headers']
    except FeedError:
        raise
    except Exception:
        raise FeedError('tls_error','Защищённое соединение не прошло проверку и в Windows. Проверка сертификатов не отключалась.') from None


def robots(source, cached, now):
    raw = cached.get('robots_text')
    at = cached.get('robots_checked')
    if raw is None or not at or now-datetime.fromisoformat(at) >= timedelta(hours=24):
        p = urlsplit(source.feed)
        try:
            body,_ = request_bytes(source,f'https://{p.netloc}/robots.txt',limit=131072)
            raw = body.decode('utf-8',errors='replace') if body is not None else ''
            if '<html' in raw.lower():
                raise FeedError('robots_unavailable','robots.txt вернул HTML; загрузка RSS приостановлена.')
        except FeedError as exc:
            if exc.status != 'not_found':
                raise
            raw = ''  # A real 404 is not a site prohibition; network errors fail closed.
        at = now.isoformat()
    parser = RobotFileParser()
    parser.parse(raw.splitlines())
    if not parser.can_fetch(ROBOT_AGENT,source.feed):
        raise FeedError('robots_blocked','robots.txt запрещает загрузку этой RSS-ленты.',86400)
    delay = parser.crawl_delay(ROBOT_AGENT) or 0
    rate = parser.request_rate(ROBOT_AGENT)
    interval = max(600,delay,rate.seconds/rate.requests if rate and rate.requests else 0)
    return {'robots_text':raw,'robots_checked':at,'robots_delay':max(delay,rate.seconds/rate.requests if rate and rate.requests else 0)},int(interval)


def fetch_feed(source, cached, now):
    if not source.available(now):
        raise FeedError('policy_blocked','Источник отключён или срок проверки правил истёк.',86400)
    checked,interval = robots(source,cached,now)
    if checked['robots_checked'] != cached.get('robots_checked') and checked['robots_delay']:
        # The robots request itself counts as an access. Defer rather than
        # immediately fetching the feed or keeping a worker asleep for minutes.
        return None,{**checked,'deferred':True},interval
    headers = {'Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml'}
    for key,header in (('etag','If-None-Match'),('last_modified','If-Modified-Since')):
        value = cached.get(key)
        if value and '\n' not in value and '\r' not in value:
            headers[header] = value[:500]
    raw, response_headers = request_bytes(source,source.feed,headers)
    # urllib header casing differs by publisher; make conditional metadata stable.
    response_headers = {k.lower():v for k,v in response_headers.items()}
    state = {**checked,'deferred':False,'etag':response_headers.get('etag',cached.get('etag')),
             'last_modified':response_headers.get('last-modified',cached.get('last_modified'))}
    items = None if raw is None else parse_feed(raw,source,now)
    return items,state,interval
