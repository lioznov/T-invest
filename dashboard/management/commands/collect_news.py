import time
from django.core.management.base import BaseCommand, CommandError
from dashboard.news_sources import SOURCES
from dashboard.news_store import collect_once


class Command(BaseCommand):
    help = 'Collect approved personal RSS headlines only. No full texts, broker calls or trading signals.'

    def add_arguments(self, parser):
        parser.add_argument('--sources',nargs='+',choices=tuple(SOURCES))
        parser.add_argument('--cycles',type=int,default=1)
        parser.add_argument('--interval',type=int,default=600)

    def handle(self,*args,**options):
        if not 1 <= options['cycles'] <= 144 or not 600 <= options['interval'] <= 86400:
            raise CommandError('Нужно 1–144 цикла и интервал не меньше 600 секунд.')
        sources = [SOURCES[k] for k in options['sources']] if options['sources'] else list(SOURCES.values())
        for cycle in range(options['cycles']):
            for r in collect_once(sources=sources):
                self.stdout.write(f'{r["source"]}: {r["status"]}; заголовков: {r["items"]}')
            if cycle+1 < options['cycles']:
                try:
                    time.sleep(options['interval'])
                except KeyboardInterrupt:
                    self.stdout.write('Сбор остановлен; сохранённые заголовки остаются в data/news.sqlite3.')
                    return
