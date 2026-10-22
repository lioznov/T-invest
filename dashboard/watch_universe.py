"""An unrestricted watchlist, distinct from bounded simultaneous subscriptions."""
import re

COLLECTION_BATCH = 50  # 4 counted subscriptions/share = 200, below the documented 300.
ROTATION_SECONDS = 900
ANALYSIS_PARALLELISM = 3


def normalize(values):
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError('Укажи хотя бы один тикер, например SBER, GAZP.')
    unique = {}
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,14}', value.upper()):
            raise ValueError('Тикеры — латинские буквы и цифры, например SBER, GAZP.')
        unique[value.upper()] = None
    return list(unique)


def collection_group(tickers, elapsed):
    count = (len(tickers)+COLLECTION_BATCH-1)//COLLECTION_BATCH
    index = int(max(0, elapsed)//ROTATION_SECONDS) % count
    return tickers[index*COLLECTION_BATCH:(index+1)*COLLECTION_BATCH], index, count


class AnalysisQueue:
    """Fair round-robin; never flood the shared signal executor/cache with a universe."""
    def __init__(self):
        self.symbols = ()
        self.cursor = 0
        self.pending = {}

    def step(self, tickers, fetch, observe, now):
        current = tuple(tickers)
        if current != self.symbols:
            self.symbols, self.cursor = current, 0
            selected = set(current)
            self.pending = {t: started for t, started in self.pending.items() if t in selected}
        for _ in range(len(current)):
            if len(self.pending) >= ANALYSIS_PARALLELISM:
                break
            ticker = current[self.cursor]
            self.cursor = (self.cursor+1) % len(current)
            self.pending.setdefault(ticker, now)
        # No next group is queued until finished results from this one are observed.
        for ticker, started in list(self.pending.items()):
            try:
                payload = fetch('live', ticker)
            except Exception:
                payload = {'status': 'error'}
            ready = payload.get('status') == 'error' or (
                payload.get('status') == 'ok' and not payload.get('refreshing'))
            if ready or now-started >= 90:
                observe(ticker, payload if ready else {'status': 'error'})
                self.pending.pop(ticker, None)
