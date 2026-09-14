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

#: Потолок числа отслеживаемых ключей. Ключ складывается из адреса и логина —
#: и то, и другое приходит снаружи, поэтому без потолка счётчики сами
#: становятся способом занять память: миллион попыток с разными логинами —
#: миллион записей. При переполнении выбрасываем самые старые: свежие попытки
#: важнее, а тот, кто перебирает, как раз и создаёт поток новых ключей.
MAX_TRACKED_KEYS = 4096

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


def _evict(now: float) -> None:
    """Убрать протухшее, а если ключей всё равно слишком много — самые старые.

    Сначала чистим по сроку: блокировки, у которых он вышел, и попытки за
    пределами окна ничего уже не значат. Обычно этого достаточно, и до
    выбрасывания живых записей дело не доходит.
    """
    for key, until in list(_locked_until.items()):
        if until <= now:
            _locked_until.pop(key, None)
    for key in list(_failures):
        _prune(key, now)

    excess = len(_failures) + len(_locked_until) - MAX_TRACKED_KEYS
    if excess <= 0:
        return
    # Блокировки держим до последнего: это те, кого уже поймали на переборе
    by_age = sorted(_failures.items(), key=lambda item: item[1][0] if item[1] else now)
    for key, _ in by_age[:excess]:
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
        if len(_failures) + len(_locked_until) > MAX_TRACKED_KEYS:
            _evict(now)


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

    Берём ``X-Real-IP``, а не ``X-Forwarded-For``. Разница принципиальная:
    nginx собирает ``X-Forwarded-For`` директивой ``$proxy_add_x_forwarded_for``,
    то есть **дописывает** свой адрес к тому, что прислал клиент. Первый
    элемент списка приходит снаружи и подделывается свободно — а именно его
    здесь и брали раньше. Достаточно было слать на каждой попытке новый
    вымышленный адрес, чтобы счётчик попыток никогда не срабатывал, а словарь
    счётчиков рос от каждой из них.

    ``X-Real-IP`` nginx проставляет через ``proxy_set_header``, который
    значение заменяет, а не дополняет: подделать его снаружи нельзя. Если
    заголовка нет (терминал запущен без прокси), берём адрес сокета — его
    подделать нельзя тем более.
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        # Даже доверенный заголовок не пускаем в ключ целиком: длина у него
        # своя, а словарь счётчиков — общий
        return real[:64]
    client = request.client
    return client.host if client else "unknown"
