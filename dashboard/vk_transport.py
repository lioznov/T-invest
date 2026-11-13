"""Community-to-owner VK notifications. Fixed host, bounded I/O, no secret diagnostics."""
import html
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from .telegram_transport import NoRedirect


def configured():
    user = os.getenv('VK_USER_ID', '').strip()
    return bool(os.getenv('VK_COMMUNITY_TOKEN', '').strip() and user.isascii()
                and user.isdecimal() and len(user) <= 10 and 0 < int(user) < 2000000000)


def send(message):
    """ok=VK accepted; rejected=known refusal; unknown=never automatically repeat."""
    if not configured():
        return {'status': 'rejected', 'retry_after': 300, 'message': 'ВК не настроен. Проверь ключ сообщества и числовой ID получателя.'}
    # Shared signal formatter uses only these two HTML tags for Telegram.
    text = html.unescape(message.replace('<b>', '').replace('</b>', ''))
    data = urllib.parse.urlencode({'access_token': os.environ['VK_COMMUNITY_TOKEN'].strip(),
        'peer_id': os.environ['VK_USER_ID'].strip(), 'message': text,
        'random_id': secrets.randbelow(2**31-1)+1, 'v': '5.199', 'dont_parse_links': 1}).encode('utf-8')
    request = urllib.request.Request('https://api.vk.com/method/messages.send', data=data)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=8) as response:
            value = json.loads(response.read(65536))
        if type(value.get('response')) is int and value['response'] > 0:
            return {'status': 'ok', 'message': 'ВК принял сообщение. Push-уведомление зависит от настроек приложения ВК.'}
        code = value.get('error', {}).get('error_code')
        if code in (5, 7, 15, 27, 28, 100, 200, 901, 902):
            return {'status': 'rejected', 'retry_after': 300, 'message': 'ВК отказал: проверь ключ сообщества, ID получателя и разрешение получать сообщения.'}
        if code in (6, 9, 14, 29):
            return {'status': 'rejected', 'retry_after': 300, 'message': 'ВК ограничил отправку. Проверь настройки сообщества; повтор отложен.'}
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 429):
            return {'status': 'rejected', 'retry_after': 300, 'message': 'ВК отклонил запрос. Проверь доступ и ограничения отправки.'}
    except Exception:
        pass  # Never expose tokens, request contents or raw provider diagnostics.
    return {'status': 'unknown', 'message': 'Приём сообщения ВК не подтверждён. Автоповтор этого сигнала отключён, чтобы избежать дубля.'}
