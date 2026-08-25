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
    # Память под кэш страниц и отображение файла: на медленном диске это
    # решает больше, чем скорость самого диска
    sqlite_cache_mb: int = 64
    sqlite_mmap_mb: int = 256
    # Сколько дней хранить внутридневные срезы котировок. Дневная история
    # живёт в таблице баров и не страдает, а срезы позавчерашнего дня не
    # нужны никому — зато замедляют каждую страницу.
    #
    # Арифметика, из-за которой это число важно: снимок раз в пять минут по
    # четырём тысячам бумаг — это миллион строк в сутки. При хранении неделю
    # таблица дорастает до семи миллионов, и отбор «последняя котировка по
    # каждой бумаге», через который проходит почти каждая страница, начинает
    # занимать секунду с лишним. Двух дней хватает и графику хода торгов, и
    # утреннему сравнению со вчера.
    quote_retention_days: int = 2
    # Удаляем порциями: одна большая транзакция на медленном диске
    # заблокировала бы терминал на минуты
    quote_prune_batch: int = 50_000
    # Потолок за один заход. Должен с запасом перекрывать суточный прирост,
    # иначе уборка отстаёт навсегда: при пределе в 50 тысяч и приросте в
    # миллион строк таблица растёт, сколько её ни чисти
    quote_prune_max_rows: int = 400_000

    # Снимать ли срез только в часы работы биржи. Ночью и в выходные цены не
    # меняются, а снимки идут — это больше половины всех строк в таблице, и
    # ни одна из них ничего не добавляет. Отключите, если собираете данные
    # с площадки, которая торгует круглосуточно.
    collect_in_trading_hours_only: bool = True
    # Границы по московскому времени: утренние торги начинаются в 6:50,
    # вечерняя сессия заканчивается в 23:50. Берём с запасом в обе стороны
    trading_hours_start_msk: int = 6
    trading_hours_end_msk: int = 24

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
    #: Сторонние адреса, которым разрешено обращаться к API из браузера.
    #: Пусто — кросс-доменные запросы запрещены совсем, и это верно для
    #: обычной установки: интерфейс отдаётся с того же адреса, что и API.
    #: Заполнять только если интерфейс вынесен на отдельный домен.
    #: Через запятую: TREASURY_CORS_ORIGINS=https://terminal.example.ru
    cors_origins: str = ""

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

    @property
    def cors_origin_list(self) -> tuple[str, ...]:
        return tuple(
            item.strip() for item in self.cors_origins.split(",") if item.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
