"""Read-only, fail-closed projection of the evening-clock comparison.

Reading this report never fetches a quote, trains a model, or emits an order.
The experiment cannot promote any timing variant to a live trading signal.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from .morning_service import _report, _text, _time, digest


MAX_ARTIFACT_BYTES = 3_000_000
VARIANTS = {
    'control': ('17:45', '18:00'),
    'main_hour': ('18:00', '18:15'),
    'evening_hour': ('22:45', '23:00'),
}
NOTICE = ('Сравнение часов расчёта на прошлых данных, не текущий прогноз и не сигнал. '
          'Выбор лучшего результата задним числом не доказывает будущую прибыль. '
          'Реальные заявки, шорт и Telegram не подключены.')


def study_spec():
    from . import close_timing_study
    return close_timing_study


def _variants(value):
    if not isinstance(value, list) or len(value) != len(VARIANTS):
        raise ValueError('Missing fixed timing variants')
    projected = {}
    for variant in value:
        if not isinstance(variant, dict):
            raise ValueError('Invalid variant')
        name = variant['id']
        if name not in VARIANTS or name in projected:
            raise ValueError('Unknown or duplicated variant')
        cutoff, entry = VARIANTS[name]
        if (variant['cutoff'] != cutoff or variant['entry'] != entry
                or not _text(variant['label'], 200)):
            raise ValueError('Invalid fixed timing')
        projected[name] = {
            'id': name, 'cutoff': cutoff, 'entry': entry, 'label': variant['label'],
            'report': _report(variant['report']),
            'paired_report': _report(variant['paired_report']),
        }
    return [projected[name] for name in VARIANTS]


def load_artifact(now=None):
    """A broken newest artifact must never silently fall back to an older one."""
    now = now or datetime.now(timezone.utc)
    root = (Path(settings.BASE_DIR) / 'dashboard' / 'ai_models').resolve()
    try:
        paths = sorted(root.glob('close-timing-v1-*.json'), reverse=True)
        if not paths:
            return None
        path = paths[0]
        if path.resolve().parent != root or path.stat().st_size > MAX_ARTIFACT_BYTES:
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None
        spec = study_spec()
        if (value['version'] != spec.VERSION or value['entry_allowed'] is not False
                or value['source_sha256'] != spec.source_hashes()
                or value['artifact_sha256'] != digest({k: v for k, v in value.items()
                                                     if k != 'artifact_sha256'})
                or _time(value['created_at']) > now):
            return None
        _variants(value['variants'])
        for key in ('note', 'paired_note'):
            if not _text(value[key], 8000):
                return None
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError,
            OverflowError, RecursionError, ImportError):
        return None


def payload(now=None):
    base = {'status': 'unavailable', 'entry_allowed': False, 'notice': NOTICE,
            'message': 'Сохранённого сравнения пока нет или файл не прошёл проверку.'}
    artifact = load_artifact(now)
    if artifact is None:
        return base
    return {**base, 'status': 'historical', 'version': artifact['version'],
            'created_at': artifact['created_at'], 'variants': _variants(artifact['variants']),
            'note': artifact['note'], 'paired_note': artifact['paired_note'],
            'message': 'Три времени расчёта проверены отдельно. Ни один вариант не включён в текущие сигналы.'}
