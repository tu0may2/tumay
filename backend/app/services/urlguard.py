"""Проверка адресов, на которые терминал ходит сам.

Адрес вебхука задаёт человек, а запрос по нему делает сервер — со своего
места в сети, изнутри периметра. Без проверки это готовый способ заставить
терминал сходить туда, куда снаружи хода нет: на служебный адрес облака,
раздающий токены виртуальной машины, на соседний сервис в локальной сети или
на собственный файл через ``file://``. Ответ наружу не возвращается, но сам
факт «дошло или нет» и время ответа уже позволяют прощупывать внутреннюю
сеть, а ``file://`` открывает файлы на диске.

Поэтому адрес проверяется дважды: сначала по схеме и имени узла, потом по
каждому IP, в который это имя разрешается. Второе обязательно — имя в
публичном домене может указывать на 127.0.0.1 или на 169.254.169.254, и
проверка одной строки адреса этого не поймает.

Переадресации отключены по той же причине: разрешённый адрес вправе ответить
«идите на 169.254.169.254», и без запрета клиент послушно пойдёт.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

#: Единственные схемы, по которым терминал ходит наружу. ``file``, ``ftp`` и
#: прочее умеет и urllib — отсюда и запрет: ``file:///etc/passwd`` в поле
#: «адрес вебхука» иначе читает файл с диска сервера
ALLOWED_SCHEMES = ("http", "https")

#: Порты, за которыми в локальной сети обычно стоит не вебхук, а база данных
#: или служебный интерфейс. Список не защита сам по себе (адреса уже
#: проверены), а вторая преграда на случай ошибки в первой
BLOCKED_PORTS = frozenset({22, 23, 25, 445, 3306, 5432, 6379, 9200, 11211, 27017})


class UnsafeUrl(ValueError):
    """Адрес, по которому терминалу ходить нельзя."""


def _is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Внутренний ли адрес.

    Проверяем не только «серые» диапазоны: link-local 169.254.0.0/16 — это
    служебный адрес облака, откуда виртуальная машина берёт свои токены, и
    он не подпадает под ``is_private`` в старых версиях Python.
    """
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def resolve_targets(host: str, port: int) -> list[str]:
    """Все адреса, в которые разрешается имя узла.

    Проверять надо каждый: имя может отдавать несколько записей, и достаточно
    одной внутренней, чтобы запрос ушёл не туда.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"Имя {host} не разрешается: {exc}") from exc
    return sorted({info[4][0] for info in infos})


def check_outbound_url(url: str, *, resolve: bool = True) -> str:
    """Проверить адрес перед исходящим запросом. Вернуть его же или бросить.

    ``resolve=False`` пропускает обращение к DNS — нужно тестам и разбору
    заведомо некорректных адресов, где до сети дело не доходит.
    """
    parts = urlsplit((url or "").strip())

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrl(
            "Адрес должен начинаться с http:// или https:// — "
            f"схема «{parts.scheme or 'не указана'}» не подходит"
        )
    if not parts.hostname:
        raise UnsafeUrl("В адресе не указан узел")

    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    if port in BLOCKED_PORTS:
        raise UnsafeUrl(f"Порт {port} для вебхука не годится")

    host = parts.hostname
    # Адрес, записанный числом, проверяем сразу — DNS тут не участвует
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_private(literal):
            raise UnsafeUrl(
                f"{host} — внутренний адрес. Вебхук должен смотреть наружу: "
                "иначе терминал станет ходить по локальной сети от своего имени"
            )
        return url

    if not resolve:
        return url

    for candidate in resolve_targets(host, port):
        address = ipaddress.ip_address(candidate)
        if _is_private(address):
            raise UnsafeUrl(
                f"Имя {host} указывает на внутренний адрес {candidate}. "
                "Вебхук должен смотреть наружу"
            )
    return url


def is_safe(url: str, *, resolve: bool = True) -> bool:
    """Тот же вопрос, но ответом «да/нет» — для мест, где исключение лишнее."""
    try:
        check_outbound_url(url, resolve=resolve)
    except UnsafeUrl:
        return False
    return True
