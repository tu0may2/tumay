"""Ограничение частоты попыток входа.

Без него пароль подбирается перебором: форма входа отвечает быстро, а
терминал выставлен в интернет. Особенно это касается запасных паролей
(``TREASURY_EXTRA_PASSWORDS``) — они короткие и общие для всех учётных
записей, то есть подбирать их проще, чем обычный пароль.

Счётчики держим в памяти процесса. Это осознанное упрощение: терминал
работает одним процессом uvicorn, внешнего хранилища у него нет, а
переживать перезапуск счётчикам попыток не обязательно — перезапуск
сервиса и так редкое событие, которым злоумышленник не управляет.
Если терминал когда-нибудь запустят в несколько рабочих процессов,
ограничение станет посвободнее ровно во столько раз, сколько процессов;
тогда счётчики надо будет вынести наружу.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, status

#: Сколько неудачных попыток допускаем и за какое время
MAX_ATTEMPTS = 8
WINDOW_SEC = 300
#: На сколько закрываем вход после исчерпания попыток
LOCKOUT_SEC = 900

_lock = threading.Lock()
#: ключ → отметки времени неудачных попыток
_failures: dict[str, deque[float]] = defaultdict(deque)
#: ключ → до какого момента вход закрыт
_locked_until: dict[str, float] = {}


def _prune(key: str, now: float) -> None:
    """Забыть попытки, вышедшие за окно."""
    marks = _failures[key]
    while marks and now - marks[0] > WINDOW_SEC:
        marks.popleft()
    if not marks:
        _failures.pop(key, None)


def check(key: str) -> None:
    """Пустить или отказать. При отказе поднимает 429 с временем ожидания."""
    now = time.monotonic()
    with _lock:
        until = _locked_until.get(key)
        if until is not None:
            if until > now:
                wait = int(until - now) + 1
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        "Слишком много попыток входа. "
                        f"Повторите через {wait // 60 + 1} мин."
                    ),
                    headers={"Retry-After": str(wait)},
                )
            # Срок истёк — начинаем с чистого листа
            _locked_until.pop(key, None)
            _failures.pop(key, None)


def register_failure(key: str) -> None:
    """Отметить неудачную попытку и при переборе закрыть вход."""
    now = time.monotonic()
    with _lock:
        _prune(key, now)
        _failures[key].append(now)
        if len(_failures[key]) >= MAX_ATTEMPTS:
            _locked_until[key] = now + LOCKOUT_SEC
            _failures.pop(key, None)


def register_success(key: str) -> None:
    """Успешный вход обнуляет счётчик: он считает именно подбор."""
    with _lock:
        _failures.pop(key, None)
        _locked_until.pop(key, None)


def reset() -> None:
    """Полный сброс — нужен тестам, чтобы они не влияли друг на друга."""
    with _lock:
        _failures.clear()
        _locked_until.clear()


def client_key(request) -> str:
    """Из какого адреса пришёл запрос.

    За обратным прокси реальный адрес приходит в ``X-Forwarded-For``; берём
    первый элемент — его подставляет наш nginx. Заголовку можно доверять
    только потому, что снаружи к приложению не достучаться: оно слушает
    127.0.0.1, и единственный источник запросов — прокси, который этот
    заголовок перезаписывает.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"
