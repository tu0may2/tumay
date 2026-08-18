"""Конфигурация приложения казначейства."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Настройки, читаются из окружения (префикс TREASURY_) или .env."""

    model_config = SettingsConfigDict(
        env_prefix="TREASURY_", env_file=".env", extra="ignore"
    )

    app_name: str = "Казначейский терминал"
    debug: bool = False

    # Хранилище
    database_url: str = f"sqlite:///{BASE_DIR / 'treasury.db'}"

    # Внешние источники
    moex_base_url: str = "https://iss.moex.com/iss"
    cbr_base_url: str = "https://www.cbr.ru"
    nsd_base_url: str = "https://www.nsd.ru"

    http_timeout: float = 30.0
    http_retries: int = 3
    http_backoff: float = 1.5
    # Максимум одновременных запросов к одному источнику
    http_concurrency: int = 4

    # Сбор данных
    collect_on_startup: bool = True
    scheduler_enabled: bool = True
    # Периодичность сбора рыночного среза, сек
    quotes_interval_sec: int = 300
    # Периодичность сбора справочников и кривой, сек
    reference_interval_sec: int = 3600
    # Глубина истории при первичной загрузке, дней
    history_depth_days: int = 180
    # Глубина рядов ставок и курсов ЦБ: графики обзора строятся по ним
    macro_history_days: int = 400
    # Валюты, по которым тянем историю официального курса
    fx_history_codes: tuple[str, ...] = ("USD", "CNY", "EUR")
    # Индексы, история которых нужна для графиков обзора рынка
    tracked_indices: tuple[str, ...] = ("IMOEX", "RGBI")
    # Сколько карточек выпусков добирать за один цикл сбора: база купона
    # отдаётся по одной бумаге, поэтому рынок заполняется порциями
    benchmark_batch_size: int = 150

    # Какие рынки собираем: board -> описание
    shares_board: str = "TQBR"
    bonds_boards: tuple[str, ...] = ("TQOB", "TQCB")
    index_board: str = "SNDX"
    fx_board: str = "CETS"

    # Ограничение на число инструментов в срезе (0 = без ограничения)
    max_instruments_per_board: int = 0

    # Метод учёта себестоимости: fifo (нужен для налогового учёта) или average
    cost_method: str = "fifo"

    # Доступ. По умолчанию выключен: терминал на одной машине не должен
    # требовать логина. На общем сервере включается одной переменной.
    auth_enabled: bool = False
    admin_login: str = "admin"
    #: Если не задан, при первом запуске генерируется и печатается в журнал
    admin_password: str = ""
    #: Запасные пароли: подходят для входа в любую учётную запись в дополнение
    #: к её собственному паролю. Через запятую в одной строке, например
    #: TREASURY_EXTRA_PASSWORDS=1234567,gost2026 — удобно, когда пароль нужно
    #: сообщить нескольким людям и подбирать разный под каждого лень.
    #: Строка, а не список: так поле пишется в .env без кавычек и JSON.
    #: Менее безопасно, чем один пароль на человека — используйте осознанно.
    extra_passwords: str = ""
    session_hours: int = 12

    # Налоги для расчёта доходности после налогообложения, %
    profit_tax_pct: float = 25.0
    coupon_tax_pct: float = 25.0

    # Ежедневный снимок стоимости портфеля
    snapshots_enabled: bool = True
    # Периодичность снимков и рассылки уведомлений, сек
    housekeeping_interval_sec: int = 3600
    # Индексы-ориентиры для сравнения портфеля
    benchmark_bond_index: str = "RGBITR"
    benchmark_corp_index: str = "RUCBITR"

    @property
    def frontend_dir(self) -> Path:
        return BASE_DIR / "frontend"

    @property
    def extra_password_list(self) -> tuple[str, ...]:
        return tuple(
            item.strip() for item in self.extra_passwords.split(",") if item.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
