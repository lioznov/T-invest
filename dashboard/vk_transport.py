"""Community-to-owner VK notifications. Fixed host, bounded I/O, no secret diagnostics."""
import html
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from .telegram_transport import NoRedirect

REFUSALS = {
    5: 'Ключ не прошёл авторизацию. Проверь ключ сообщества в .env и перезапусти сервер.',
    7: 'Нет права на отправку. Проверь право «Сообщения» у ключа сообщества.',
    15: 'Доступ запрещён. Проверь включение сообщений сообщества и права ключа.',
    27: 'Ключ сообщества не прошёл авторизацию. Создай новый ключ с правом «Сообщения», замени в .env и перезапусти сервер.',
    28: 'Авторизация приложения не прошла. Для бота нужен ключ сообщества с правом «Сообщения».',
    100: 'Параметры запроса не приняты. Проверь числовой VK_USER_ID личной страницы и версию программы.',
    113: 'Получатель не найден. В VK_USER_ID нужен числовой ID твоей личной страницы.',
    200: 'Доступ запрещён. Проверь ключ сообщества, его права и включение сообщений.',
    900: 'Отправка запрещена из-за чёрного списка. Проверь блокировки между личной страницей и сообществом.',
    901: 'Нет разрешения писать получателю. Со своей личной страницы открой диалог именно с Vorby Pro, разреши сообщения и отправь «Начать». Подписки на группу недостаточно.',
    902: 'Сообщения получателю запрещены настройками приватности. Проверь разрешение сообщений от этого сообщества и VK_USER_ID.',
}


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
        if type(code) is int and code in REFUSALS:
            return {'status': 'rejected', 'retry_after': 300, 'message': f'ВК ({code}): {REFUSALS[code]}'}
        if type(code) is int and code in (6, 9, 14, 29):
            detail = 'ВК потребовал проверку CAPTCHA. Отправка приостановлена; проверь сообщество в ВК.' if code == 14 else 'ВК ограничил отправку. Повтор отложен.'
            return {'status': 'rejected', 'retry_after': 300, 'message': f'ВК ({code}): {detail}'}
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 429):
            return {'status': 'rejected', 'retry_after': 300, 'message': 'ВК отклонил запрос. Проверь доступ и ограничения отправки.'}
    except Exception:
        pass  # Never expose tokens, request contents or raw provider diagnostics.
    return {'status': 'unknown', 'message': 'Приём сообщения ВК не подтверждён. Автоповтор этого сигнала отключён, чтобы избежать дубля.'}
