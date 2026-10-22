"""Manually reviewed public feeds. RSS is not a blanket copyright licence."""
from dataclasses import dataclass
from datetime import date, timedelta, timezone

USER_AGENT = 'VorbyNews/1.0 (+local personal RSS reader)'
ROBOT_AGENT = 'VorbyNews'
REVIEWED = date(2026, 10, 21)
REVIEW_DAYS = 30
INTERVAL = 600


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    home: str
    feed: str
    hosts: tuple
    terms: str
    basis: str
    official: bool = False
    enabled: bool = True
    reviewed: date = REVIEWED
    license_url: str = ''

    def available(self, now):
        local_day = now.astimezone(timezone(timedelta(hours=3))).date()
        return self.enabled and self.reviewed <= local_day <= self.reviewed+timedelta(days=REVIEW_DAYS)

    def public(self):
        return {'id': self.key, 'name': self.name, 'home': self.home,
                'terms_url': self.terms, 'policy_note': self.basis, 'official': self.official,
                'enabled': self.enabled, 'reviewed_on': self.reviewed.isoformat(),
                'review_due': (self.reviewed+timedelta(days=REVIEW_DAYS)).isoformat(),
                'license_url':self.license_url}


SOURCES = {
    'cbr': Source('cbr', 'Банк России', 'https://www.cbr.ru/', 'https://www.cbr.ru/rss/RssNews',
        ('cbr.ru', 'www.cbr.ru'), 'https://www.cbr.ru/development/',
        'Официальная RSS-подписка. Только заголовки и прямые ссылки; запросы кэшируются.', True),
    'moex': Source('moex', 'Московская биржа', 'https://www.moex.com/',
        'https://moex.com/export/news.aspx?cat=101', ('moex.com','www.moex.com'),
        'https://www.moex.com/a40',
        'Биржа разрешает RSS-агрегаторы и размещение заголовков с аннотациями. Используем только заголовки.', True),
    'interfax': Source('interfax', 'Интерфакс', 'https://www.interfax.ru/',
        'https://www.interfax.ru/rss.asp', ('interfax.ru','www.interfax.ru'),
        'https://m.interfax.ru/license',
        'Локальное личное чтение RSS, без публичной ретрансляции. Ссылка на Интерфакс и оригинал обязательна.'),
    'prime': Source('prime', 'ПРАЙМ', 'https://1prime.ru/', 'https://1prime.ru/export/rss2/index.xml',
        ('1prime.ru','www.1prime.ru'), 'https://1prime.ru/docs/terms_terms_of_use.html',
        'Правила разрешают трансляцию заголовков с соблюдением ссылок. Полные статьи не скачиваются.'),
    'rbc': Source('rbc', 'РБК', 'https://www.rbc.ru/', '', ('rbc.ru','www.rbc.ru'),
        'https://www.rbc.ru/privacy/',
        'Отключён: правила запрещают автоматизированное извлечение без официального разрешения. Личное использование не отменяет этот запрет.',
        enabled=False),
    'finam': Source('finam','Финам','https://www.finam.ru/',
        'https://www.finam.ru/analysis/conews/rsspoint/',('finam.ru','www.finam.ru'),
        'https://www.finam.ru/page/copyright/',
        'Официальная RSS новостей компаний; материалы этого раздела — CC BY 4.0. Авторство и лицензия сохраняются; метки добавлены Vorby Pro.',
        license_url='https://creativecommons.org/licenses/by/4.0/'),
    'disclosure': Source('disclosure','Интерфакс · раскрытие','https://www.e-disclosure.ru/','',
        ('e-disclosure.ru','www.e-disclosure.ru'),'https://e-disclosure.ru/poluchenie-informacii/shlyuz-api',
        'Официальный API требует авторизованного доступа и согласованных условий. До получения доступа не запрашивается.',enabled=False),
}
