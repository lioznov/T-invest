from django.core.management.base import BaseCommand
from dashboard.news_context import TICKERS, fetch_calendar, fetch_prices


class Command(BaseCommand):
    help = 'Read-only corporate dates and exchange candles for descriptive news reactions. No orders.'

    def add_arguments(self,parser):
        parser.add_argument('--tickers',nargs='+',choices=TICKERS,default=list(TICKERS))
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--calendar-only',action='store_true')
        mode.add_argument('--prices-only',action='store_true')

    def handle(self,*args,**options):
        for ticker in options['tickers']:
            if not options['prices_only']:
                self.stdout.write(str(fetch_calendar(ticker)))
            if not options['calendar_only']:
                self.stdout.write(str(fetch_prices(ticker)))
