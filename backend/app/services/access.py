"""Роли, состав вкладок и запрет на чужие разделы.

Роль отвечает на два независимых вопроса: что человеку можно менять и что ему
показывать. Первое — ``level`` (смотреть, торговать, настраивать), второе —
список вкладок. Держать их вместе нельзя: иначе под каждое сочетание «сделки
заводит, но облигации ему не нужны» пришлось бы заводить отдельную роль.

Скрытая вкладка закрывается и на сервере, а не только в интерфейсе. Спрятать
кнопку — не защита: адрес запроса виден в любой вкладке разработчика, и
казначейство вправе рассчитывать, что «этому человеку облигации не видны»
означает именно это. Поэтому каждый путь API отнесён к разделам, из которых
он вызывается, и запрос отвергается, если ни один из них человеку не открыт.

Пути, не попавшие в карту, разрешены. Это осознанный перекос в сторону
работоспособности: забытый в карте новый обработчик всего лишь останется
доступным, тогда как обратное умолчание превращало бы каждую новую ручку в
поломку у половины пользователей — причём заметную не сразу.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Role, User

#: Уровни прав по возрастанию — ими управляется запись, а не видимость
LEVELS = ("viewer", "trader", "admin")
LEVEL_TITLES = {
    "viewer": "Просмотр",
    "trader": "Сделки и лимиты",
    "admin": "Администратор",
}

#: Вкладки терминала в том порядке, в каком они стоят в интерфейсе.
#: Список — единственный источник правды: по нему рисуется и выбор вкладок в
#: роли, и проверка на сервере, поэтому разойтись с разметкой он не может.
TABS: tuple[tuple[str, str], ...] = (
    ("calendar", "Платёжный календарь"),
    ("cash", "Позиция"),
    ("portfolio", "Портфель"),
    ("ratios", "Нормативы"),
    ("accounts", "Счета"),
    ("overview", "Обзор рынка"),
    ("instruments", "Инструменты"),
    ("bonds", "Облигации"),
    ("imports", "Импорт и сверка"),
    ("signals", "Сигналы"),
    ("admin", "Настройки"),
    ("access", "Доступы"),
    ("sources", "Источники"),
)

TAB_CODES: tuple[str, ...] = tuple(code for code, _ in TABS)
TAB_TITLES: dict[str, str] = dict(TABS)

#: Пути, доступные без входа: без них нельзя ни войти, ни проверить,
#: что сервис жив
PUBLIC: tuple[str, ...] = ("/api/auth", "/api/health")

#: Пути, которые не проверяются на раздел, но вход всё равно требуют.
#: ``/api/overview`` здесь ради бегущей строки в шапке — ключевая ставка,
#: индексы и курсы это публичные котировки, скрывать их не от кого, а пустая
#: строка вверху выглядит поломкой. ``/api/portfolio/names`` наполняет выбор
#: портфеля, который стоит в шапке и действует на все вкладки сразу.
ALWAYS_ALLOWED: tuple[str, ...] = PUBLIC + (
    "/api/overview",
    "/api/portfolio/names",
)

#: Путь API → разделы, из которых он вызывается. Доступ есть, если человеку
#: открыт хотя бы один из них: карточка бумаги открывается и из «Инструментов»,
#: и из «Облигаций», и из ломбардного списка на «Нормативах».
#:
#: Карта снята с работающего интерфейса, а не придумана: каждая вкладка была
#: открыта в браузере, и запросы записаны — поэтому в ней нет ни лишнего, ни
#: забытого на момент составления.
SECTION_PATHS: tuple[tuple[str, frozenset[str]], ...] = (
    # Платёжный календарь
    ("/api/cash/matrix", frozenset({"calendar"})),
    ("/api/cash/ledger", frozenset({"calendar"})),
    ("/api/cash/history", frozenset({"calendar"})),
    ("/api/cash/calendar", frozenset({"calendar", "cash"})),
    # Позиция и Счета
    ("/api/cash/position", frozenset({"cash", "accounts"})),
    ("/api/cash/accounts", frozenset({"cash", "accounts"})),
    ("/api/cash/flows", frozenset({"accounts"})),
    ("/api/cash/placements", frozenset({"accounts"})),
    # Портфель
    ("/api/portfolio", frozenset({"portfolio"})),
    ("/api/history", frozenset({"portfolio"})),
    ("/api/report", frozenset({"portfolio"})),
    # Нормативы
    ("/api/ratios", frozenset({"ratios"})),
    ("/api/collateral", frozenset({"ratios"})),
    # Обзор рынка
    ("/api/curve", frozenset({"overview"})),
    ("/api/movers", frozenset({"overview"})),
    ("/api/fx", frozenset({"overview"})),
    ("/api/rates", frozenset({"overview"})),
    ("/api/series", frozenset({"overview"})),
    # Инструменты и Облигации
    ("/api/instruments", frozenset(
        {"instruments", "bonds", "overview", "portfolio", "ratios", "signals"}
    )),
    ("/api/boards", frozenset({"instruments"})),
    ("/api/screens", frozenset({"instruments", "bonds"})),
    ("/api/watchlist", frozenset({"instruments", "bonds", "signals"})),
    ("/api/bonds", frozenset({"bonds"})),
    ("/api/export", frozenset({"bonds"})),
    ("/api/benchmark", frozenset({"bonds", "portfolio"})),
    # Импорт и сверка
    ("/api/import", frozenset({"imports"})),
    # Сигналы
    ("/api/alerts", frozenset({"signals"})),
    ("/api/anomalies", frozenset({"signals"})),
    ("/api/calendar", frozenset({"signals"})),
    ("/api/offers", frozenset({"signals"})),
    ("/api/limits", frozenset({"signals", "portfolio"})),
    # Настройки
    ("/api/notifications", frozenset({"admin"})),
    ("/api/taxes", frozenset({"admin"})),
    ("/api/audit", frozenset({"admin", "access"})),
    ("/api/events", frozenset({"admin"})),
    # Доступы
    ("/api/users", frozenset({"access"})),
    ("/api/roles", frozenset({"access"})),
    # Источники
    ("/api/sources", frozenset({"sources"})),
    ("/api/collect", frozenset({"sources"})),
)

#: Разбираем от длинного к короткому: «/api/cash/calendar» должен побеждать
#: «/api/cash», иначе один раздел утащил бы к себе весь денежный блок
_SORTED_PATHS: tuple[tuple[str, frozenset[str]], ...] = tuple(
    sorted(SECTION_PATHS, key=lambda item: len(item[0]), reverse=True)
)

#: Встроенные роли. Заводятся при первом запуске и удалению не подлежат:
#: на них ссылаются уже существующие учётные записи.
BUILTIN_ROLES: tuple[dict[str, Any], ...] = (
    {
        "name": "viewer",
        "title": "Просмотр",
        "level": "viewer",
        "comment": "Смотрит, но ничего не меняет",
    },
    {
        "name": "trader",
        "title": "Сделки и лимиты",
        "level": "trader",
        "comment": "Заводит сделки, движения и лимиты",
    },
    {
        "name": "admin",
        "title": "Администратор",
        "level": "admin",
        "comment": "Полный доступ, включая учётные записи",
    },
)

#: Роль, у которой вкладки не отнимаются ни при каких настройках. Иначе
#: администратор способен запереть сам себя снаружи собственного терминала,
#: и открыть его обратно будет нечем.
PROTECTED_ROLE = "admin"


def parse_tabs(value: str | None) -> list[str]:
    """Разобрать список вкладок, отбросив коды несуществующих.

    Вкладку могли переименовать или убрать из терминала, а в роли она осталась.
    Молча пропускаем такую: показывать в настройках роли строку, которой нет в
    интерфейсе, значит предлагать настроить несуществующее.
    """
    if not value:
        return []
    seen = {item.strip() for item in value.split(",") if item.strip()}
    return [code for code in TAB_CODES if code in seen]


def dump_tabs(codes: Iterable[str]) -> str:
    """Собрать список вкладок обратно в строку, сохранив порядок интерфейса."""
    wanted = set(codes)
    return ",".join(code for code in TAB_CODES if code in wanted)


def ensure_builtin_roles(session: Session) -> int:
    """Завести встроенные роли, если их ещё нет.

    Всем трём открыты все вкладки: до появления этой настройки терминал
    показывал каждому всё, и молча отобрать половину экранов при обновлении
    было бы неприятным сюрпризом. Сузить состав — осознанное действие.
    """
    existing = {
        name for (name,) in session.execute(select(Role.name)).all()
    }
    created = 0
    for item in BUILTIN_ROLES:
        if item["name"] in existing:
            continue
        session.add(
            Role(
                name=item["name"],
                title=item["title"],
                level=item["level"],
                tabs=dump_tabs(TAB_CODES),
                builtin=True,
                comment=item["comment"],
            )
        )
        created += 1
    if created:
        session.commit()
    return created


def role_by_name(session: Session, name: str) -> Role | None:
    return session.execute(
        select(Role).where(Role.name == name)
    ).scalar_one_or_none()


def resolve(session: Session, role_name: str) -> dict[str, Any]:
    """Уровень прав и вкладки роли.

    Незнакомая роль трактуется как «просмотр без вкладок»: так ведёт себя
    учётная запись, чью роль удалили, — она не получает лишнего, но и терминал
    не роняет.
    """
    if role_name == PROTECTED_ROLE:
        # Администратору вкладки не отнимаются даже при испорченной строке
        # в базе: иначе единственный вход в настройки может закрыться
        return {"level": "admin", "tabs": list(TAB_CODES), "title": "Администратор"}

    role = role_by_name(session, role_name)
    if role is None:
        return {"level": "viewer", "tabs": [], "title": role_name}
    level = role.level if role.level in LEVELS else "viewer"
    return {"level": level, "tabs": parse_tabs(role.tabs), "title": role.title}


def normalise_path(path: str) -> str:
    """Привести путь к виду, в котором его сравнивают с картой.

    Сравнение строк без этого обманывается записью того же адреса другими
    буквами: ``/api//bonds``, ``/api/./bonds``, ``/API/bonds``. Сейчас такие
    запросы до обработчика не доходят — маршрутизатор отвечает на них 404, —
    то есть дыры нет. Но это защита по случайному стечению: стоит появиться
    маршруту, который такую запись стерпит, и проверка раздела молча
    пропустит. Нормализуем сами, чтобы не зависеть от чужого поведения.
    """
    lowered = (path or "").lower()
    parts: list[str] = []
    for segment in lowered.split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/" + "/".join(parts)


def path_allowed(path: str, tabs: Sequence[str]) -> bool:
    """Открыт ли путь API человеку с таким набором вкладок."""
    path = normalise_path(path)
    if path.startswith(ALWAYS_ALLOWED):
        return True
    for prefix, sections in _SORTED_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return bool(sections & set(tabs))
    # Не попавшее в карту разрешаем: см. модуль
    return True


def section_of(path: str) -> frozenset[str] | None:
    """Разделы, к которым относится путь, — для понятного текста отказа."""
    path = normalise_path(path)
    if path.startswith(ALWAYS_ALLOWED):
        return None
    for prefix, sections in _SORTED_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            return sections
    return None


# ----------------------------------------------------------------------
# Управление ролями
# ----------------------------------------------------------------------
def list_roles(session: Session) -> list[dict[str, Any]]:
    """Роли со списком вкладок и числом учётных записей на каждой."""
    counts: dict[str, int] = {}
    for (name,) in session.execute(select(User.role).where(User.active.is_(True))).all():
        counts[name] = counts.get(name, 0) + 1

    roles = list(session.execute(select(Role).order_by(Role.id)).scalars())
    return [
        {
            "id": role.id,
            "name": role.name,
            "title": role.title,
            "level": role.level,
            "level_title": LEVEL_TITLES.get(role.level, role.level),
            "tabs": list(TAB_CODES) if role.name == PROTECTED_ROLE else parse_tabs(role.tabs),
            "builtin": role.builtin,
            #: Состав вкладок администратора не редактируется — им же и
            #: чинят всё остальное
            "locked": role.name == PROTECTED_ROLE,
            "comment": role.comment,
            "users": counts.get(role.name, 0),
        }
        for role in roles
    ]


def save_role(
    session: Session,
    *,
    name: str,
    title: str,
    level: str,
    tabs: Sequence[str],
    comment: str | None = None,
) -> Role:
    """Завести роль или обновить существующую."""
    name = name.strip().lower()
    if not name:
        raise ValueError("Не указан код роли")
    if level not in LEVELS:
        raise ValueError(f"Неизвестный уровень прав: {level}")

    unknown = sorted(set(tabs) - set(TAB_CODES))
    if unknown:
        raise ValueError("Неизвестные вкладки: " + ", ".join(unknown))

    role = role_by_name(session, name)
    if role is None:
        role = Role(name=name, builtin=False)
        session.add(role)
    elif role.name == PROTECTED_ROLE:
        # Уровень и вкладки администратора не трогаем, название — можно
        role.title = title.strip() or role.title
        role.comment = comment
        session.commit()
        return role

    role.title = title.strip() or name
    role.level = level
    role.tabs = dump_tabs(tabs)
    role.comment = comment
    session.commit()
    session.refresh(role)
    return role


def delete_role(session: Session, name: str) -> None:
    """Удалить роль. Встроенную и занятую — нельзя."""
    role = role_by_name(session, name)
    if role is None:
        raise ValueError("Роль не найдена")
    if role.builtin:
        raise ValueError("Встроенную роль удалить нельзя")

    busy = session.execute(
        select(User.login).where(User.role == name, User.active.is_(True))
    ).scalars().first()
    if busy is not None:
        # Иначе учётная запись осталась бы с ролью, которой нет, и человек
        # при следующем входе не увидел бы ни одной вкладки
        raise ValueError(
            f"Роль занята: её носит {busy}. Переведите на другую и повторите."
        )

    session.delete(role)
    session.commit()


def tabs_catalog() -> list[dict[str, str]]:
    """Список вкладок для выбора в настройках роли."""
    return [{"code": code, "title": title} for code, title in TABS]
