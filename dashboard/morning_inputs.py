"""Load existing immutable, source-checked EXCHANGE inputs without downloading."""
from pathlib import Path
from .morning_model import TICKERS, validate


def load_history(directory, ticker):
    from .management.commands.study_pattern_flow import acquire
    from .management.commands.study_opening_activity import extension
    from .management.commands.verify_neural_trader import load_extension
    from .management.commands.study_diverse_long import fresh_input
    if ticker not in TICKERS:
        raise ValueError('Unsupported ticker')
    directory = Path(directory)
    base = acquire(directory, ticker, fetch=False)
    if not base['quality']['exchange_requested'] or not base['quality']['exchange_source_verified']:
        raise ValueError('No verified exchange history')
    instrument = base['saved']['instrument']
    candles, sources = list(base['candles']), [base['source']]
    for loader in (extension, load_extension, fresh_input):
        bars, source = loader(directory, ticker, instrument, False)
        candles.extend(bars); sources.append(source)
    # Overlaps must not silently replace earlier observed prices.
    by_time = {}
    for bar in candles:
        if bar.time in by_time and by_time[bar.time] != bar:
            raise ValueError('Conflicting historical candles')
        by_time[bar.time] = bar
    ordered = [by_time[at] for at in sorted(by_time)]
    validate(ordered)
    return {'instrument': instrument, 'candles': ordered, 'sources': sources}
