"""VK delivery with the same freshness, episode and cooldown rules as Telegram."""
import threading
import time
from . import telegram_alerts as core
from . import vk_transport

THREAD_LOCK = threading.Lock()
THREAD = None


class Store(core.Store):
    transport = vk_transport
    filename = 'vk.sqlite3'
    channel_name = 'ВКонтакте'
    setup_message = 'В серверном .env нужны VK_COMMUNITY_TOKEN и VK_USER_ID. Затем перезапусти сайт.'

    def test_message(self):
        if not self.transport.configured():
            raise ValueError(self.setup_message)
        # Persist before network I/O, including across process restarts and ambiguous responses.
        now = time.time()
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS test_attempt (id INTEGER PRIMARY KEY, at REAL NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            last = db.execute('SELECT at FROM test_attempt WHERE id=1').fetchone()
            if last and now-last['at'] < 60:
                raise ValueError('Между тестовыми сообщениями нужна пауза не менее минуты.')
            db.execute('INSERT OR REPLACE INTO test_attempt VALUES(1,?)', (now,))
        return self.transport.send('VORBY · Проверка уведомлений ВК.\nПодключение сайта проверяется этим сообщением. Это тест, не сигнал покупки.')


def ensure_worker():
    global THREAD
    with THREAD_LOCK:
        if THREAD is None or not THREAD.is_alive():
            THREAD = threading.Thread(target=core.worker_loop, kwargs={'store': Store()},
                                      daemon=True, name='vk-candidates')
            THREAD.start()
