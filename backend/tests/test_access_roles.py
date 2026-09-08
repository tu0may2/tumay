"""Роли, состав вкладок и запрет на чужие разделы.

Скрытая вкладка обязана быть закрыта и на сервере: спрятать кнопку — не
защита, адрес запроса виден в браузере у любого. Здесь проверяется именно это,
а заодно то, ради чего настройка вообще заводится, — что человека нельзя
случайно запереть снаружи собственного терминала.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Role, User
from app.services import access
from app.services.auth import create_user


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active:
        access.ensure_builtin_roles(active)
        yield active


# ----------------------------------------------------------------------
# Справочник вкладок
# ----------------------------------------------------------------------
def _markup_tabs() -> list[tuple[str, str]]:
    """Вкладки, как они объявлены в разметке терминала."""
    import re
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    ).read_text(encoding="utf-8")
    nav = re.search(r'<nav class="tabs".*?</nav>', html, re.S)
    assert nav, "в разметке не нашлась полоса вкладок"
    return re.findall(
        r'data-view="([a-z]+)"[^>]*>([^<]+)</button>', nav.group(0)
    )


class TestCatalog:
    def test_catalog_matches_the_markup(self):
        """Список вкладок и полоса в разметке — одно и то же.

        По списку в коде и рисуется выбор вкладок в роли, и проверяется
        доступ на сервере. Разойдись он с разметкой — и роль настраивала бы
        вкладку, которой нет, либо не видела бы существующую.
        """
        assert [
            (code, title.strip()) for code, title in _markup_tabs()
        ] == list(access.TABS)

    def test_tab_codes_are_unique(self):
        assert len(access.TAB_CODES) == len(set(access.TAB_CODES))

    def test_every_tab_has_a_title(self):
        for code in access.TAB_CODES:
            assert access.TAB_TITLES[code].strip(), code

    def test_parse_keeps_interface_order(self):
        """Порядок вкладок в роли — всегда порядок интерфейса, не ввода."""
        parsed = access.parse_tabs("sources,calendar,bonds")
        assert parsed == ["calendar", "bonds", "sources"]

    def test_parse_drops_unknown_codes(self):
        """Вкладку могли убрать из терминала, а в роли она осталась."""
        assert access.parse_tabs("calendar,ного-такой-нет") == ["calendar"]

    def test_parse_handles_empty(self):
        assert access.parse_tabs("") == []
        assert access.parse_tabs(None) == []

    def test_dump_and_parse_round_trip(self):
        codes = ["bonds", "calendar", "admin"]
        assert access.parse_tabs(access.dump_tabs(codes)) == [
            "calendar", "bonds", "admin"
        ]


# ----------------------------------------------------------------------
# Разбор путей
# ----------------------------------------------------------------------
class TestPathRules:
    def test_login_is_open_to_everyone(self):
        assert access.path_allowed("/api/auth/login", [])
        assert access.path_allowed("/api/health", [])

    def test_ticker_and_portfolio_picker_stay_open(self):
        """Шапка есть на каждой вкладке — пустой она выглядит поломкой."""
        assert access.path_allowed("/api/overview", [])
        assert access.path_allowed("/api/portfolio/names", [])

    def test_hidden_section_is_closed(self):
        assert not access.path_allowed("/api/bonds/analysis", ["calendar"])

    def test_visible_section_is_open(self):
        assert access.path_allowed("/api/bonds/analysis", ["bonds"])

    def test_longer_prefix_wins(self):
        """«/api/cash/calendar» не должен утаскиваться правилом «/api/cash»."""
        assert access.path_allowed("/api/cash/matrix", ["calendar"])
        assert not access.path_allowed("/api/cash/flows", ["calendar"])
        assert access.path_allowed("/api/cash/flows", ["accounts"])

    def test_path_shared_by_two_sections(self):
        """Карточка бумаги открывается и из «Облигаций», и из «Инструментов»."""
        for tabs in (["bonds"], ["instruments"], ["ratios"]):
            assert access.path_allowed("/api/instruments/SBER", tabs), tabs
        assert not access.path_allowed("/api/instruments/SBER", ["accounts"])

    def test_prefix_does_not_leak_to_a_similar_path(self):
        """«/api/calendar» — корпоративные события, а не платёжный календарь."""
        assert not access.path_allowed("/api/calendar", ["calendar"])
        assert access.path_allowed("/api/calendar", ["signals"])

    def test_unmapped_path_is_allowed(self):
        """Забытый в карте обработчик не должен ломаться у половины людей."""
        assert access.path_allowed("/api/чего-то-нового", ["calendar"])

    def test_no_tabs_means_no_sections(self):
        assert not access.path_allowed("/api/portfolio", [])
        assert access.path_allowed("/api/auth/me", [])

    def test_section_of_names_the_sections(self):
        assert access.section_of("/api/bonds/filters") == frozenset({"bonds"})
        assert access.section_of("/api/health") is None


# ----------------------------------------------------------------------
# Встроенные роли
# ----------------------------------------------------------------------
class TestBuiltinRoles:
    def test_three_roles_are_created(self, session):
        names = {role.name for role in session.query(Role)}
        assert names == {"viewer", "trader", "admin"}

    def test_builtin_roles_see_everything_by_default(self, session):
        """Терминал показывал всем всё — молча отнять половину нельзя."""
        for role in session.query(Role):
            assert access.parse_tabs(role.tabs) == list(access.TAB_CODES), role.name

    def test_seeding_twice_changes_nothing(self, session):
        access.save_role(
            session, name="viewer", title="Просмотр", level="viewer", tabs=["calendar"]
        )
        assert access.ensure_builtin_roles(session) == 0
        assert access.resolve(session, "viewer")["tabs"] == ["calendar"]

    def test_admin_keeps_every_tab_whatever_is_stored(self, session):
        """Иначе администратор способен закрыть себе вход в настройки."""
        role = access.role_by_name(session, "admin")
        role.tabs = ""
        session.commit()
        assert access.resolve(session, "admin")["tabs"] == list(access.TAB_CODES)

    def test_admin_tabs_cannot_be_narrowed(self, session):
        access.save_role(
            session, name="admin", title="Администратор", level="viewer", tabs=[]
        )
        resolved = access.resolve(session, "admin")
        assert resolved["tabs"] == list(access.TAB_CODES)
        assert resolved["level"] == "admin"

    def test_admin_is_listed_as_locked(self, session):
        admin = next(r for r in access.list_roles(session) if r["name"] == "admin")
        assert admin["locked"] is True
        assert admin["tabs"] == list(access.TAB_CODES)


# ----------------------------------------------------------------------
# Свои роли
# ----------------------------------------------------------------------
class TestCustomRoles:
    def test_create_and_resolve(self, session):
        access.save_role(
            session, name="treasurer", title="Казначей", level="trader",
            tabs=["calendar", "accounts"],
        )
        resolved = access.resolve(session, "treasurer")
        assert resolved == {
            "level": "trader", "tabs": ["calendar", "accounts"], "title": "Казначей"
        }

    def test_level_and_tabs_are_independent(self, session):
        """Ради этого они и разведены: «сделки заводит, облигации не видит»."""
        access.save_role(
            session, name="dealer", title="Дилер", level="trader", tabs=["calendar"]
        )
        resolved = access.resolve(session, "dealer")
        assert resolved["level"] == "trader"
        assert not access.path_allowed("/api/bonds", resolved["tabs"])

    def test_saving_again_replaces_the_tabs(self, session):
        access.save_role(session, name="x", title="X", level="viewer", tabs=["bonds"])
        access.save_role(session, name="x", title="X", level="viewer", tabs=["calendar"])
        assert access.resolve(session, "x")["tabs"] == ["calendar"]

    def test_unknown_tab_is_rejected(self, session):
        with pytest.raises(ValueError, match="Неизвестные вкладки"):
            access.save_role(
                session, name="x", title="X", level="viewer", tabs=["луна"]
            )

    def test_unknown_level_is_rejected(self, session):
        with pytest.raises(ValueError, match="уровень"):
            access.save_role(
                session, name="x", title="X", level="король", tabs=["calendar"]
            )

    def test_name_is_normalised(self, session):
        access.save_role(
            session, name="  Treasurer  ", title="Казначей", level="viewer", tabs=[]
        )
        assert access.role_by_name(session, "treasurer") is not None

    def test_deleted_role_leaves_no_tabs(self, session):
        """Учётная запись с несуществующей ролью не должна видеть лишнего."""
        resolved = access.resolve(session, "такой-роли-нет")
        assert resolved["tabs"] == []
        assert resolved["level"] == "viewer"

    def test_builtin_role_cannot_be_deleted(self, session):
        with pytest.raises(ValueError, match="Встроенную"):
            access.delete_role(session, "viewer")

    def test_role_in_use_cannot_be_deleted(self, session):
        """Иначе человек при следующем входе не увидел бы ни одной вкладки."""
        access.save_role(session, name="x", title="X", level="viewer", tabs=[])
        create_user(session, login="petrov", password="parol-petrova", role="x")
        with pytest.raises(ValueError, match="занята"):
            access.delete_role(session, "x")

    def test_free_role_is_deleted(self, session):
        access.save_role(session, name="x", title="X", level="viewer", tabs=[])
        access.delete_role(session, "x")
        assert access.role_by_name(session, "x") is None

    def test_disabled_user_does_not_hold_a_role(self, session):
        access.save_role(session, name="x", title="X", level="viewer", tabs=[])
        user = create_user(session, login="ivanov", password="parol-ivanova", role="x")
        user.active = False
        session.commit()
        access.delete_role(session, "x")
        assert access.role_by_name(session, "x") is None

    def test_user_cannot_take_an_unknown_role(self, session):
        with pytest.raises(ValueError, match="Неизвестная роль"):
            create_user(session, login="x", password="parol-polzovatelya", role="нет")

    def test_user_can_take_a_custom_role(self, session):
        access.save_role(session, name="x", title="X", level="viewer", tabs=[])
        assert create_user(
            session, login="sidorov", password="parol-sidorova", role="x"
        ).role == "x"

    def test_listing_counts_active_users(self, session):
        access.save_role(session, name="x", title="X", level="viewer", tabs=[])
        create_user(session, login="a", password="parol-pervogo", role="x")
        user = create_user(session, login="b", password="parol-vtorogo", role="x")
        user.active = False
        session.commit()
        row = next(r for r in access.list_roles(session) if r["name"] == "x")
        assert row["users"] == 1
