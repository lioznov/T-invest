"""The radar's Telegram destination, with bounded I/O and no secret diagnostics."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def configured():
    return bool(os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
                and os.getenv('TELEGRAM_CHAT_ID', '').strip())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a bot credential to a redirected host.


def send(message):
    """ok / rejected / unknown. Never retry an ambiguous delivery automatically."""
    if not configured():
        return {'status': 'rejected', 'retry_after': 300, 'message': 'Бот не настроен.'}
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
    payload = urllib.parse.urlencode({
        'chat_id': os.environ['TELEGRAM_CHAT_ID'].strip(), 'text': message,
        'parse_mode': 'HTML', 'link_preview_options': json.dumps({'is_disabled': True}),
    }).encode('utf-8')
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/sendMessage', data=payload)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
            value = json.loads(response.read(65536))
        if value.get('ok') is True and isinstance(value.get('result'), dict) and value['result'].get('message_id'):
            return {'status': 'ok', 'message': 'Telegram подтвердил отправку.'}
        if value.get('ok') is False:
            return {'status': 'rejected', 'retry_after': 60, 'message': 'Telegram отклонил сообщение. Проверь бота и доступ к чату.'}
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                delay = int(json.loads(exc.read(65536)).get('parameters', {}).get('retry_after', 60))
            except (ValueError, TypeError, AttributeError):
                delay = 60
            return {'status': 'rejected', 'retry_after': max(60, min(delay, 86400)),
                    'message': 'Лимит Telegram. Отправка временно приостановлена.'}
        if exc.code in (400, 401, 403, 404):
            return {'status': 'rejected', 'retry_after': 300,
                    'message': 'Telegram отказал в отправке. Проверь токен, чат и /start в боте.'}
    except Exception:
        pass  # Exception URLs can contain the bot token. Never log them.
    return {'status': 'unknown', 'message': 'Доставка не подтверждена; повтор этого сигнала отключён, чтобы не дублировать сообщение.'}
