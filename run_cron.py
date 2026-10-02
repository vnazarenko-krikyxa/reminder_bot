"""Режим для виртуального хостинга: cron запускает скрипт раз в минуту.

Скрипт ~55 секунд слушает Telegram (long polling), обрабатывает команды и кнопки,
отправляет созревшие напоминания и выходит. Состояние диалогов (/add) переживает
перезапуски через PicklePersistence. Файловая блокировка не даёт двум запускам
работать одновременно.
"""
import asyncio
import fcntl
import logging
import time

from telegram.error import NetworkError, RetryAfter
from telegram.ext import PicklePersistence

from bot import build_app, send_due
from config import BASE_DIR

RUN_SECONDS = 55
log = logging.getLogger("reminder_bot.cron")


async def run():
    persistence = PicklePersistence(BASE_DIR / "state.pickle", update_interval=3600)
    app = build_app(persistence)
    deadline = time.monotonic() + RUN_SECONDS
    offset = None
    # Сеть хостинга до Telegram иногда даёт таймаут/502 — повторяем, а не падаем
    while True:
        try:
            await app.initialize()
            break
        except RetryAfter as e:
            log.warning("Telegram просит подождать %s с", e.retry_after)
            await asyncio.sleep(float(getattr(e.retry_after, "total_seconds", lambda: e.retry_after)()))
            continue
        except NetworkError as e:
            if deadline - time.monotonic() < 10:
                log.warning("Telegram недоступен, пропускаю запуск: %s", e)
                return
            log.warning("Telegram недоступен, повтор: %s", e)
            await asyncio.sleep(3)
    try:
        try:
            await send_due(app.bot)
        except NetworkError as e:
            log.warning("Напоминания не отправлены, попробую в следующую минуту: %s", e)
        while (left := deadline - time.monotonic()) > 2:
            try:
                updates = await app.bot.get_updates(
                    offset=offset, timeout=int(min(25, left - 1)),
                    allowed_updates=["message", "callback_query"],
                )
            except RetryAfter as e:
                log.warning("getUpdates: Telegram просит подождать %s с", e.retry_after)
                await asyncio.sleep(float(getattr(e.retry_after, "total_seconds", lambda: e.retry_after)()))
                continue
            except NetworkError as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(3)
                continue
            for u in updates:
                offset = u.update_id + 1
                try:
                    await app.process_update(u)
                except Exception:
                    log.exception("Ошибка обработки update %s", u.update_id)
            if updates:
                await app.update_persistence()
        if offset is not None:
            # подтверждаем Telegram, что эти апдейты уже обработаны
            try:
                await app.bot.get_updates(offset=offset, timeout=0)
            except NetworkError as e:
                log.warning("Не подтвердил апдейты (придут повторно): %s", e)
        await app.update_persistence()
    finally:
        await app.shutdown()  # сбрасывает persistence на диск


def main():
    lock = open(BASE_DIR / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # предыдущий запуск ещё работает
    asyncio.run(run())


if __name__ == "__main__":
    main()
