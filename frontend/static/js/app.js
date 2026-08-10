/* Казначейский терминал — логика интерфейса. */
(function () {
  'use strict';

  const state = {
    view: 'overview',
    loaded: {},
    portfolioName: null,
    exportMode: 'by_date',
    exportReady: false,
    exportParamsLoaded: false,
    bondFiltersLoaded: false,
    bondMode: 'screen',
    analysisLoaded: false,
    screens: [],
    limitKinds: [],
  };

  const THEME_KEY = 'treasury-theme';

  /** Применить тему и запомнить выбор между сессиями. */
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const button = document.getElementById('btn-theme');
    if (button) {
      // Ёлка на тёмной теме, снежинка на светлой — что включится по нажатию
      button.textContent = theme === 'light' ? '🌲' : '❄️';
      button.title = theme === 'light' ? 'Тёмная тема' : 'Светлая тема';
    }
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch (error) { /* приватный режим — просто не запоминаем */ }
  }

  function initialTheme() {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === 'light' || saved === 'dark') return saved;
    } catch (error) { /* хранилище недоступно */ }
    // Иначе следуем настройке системы
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
      ? 'light'
      : 'dark';
  }

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  /**
   * Безопасная привязка обработчика.
   * Все кнопки и фильтры связываются в одной функции инициализации: если хоть
   * один элемент отсутствует, обычный addEventListener бросает исключение и
   * обрывает её — тогда перестают работать сразу все элементы, включая тему.
   * Здесь отсутствие элемента только пишется в консоль.
   */
  function on(selector, event, handler) {
    const node = typeof selector === 'string' ? $(selector) : selector;
    if (!node) {
      console.warn('Элемент не найден, обработчик не привязан:', selector);
      return false;
    }
    node.addEventListener(event, handler);
    return true;
  }

  // ------------------------------------------------------------------
  // Вспомогательные элементы
  // ------------------------------------------------------------------
  function toast(message, isError) {
    const node = $('#toast');
    node.textContent = message;
    node.className = 'toast' + (isError ? ' toast--error' : '');
    node.hidden = false;
    clearTimeout(node._timer);
    node._timer = setTimeout(() => { node.hidden = true; }, 4200);
  }

  function loading(container) {
    if (container) container.innerHTML = '<div class="empty">Загрузка…</div>';
  }

  function failure(container, error) {
    if (container) {
      container.innerHTML = `<div class="empty">Не удалось загрузить: ${fmt.esc(error.message)}</div>`;
    }
  }

  /** Ячейка с кодом и названием бумаги. */
  function secCell(row) {
    return `<div class="sec">
      <span class="sec__code">${fmt.esc(row.secid)}</span>
      <span class="sec__name">${fmt.esc(row.name || '')}</span>
    </div>`;
  }

  /** Шкала ликвидности 0–100. */
  function liquidityCell(value) {
    if (!fmt.isNum(value)) return '<span class="dim">—</span>';
    const hue = value >= 70 ? 'var(--up)' : value >= 40 ? 'var(--warn)' : 'var(--down)';
    return `<div class="meter">
      <span class="meter__val">${fmt.num(value, 0)}</span>
      <span class="meter__bar"><span class="meter__fill" style="width:${value}%;background:${hue}"></span></span>
    </div>`;
  }

  function changeCell(value) {
    return `<span class="${fmt.trendClass(value)}">${fmt.signedPct(value)}</span>`;
  }

  /**
   * Премия к безрисковой кривой.
   * Значения в тысячи базисных пунктов — это не доходность, а признак того,
   * что рынок закладывает дефолт: показываем словом, а не числом, чтобы такую
   * бумагу не приняли за выгодную покупку.
   */
  function premiumCell(value) {
    if (!fmt.isNum(value)) return '<span class="dim">—</span>';
    if (value > 5000) {
      return '<span class="badge badge--down" title="Премия свыше 5000 бп: рынок оценивает выпуск как проблемный">дефолтный риск</span>';
    }
    const cls = value > 1000 ? 'badge--down' : value > 300 ? 'badge--warn' : 'badge--up';
    const mark = value > 1000 ? ' ⚠' : '';
    return `<span class="badge ${cls}">${fmt.bp(value)}${mark}</span>`;
  }

  /** Цвет из текущей темы — чтобы графики перекрашивались вместе с ней. */
  function themeColor(name, fallback) {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    return value || fallback;
  }

  /**
   * Сборка таблицы.
   * columns: [{ title, render(row), className }]
   */
  function renderTable(container, columns, rows, options = {}) {
    // Страховка от перепутанных аргументов: без неё забытый список строк
    // выглядит как пустая таблица и молча скрывает данные.
    if (rows !== null && rows !== undefined && !Array.isArray(rows)) {
      console.error('renderTable: ожидался массив строк, получено', rows);
      rows = [];
    }
    if (!rows || !rows.length) {
      container.innerHTML = `<div class="empty">${options.emptyMessage || 'Нет данных'}</div>`;
      return;
    }

    const head = columns.map((col) => `<th class="${col.className || ''}">${col.title}</th>`).join('');
    const body = rows
      .map((row, index) => {
        const cells = columns
          .map((col) => `<td class="${col.className || ''}">${col.render(row, index)}</td>`)
          .join('');
        const clickable = options.onRowClick ? ' class="clickable"' : '';
        const key = options.rowKey ? ` data-key="${fmt.esc(options.rowKey(row))}"` : '';
        return `<tr${clickable}${key}>${cells}</tr>`;
      })
      .join('');

    container.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;

    if (options.onRowClick) {
      container.querySelectorAll('tbody tr').forEach((tr) => {
        tr.addEventListener('click', (event) => {
          // Кнопки внутри строки обрабатывают клик сами
          if (event.target.closest('button')) return;
          options.onRowClick(tr.dataset.key);
        });
      });
    }
  }

  // ------------------------------------------------------------------
  // Обзор рынка
  // ------------------------------------------------------------------
  async function renderOverview() {
    const kpiGrid = $('#kpi-grid');
    loading(kpiGrid);

    let overview;
    try {
      overview = await api.overview();
    } catch (error) {
      return failure(kpiGrid, error);
    }

    renderTicker(overview);

    const usd = overview.fx.find((item) => item.code === 'USD');
    const cny = overview.fx.find((item) => item.code === 'CNY');
    const imoex = overview.indices.find((item) => item.secid === 'IMOEX');
    const rgbi = overview.indices.find((item) => item.secid === 'RGBI');

    const cards = [
      {
        label: 'Ключевая ставка ЦБ',
        value: overview.key_rate ? fmt.pct(overview.key_rate.value, 2) : '—',
        meta: overview.key_rate ? `на ${fmt.date(overview.key_rate.date)}` : 'нет данных',
      },
      {
        label: 'RUONIA',
        value: overview.ruonia ? fmt.pct(overview.ruonia.value, 2) : '—',
        meta: overview.ruonia ? `на ${fmt.date(overview.ruonia.date)}` : 'нет данных',
      },
      {
        label: 'Индекс МосБиржи',
        value: imoex ? fmt.num(imoex.value, 2) : '—',
        meta: imoex ? `<span class="${fmt.trendClass(imoex.change_pct)}">${fmt.signedPct(imoex.change_pct)}</span>` : '',
      },
      {
        label: 'RGBI (гособлигации)',
        value: rgbi ? fmt.num(rgbi.value, 2) : '—',
        meta: rgbi ? `<span class="${fmt.trendClass(rgbi.change_pct)}">${fmt.signedPct(rgbi.change_pct)}</span>` : '',
      },
      {
        label: 'USD / RUB',
        value: usd ? fmt.num(usd.value, 4) : '—',
        meta: usd ? `ЦБ на ${fmt.date(usd.date)}` : '',
      },
      {
        label: 'CNY / RUB',
        value: cny ? fmt.num(cny.value, 4) : '—',
        meta: cny ? `ЦБ на ${fmt.date(cny.date)}` : '',
      },
      {
        label: 'Оборот за сессию',
        value: fmt.money(overview.total_turnover) + ' ₽',
        meta: `${fmt.int(overview.instruments_traded)} из ${fmt.int(overview.instruments_total)} бумаг с оборотом`,
      },
    ];

    kpiGrid.innerHTML = cards
      .map((card) => `
        <div class="kpi">
          <div class="kpi__label">${card.label}</div>
          <div class="kpi__value">${card.value}</div>
          <div class="kpi__meta">${card.meta || ''}</div>
        </div>`)
      .join('');

    $('#status').textContent = overview.updated_at
      ? `срез ${fmt.dateTime(overview.updated_at)}`
      : 'нет данных';

    renderCurve(overview.curve);
    renderRatesChart();
    renderMovers();
    renderTopLiquid();
  }

  function renderTicker(overview) {
    const items = [];
    if (overview.key_rate) {
      items.push({ label: 'Ключевая', value: fmt.pct(overview.key_rate.value, 2) });
    }
    overview.indices.forEach((index) => {
      items.push({
        label: index.secid,
        value: fmt.num(index.value, 2),
        change: index.change_pct,
      });
    });
    overview.fx.forEach((rate) => {
      items.push({ label: rate.code, value: fmt.num(rate.value, 2) });
    });

    $('#ticker').innerHTML = items
      .map((item) => `
        <span class="ticker__item">
          <span class="ticker__label">${fmt.esc(item.label)}</span>
          <span class="ticker__value">${item.value}</span>
          ${item.change != null ? `<span class="ticker__chg ${fmt.trendClass(item.change)}">${fmt.signedPct(item.change)}</span>` : ''}
        </span>`)
      .join('');
  }

  function renderCurve(curve) {
    const container = $('#curve-chart');
    if (!curve || !curve.points || !curve.points.length) {
      return charts.empty(container, 'Кривая не загружена');
    }
    $('#curve-date').textContent = `на ${fmt.date(curve.curve_date)}`;

    charts.lineChart(
      container,
      [{
        name: 'КБД',
        color: themeColor('--accent', '#3f9d6d'),
        points: curve.points.map((point) => ({
          x: point.period_years,
          y: point.value,
          label: `${point.period_years} ${fmt.plural(point.period_years, 'год', 'года', 'лет')}: ${fmt.pct(point.value)}`,
        })),
      }],
      {
        height: 220,
        yFormat: (v) => fmt.num(v, 1) + '%',
        xFormat: (v) => (v < 1 ? `${v}` : `${Math.round(v)}л`),
        dots: true,
      }
    );
  }

  async function renderRatesChart() {
    const container = $('#rates-chart');
    try {
      const [keyRate, ruonia] = await Promise.all([
        api.rates({ code: 'KEY_RATE', days: 365 }),
        api.rates({ code: 'RUONIA', days: 365 }),
      ]);

      const toPoints = (rows) =>
        rows.map((row) => ({
          x: new Date(row.date).getTime(),
          y: row.value,
          label: `${fmt.date(row.date)}: ${fmt.pct(row.value)}`,
        }));

      charts.lineChart(
        container,
        [
          { name: 'Ключевая ставка', color: themeColor('--accent', '#3f9d6d'), points: toPoints(keyRate) },
          { name: 'RUONIA', color: themeColor('--warn', '#d9a441'), points: toPoints(ruonia) },
        ],
        {
          height: 220,
          area: false,
          yFormat: (v) => fmt.num(v, 1) + '%',
          xFormat: (v) => fmt.dateShort(new Date(v)),
          emptyMessage: 'Ставки не загружены',
        }
      );
    } catch (error) {
      failure(container, error);
    }
  }

  async function renderMovers() {
    try {
      const movers = await api.movers({ limit: 6, min_turnover: 5000000 });
      const columns = [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'Цена', className: 'num', render: (row) => fmt.price(row.last) },
        { title: 'Изм.', className: 'num', render: (row) => changeCell(row.change_pct) },
        { title: 'Оборот', className: 'num', render: (row) => fmt.money(row.turnover) },
      ];
      renderTable($('#gainers'), columns, movers.gainers, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Нет данных по росту',
      });
      renderTable($('#losers'), columns, movers.losers, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Нет данных по падению',
      });
    } catch (error) {
      failure($('#gainers'), error);
    }
  }

  async function renderTopLiquid() {
    const container = $('#top-liquid');
    loading(container);
    try {
      const data = await api.instruments({ kind: ['share'], sort_by: 'turnover', limit: 15 });
      renderTable(container, [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'Цена', className: 'num', render: (row) => fmt.price(row.last) },
        { title: 'Изм.', className: 'num', render: (row) => changeCell(row.change_pct) },
        { title: 'Оборот, ₽', className: 'num', render: (row) => fmt.money(row.turnover) },
        { title: 'Объём, шт', className: 'num', render: (row) => fmt.int(row.volume) },
        { title: 'Сделок', className: 'num', render: (row) => fmt.int(row.num_trades) },
        { title: 'Спред', className: 'num', render: (row) => fmt.pct(row.spread_pct, 3) },
        { title: 'Ликвидность', className: 'num', render: (row) => liquidityCell(row.liquidity_score) },
      ], data.items, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
      });
    } catch (error) {
      failure(container, error);
    }
  }

  // ------------------------------------------------------------------
  // Витрина инструментов
  // ------------------------------------------------------------------
  async function renderInstruments() {
    const container = $('#instruments-table');
    loading(container);

    const kind = $('#f-kind').value;
    const params = {
      search: $('#f-search').value.trim(),
      min_turnover: (parseFloat($('#f-turnover').value) || 0) * 1e6,
      min_liquidity: parseFloat($('#f-liquidity').value) || 0,
      sort_by: $('#f-sort').value,
      limit: 200,
    };
    if (kind) params.kind = [kind];

    try {
      const data = await api.instruments(params);
      $('#instruments-count').textContent =
        `${fmt.int(data.total)} ${fmt.plural(data.total, 'бумага', 'бумаги', 'бумаг')}`;

      renderTable(container, [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'ISIN', render: (row) => `<span class="dim" style="font-family:var(--mono);font-size:11px">${fmt.esc(row.isin || '—')}</span>` },
        { title: 'Цена', className: 'num', render: (row) => fmt.price(row.last) },
        { title: 'Изм.', className: 'num', render: (row) => changeCell(row.change_pct) },
        { title: 'Оборот, ₽', className: 'num', render: (row) => fmt.money(row.turnover) },
        { title: 'Объём, шт', className: 'num', render: (row) => fmt.int(row.volume) },
        { title: 'Сделок', className: 'num', render: (row) => fmt.int(row.num_trades) },
        { title: 'Спред', className: 'num', render: (row) => fmt.pct(row.spread_pct, 3) },
        { title: 'Ликвидность', className: 'num', render: (row) => liquidityCell(row.liquidity_score) },
      ], data.items, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Ничего не найдено — ослабьте фильтры',
      });
    } catch (error) {
      failure(container, error);
    }
  }

  // ------------------------------------------------------------------
  // Анализ облигаций
  // ------------------------------------------------------------------
  /** Собрать параметры запроса из панели фильтров. */
  function analysisParams() {
    const num = (selector) => {
      const value = parseFloat($(selector).value);
      return isNaN(value) ? null : value;
    };
    const pick = (selector) => $(selector).value || null;
    const bool = (selector) => {
      const value = $(selector).value;
      return value === '' ? null : value;
    };

    const params = {
      search: $('#a-search').value.trim() || null,
      min_yield: num('#a-minyield'),
      max_yield: num('#a-maxyield'),
      min_duration_years: num('#a-mindur'),
      max_duration_years: num('#a-maxdur'),
      maturity_from: pick('#a-matfrom'),
      maturity_to: pick('#a-matto'),
      min_turnover: (num('#a-turnover') || 0) * 1e6,
      max_risk_score: num('#a-risk'),
      has_offer: bool('#a-offer'),
      has_amortization: bool('#a-amort'),
      sort_by: $('#a-sort').value,
      limit: 300,
    };
    const coupon = pick('#a-coupon');
    if (coupon) params.coupon_type = [coupon];
    const level = pick('#a-level');
    if (level) params.list_level = [level];
    const currency = pick('#a-currency');
    if (currency) params.currency = [currency];
    return params;
  }

  const RISK_BADGES = {
    'низкий': 'badge--up',
    'умеренный': 'badge--accent',
    'повышенный': 'badge--warn',
    'высокий': 'badge--down',
  };

  async function renderAnalysis() {
    const container = $('#analysis-table');
    loading(container);

    try {
      if (!state.bondFiltersLoaded) {
        state.bondFiltersLoaded = true;
        const options = await api.bondFilters();
        const select = $('#a-currency');
        options.currencies.forEach((code) => {
          const option = document.createElement('option');
          option.value = code;
          option.textContent = code;
          select.appendChild(option);
        });
      }

      const data = await api.bondAnalysis(analysisParams());
      state.analysisLoaded = true;
      $('#analysis-count').textContent =
        `${fmt.int(data.total)} ${fmt.plural(data.total, 'выпуск', 'выпуска', 'выпусков')}` +
        (data.curve_date ? ` · КБД на ${fmt.date(data.curve_date)}` : '');

      renderTable(container, [
        { title: 'Выпуск', render: (row) => secCell(row) },
        { title: 'ISIN', render: (row) => `<span class="dim" style="font-family:var(--mono);font-size:11px">${fmt.esc(row.isin || '—')}</span>` },
        { title: 'Погашение', render: (row) => `<span class="dim">${fmt.date(row.maturity_date)}</span>` },
        { title: 'Лет', className: 'num', render: (row) => fmt.num(row.years_to_maturity, 2) },
        { title: 'Цена, %', className: 'num', render: (row) => fmt.price(row.last) },
        { title: 'СВЦ вчера', className: 'num', render: (row) => fmt.price(row.prev_wa_price) },
        { title: 'НКД', className: 'num', render: (row) => fmt.num(row.accrued_interest, 2) },
        { title: 'Полная цена', className: 'num', render: (row) => fmt.num(row.dirty_price, 2) },
        { title: 'Доходность', className: 'num', render: (row) => `<b>${fmt.pct(row.yield_pct)}</b>` },
        { title: 'Текущая', className: 'num', render: (row) => fmt.pct(row.current_yield_pct) },
        { title: 'Премия', className: 'num', render: (row) => premiumCell(row.spread_to_curve_bp) },
        { title: 'Дюрация', className: 'num', render: (row) => (fmt.isNum(row.duration_years) ? fmt.num(row.duration_years, 2) + ' л' : '—') },
        { title: 'Купон', className: 'num', render: (row) => fmt.pct(row.coupon_percent) },
        { title: 'Тип купона', render: (row) => `<span class="badge">${fmt.esc(row.coupon_type_title || '—')}</span>` },
        { title: 'Аморт.', render: (row) => (row.has_amortization ? '<span class="badge badge--accent">да</span>' : '<span class="dim">нет</span>') },
        { title: 'Оферта', render: (row) => (row.has_offer ? `<span class="badge badge--warn">${row.offer_date ? fmt.date(row.offer_date) : 'есть'}</span>` : '<span class="dim">нет</span>') },
        { title: 'Оборот, ₽', className: 'num', render: (row) => fmt.money(row.turnover) },
        { title: 'Ликв.', className: 'num', render: (row) => liquidityCell(row.liquidity_score) },
        { title: 'Ур.', className: 'num', render: (row) => `<span class="badge">${row.list_level || '—'}</span>` },
        {
          title: 'Риск',
          className: 'num',
          render: (row) => {
            const cls = RISK_BADGES[row.risk_band] || '';
            const hint = (row.risk_reasons || []).join('; ') || 'Расчётная оценка по рыночным данным';
            return `<span class="badge ${cls}" title="${fmt.esc(hint)}">${fmt.num(row.risk_score, 0)} · ${fmt.esc(row.risk_band || '')}</span>`;
          },
        },
      ], data.items, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Нет выпусков под заданные условия — ослабьте фильтры',
      });

      renderMarketMap(data.items);
    } catch (error) {
      failure(container, error);
    }
  }

  async function downloadAnalysis(format) {
    try {
      // Фильтры те же, что на экране, но в файл отдаём больше строк
      const params = Object.assign(analysisParams(), { fmt: format, limit: 2000 });
      const name = await api.download('/api/bonds/analysis/download', { params });
      toast(`Файл сформирован: ${name}`);
    } catch (error) {
      toast(error.message, true);
    }
  }

  /** Карта рынка: дюрация × доходность, размер — оборот, цвет — риск. */
  function renderMarketMap(items) {
    const RISK_COLORS = {
      'низкий': 'var(--up)',
      'умеренный': 'var(--accent)',
      'повышенный': 'var(--warn)',
      'высокий': 'var(--down)',
    };
    // Экстремальные доходности сжимают всё облако в полоску у нуля,
    // поэтому на карте показываем разумный диапазон
    const points = items
      .filter((row) =>
        fmt.isNum(row.duration_years) &&
        fmt.isNum(row.yield_pct) &&
        row.yield_pct < 60 &&
        row.duration_years < 20
      )
      .map((row) => ({
        x: row.duration_years,
        y: row.yield_pct,
        size: row.turnover || 0,
        color: RISK_COLORS[row.risk_band] || 'var(--accent)',
        key: row.secid,
        label:
          `${row.name || row.secid}\nДюрация: ${fmt.num(row.duration_years, 2)} л` +
          `\nДоходность: ${fmt.pct(row.yield_pct)}` +
          `\nПремия: ${fmt.bp(row.spread_to_curve_bp)}` +
          `\nОборот: ${fmt.money(row.turnover)} ₽\nРиск: ${row.risk_band || '—'}`,
      }));

    charts.scatterChart($('#market-map'), points, {
      height: 380,
      xFormat: (v) => fmt.num(v, 1) + ' л',
      yFormat: (v) => fmt.num(v, 1) + '%',
      xTitle: 'Дюрация, лет',
      yTitle: 'Доходность, %',
      onPick: openInstrument,
      emptyMessage: 'Недостаточно данных для карты',
    });
  }

  // ------------------------------------------------------------------
  // Сохранённые отборы
  // ------------------------------------------------------------------
  async function loadScreens() {
    try {
      const screens = await api.screens('bonds');
      const select = $('#a-screens');
      select.innerHTML =
        '<option value="">Сохранённые отборы…</option>' +
        screens
          .map((item) => `<option value="${item.id}">${fmt.esc(item.name)}</option>`)
          .join('');
      state.screens = screens;
    } catch (error) {
      console.warn('Отборы не загружены:', error.message);
    }
  }

  async function saveScreen() {
    const name = prompt('Название отбора:');
    if (!name) return;
    try {
      await api.saveScreen({ view: 'bonds', name, params: analysisParams() });
      await loadScreens();
      toast(`Отбор «${name}» сохранён`);
    } catch (error) {
      toast(error.message, true);
    }
  }

  function applyScreen(screenId) {
    const screen = (state.screens || []).find((item) => String(item.id) === String(screenId));
    if (!screen) return;
    let params = {};
    try {
      params = JSON.parse(screen.params);
    } catch (error) {
      return toast('Не удалось прочитать сохранённый отбор', true);
    }

    const map = {
      search: '#a-search', min_yield: '#a-minyield', max_yield: '#a-maxyield',
      min_duration_years: '#a-mindur', max_duration_years: '#a-maxdur',
      maturity_from: '#a-matfrom', maturity_to: '#a-matto',
      max_risk_score: '#a-risk', sort_by: '#a-sort',
      has_offer: '#a-offer', has_amortization: '#a-amort',
    };
    Object.entries(map).forEach(([key, selector]) => {
      const node = $(selector);
      if (node) node.value = params[key] === null || params[key] === undefined ? '' : params[key];
    });
    // Оборот хранится в рублях, а в поле вводится в миллионах
    if ($('#a-turnover')) $('#a-turnover').value = (params.min_turnover || 0) / 1e6;
    if (params.coupon_type && $('#a-coupon')) $('#a-coupon').value = params.coupon_type[0] || '';
    if (params.list_level && $('#a-level')) $('#a-level').value = params.list_level[0] || '';
    if (params.currency && $('#a-currency')) $('#a-currency').value = params.currency[0] || '';
    renderAnalysis();
  }

  // ------------------------------------------------------------------
  // Выгрузка по списку бумаг
  // ------------------------------------------------------------------
  async function renderExportParams() {
    if (state.exportParamsLoaded) return;
    const container = $('#e-params');
    try {
      const catalog = await api.exportParameters();
      container.innerHTML = catalog.groups
        .map((group) => `
          <div class="param-group">
            <div class="param-group__title">${fmt.esc(group.group)}</div>
            ${group.items.map((item) => `
              <label class="check">
                <input type="checkbox" value="${fmt.esc(item.code)}" ${item.default ? 'checked' : ''}>
                <span>${fmt.esc(item.title)}</span>
              </label>`).join('')}
          </div>`)
        .join('');
      state.exportParamsLoaded = true;
    } catch (error) {
      failure(container, error);
    }
  }

  function exportBody() {
    const checked = $$('#e-params input[type="checkbox"]:checked').map((node) => node.value);
    return {
      securities: $('#e-securities').value,
      date_from: $('#e-from').value,
      date_to: $('#e-to').value,
      parameters: checked,
      mode: state.exportMode,
    };
  }

  async function runExport() {
    const container = $('#e-table');
    const body = exportBody();

    if (!body.securities.trim()) {
      toast('Вставьте список бумаг', true);
      return;
    }
    if (!body.parameters.length) {
      toast('Отметьте хотя бы один параметр', true);
      return;
    }

    const button = $('#e-run');
    button.disabled = true;
    button.textContent = 'Загружаю с биржи…';
    loading(container);

    try {
      const data = await api.exportPreview(body);
      state.exportReady = data.rows.length > 0;

      $('#e-warnings').innerHTML = data.warnings.length
        ? `<div class="warnings">${data.warnings
            .map((text) => `<div class="warnings__item">${fmt.esc(text)}</div>`)
            .join('')}</div>`
        : '';

      $('#e-count').textContent = data.rows.length
        ? `${fmt.int(data.rows.length)} ${fmt.plural(data.rows.length, 'строка', 'строки', 'строк')} · ${data.found.length} ${fmt.plural(data.found.length, 'бумага', 'бумаги', 'бумаг')}`
        : '';

      // Колонки приходят с сервера — таблица на экране совпадает с файлом
      const columns = data.columns.map((column) => ({
        title: fmt.esc(column.title),
        className: column.kind === 'number' ? 'num' : '',
        render: (row) => {
          const value = row[column.code];
          if (value === null || value === undefined) return '<span class="dim">—</span>';
          if (column.kind === 'date') return fmt.date(value);
          if (column.kind === 'number') return fmt.num(value, column.digits ?? 2);
          return fmt.esc(value);
        },
      }));

      renderTable(container, columns, data.rows, {
        emptyMessage: 'Данных за период нет — проверьте бумаги и даты',
      });

      $('#e-xlsx').disabled = !state.exportReady;
      $('#e-csv').disabled = !state.exportReady;
    } catch (error) {
      failure(container, error);
      state.exportReady = false;
      $('#e-xlsx').disabled = true;
      $('#e-csv').disabled = true;
    } finally {
      button.disabled = false;
      button.textContent = 'Показать данные';
    }
  }

  async function downloadExport(format) {
    try {
      const name = await api.download(`/api/export/download?fmt=${format}`, {
        method: 'POST',
        body: exportBody(),
      });
      toast(`Файл сформирован: ${name}`);
    } catch (error) {
      toast(error.message, true);
    }
  }

  // ------------------------------------------------------------------
  // Портфель
  // ------------------------------------------------------------------
  async function renderPortfolio() {
    const kpi = $('#portfolio-kpi');
    loading(kpi);

    try {
      const [summary, sensitivity, deals] = await Promise.all([
        api.portfolio(state.portfolioName),
        api.sensitivity(state.portfolioName),
        api.deals({ name: state.portfolioName, limit: 100 }),
      ]);

      const money = (value) => `<span class="${fmt.trendClass(value)}">${fmt.rub(value)}</span>`;
      const cards = [
        {
          label: 'Стоимость портфеля',
          value: fmt.rub(summary.total_value),
          meta: `${summary.positions_open} ${fmt.plural(summary.positions_open, 'позиция', 'позиции', 'позиций')} · учёт ${summary.cost_method === 'fifo' ? 'ФИФО' : 'по средней'}`,
        },
        {
          label: 'Ценовой результат',
          value: money(summary.price_pnl),
          meta: 'от движения котировок',
        },
        {
          label: 'Валютный результат',
          value: money(summary.fx_pnl),
          meta: 'от переоценки курса',
        },
        {
          label: 'Купонный доход',
          value: money(summary.coupon_result),
          meta: `получено купонов ${fmt.rub(summary.coupons_received)}`,
        },
        {
          label: 'Реализованный',
          value: money(summary.realized_pnl),
          meta: `комиссии ${fmt.rub(summary.fees)}`,
        },
        {
          label: 'Итого результат',
          value: money(summary.net_pnl),
          meta: 'цена + курс + купоны − комиссии',
        },
        {
          label: 'Дюрация облигаций',
          value: fmt.isNum(summary.weighted_duration_years) ? fmt.num(summary.weighted_duration_years, 2) + ' л' : '—',
          meta: fmt.isNum(summary.weighted_yield_pct) ? `доходность ${fmt.pct(summary.weighted_yield_pct)}` : '',
        },
        {
          label: 'Концентрация',
          value: fmt.isNum(summary.concentration_hhi) ? fmt.num(summary.concentration_hhi, 3) : '—',
          meta: fmt.isNum(summary.top5_weight_pct) ? `топ-5 = ${fmt.pct(summary.top5_weight_pct, 1)}` : 'индекс Херфиндаля',
        },
      ];

      kpi.innerHTML = cards
        .map((card) => `
          <div class="kpi">
            <div class="kpi__label">${card.label}</div>
            <div class="kpi__value">${card.value}</div>
            <div class="kpi__meta">${card.meta || ''}</div>
          </div>`)
        .join('');

      charts.barsHorizontal(
        $('#allocation-chart'),
        [
          ...summary.allocation.map((item) => ({
            label: item.title, value: item.value, share: item.share_pct,
          })),
          ...summary.allocation_currency
            .filter((item) => item.key !== 'RUB')
            .map((item) => ({
              label: `Валюта ${item.title}`, value: item.value, share: item.share_pct,
            })),
        ],
        { valueFormat: (v) => fmt.money(v) + ' ₽', emptyMessage: 'Нет открытых позиций' }
      );

      // Сценарии: параллельный сдвиг плюс изменение формы кривой
      const tilt = state.curveTilt || 'scenarios';
      charts.barsHorizontal(
        $('#sensitivity-chart'),
        (sensitivity[tilt] || sensitivity.scenarios).map((item) => ({
          label: `${item.shift_bp > 0 ? '+' : ''}${item.shift_bp} бп`,
          value: item.impact_rub,
          share: item.impact_pct,
        })),
        { valueFormat: (v) => fmt.money(v) + ' ₽', colorBySign: true,
          emptyMessage: 'Нет облигаций в портфеле' }
      );
      const durationNote = $('#sensitivity-note');
      if (durationNote) {
        durationNote.textContent = fmt.isNum(sensitivity.weighted_modified_duration)
          ? `Мод. дюрация ${fmt.num(sensitivity.weighted_modified_duration, 2)}, выпуклость ${fmt.num(sensitivity.weighted_convexity, 1)}`
          : '';
      }

      renderTable($('#positions-table'), [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'Вал.', render: (row) => `<span class="badge">${fmt.esc(row.currency)}</span>` },
        { title: 'Кол-во', className: 'num', render: (row) => fmt.int(row.quantity) },
        { title: 'Средняя', className: 'num', render: (row) => fmt.price(row.avg_price) },
        { title: 'Текущая', className: 'num', render: (row) => fmt.price(row.last_price) },
        { title: 'Оценка, ₽', className: 'num', render: (row) => fmt.money(row.market_value_rub) },
        { title: 'Цена, ₽', className: 'num', render: (row) => `<span class="${fmt.trendClass(row.price_pnl_rub)}">${fmt.money(row.price_pnl_rub)}</span>` },
        { title: 'Курс, ₽', className: 'num', render: (row) => (row.currency === 'RUB' ? '<span class="dim">—</span>' : `<span class="${fmt.trendClass(row.fx_pnl_rub)}">${fmt.money(row.fx_pnl_rub)}</span>`) },
        { title: 'Купоны, ₽', className: 'num', render: (row) => `<span class="${fmt.trendClass(row.coupon_result_rub)}">${fmt.money(row.coupon_result_rub)}</span>` },
        { title: 'Итого, ₽', className: 'num', render: (row) => `<b class="${fmt.trendClass(row.total_pnl_rub)}">${fmt.money(row.total_pnl_rub)}</b>` },
        { title: 'Доля', className: 'num', render: (row) => fmt.pct(row.weight_pct, 1) },
        { title: 'Дюрация', className: 'num', render: (row) => (fmt.isNum(row.duration_years) ? fmt.num(row.duration_years, 2) + ' л' : '—') },
        { title: 'Выход', className: 'num', render: (row) => (fmt.isNum(row.days_to_exit) ? `${fmt.num(row.days_to_exit, 1)} дн` : '—') },
      ], summary.positions, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Позиций нет — добавьте сделку ниже',
      });

      renderTable($('#deals-table'), [
        { title: 'Дата', render: (row) => fmt.date(row.trade_date) },
        { title: 'Бумага', render: (row) => `<span class="sec__code">${fmt.esc(row.secid)}</span>` },
        {
          title: 'Сторона',
          render: (row) =>
            `<span class="badge badge--${row.side === 'buy' ? 'up' : 'down'}">${row.side === 'buy' ? 'покупка' : 'продажа'}</span>`,
        },
        { title: 'Кол-во', className: 'num', render: (row) => fmt.int(row.quantity) },
        { title: 'Цена', className: 'num', render: (row) => fmt.price(row.price) },
        { title: 'Комиссия', className: 'num', render: (row) => fmt.num(row.fee, 2) },
        {
          title: '',
          className: 'num',
          render: (row) => `<button class="btn btn--ghost" data-delete="${row.id}" title="Удалить сделку">✕</button>`,
        },
      ], deals, { emptyMessage: 'Сделок пока нет' });

      $$('#deals-table [data-delete]').forEach((button) => {
        button.addEventListener('click', async () => {
          if (!confirm('Удалить сделку? Позиции будут пересчитаны.')) return;
          try {
            await api.deleteDeal(button.dataset.delete);
            toast('Сделка удалена');
            renderPortfolio();
          } catch (error) {
            toast(error.message, true);
          }
        });
      });

      renderCashflow();
      renderBenchmark();
      renderLimits();
    } catch (error) {
      failure(kpi, error);
    }
  }

  // ------------------------------------------------------------------
  // Денежные потоки портфеля
  // ------------------------------------------------------------------
  async function renderCashflow() {
    const container = $('#cashflow-chart');
    try {
      const data = await api.cashflow(state.portfolioName, 365);
      $('#cashflow-total').textContent = data.total_rub
        ? `${fmt.money(data.total_rub)} ₽ за год`
        : '';

      charts.barChart(
        container,
        (data.by_month || []).map((item) => ({
          x: item.month,
          y: item.total_rub,
          label: `${item.month}\nКупоны: ${fmt.money(item.coupon_rub)} ₽\nПогашения: ${fmt.money(item.amortization_rub)} ₽`,
        })),
        {
          height: 180,
          yFormat: (v) => fmt.money(v),
          xFormat: (v) => String(v).slice(5) + '.' + String(v).slice(2, 4),
          emptyMessage: 'Нет запланированных поступлений',
        }
      );

      renderTable($('#cashflow-table'), [
        { title: 'Дата', render: (row) => fmt.date(row.action_date) },
        { title: 'Через', className: 'num', render: (row) => `${row.days_left} дн` },
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'Тип', render: (row) => `<span class="badge">${ACTION_TITLES[row.action_type] || row.action_type}</span>` },
        { title: 'Сумма, ₽', className: 'num', render: (row) => `<b>${fmt.money(row.amount_rub)}</b>` },
      ], data.events, { emptyMessage: 'Выплат в горизонте года не найдено' });
    } catch (error) {
      failure(container, error);
    }
  }

  // ------------------------------------------------------------------
  // Сравнение с рынком
  // ------------------------------------------------------------------
  async function renderBenchmark() {
    const container = $('#benchmark-body');
    try {
      const data = await api.benchmark(state.portfolioName, 90);
      const rows = [
        {
          title: 'Ваш портфель',
          return_pct: data.portfolio_return_pct,
          yield_pct: data.portfolio_yield_pct,
          duration_years: data.portfolio_duration_years,
          own: true,
        },
        ...data.benchmarks
          .filter((item) => item.available)
          .map((item) => ({
            title: item.title,
            return_pct: item.return_pct,
            yield_pct: item.yield_pct,
            duration_years: item.duration_years,
            excess_pct: item.excess_pct,
          })),
      ];

      renderTable(container, [
        { title: 'Ориентир', render: (row) => (row.own ? `<b>${fmt.esc(row.title)}</b>` : fmt.esc(row.title)) },
        { title: 'Доходность за период', className: 'num', render: (row) => `<span class="${fmt.trendClass(row.return_pct)}">${fmt.signedPct(row.return_pct)}</span>` },
        { title: 'Разница', className: 'num', render: (row) => (row.own ? '<span class="dim">—</span>' : `<span class="${fmt.trendClass(row.excess_pct)}">${fmt.signedPct(row.excess_pct)}</span>`) },
        { title: 'Доходность к погашению', className: 'num', render: (row) => fmt.pct(row.yield_pct) },
        { title: 'Дюрация', className: 'num', render: (row) => (fmt.isNum(row.duration_years) ? fmt.num(row.duration_years, 2) + ' л' : '—') },
      ], rows, { emptyMessage: 'Недостаточно истории для сравнения' });

      if (fmt.isNum(data.coverage_pct) && data.coverage_pct < 99) {
        container.insertAdjacentHTML(
          'beforeend',
          `<p class="card__note" style="border:none;padding:9px 0 0">В расчёт вошло ${fmt.pct(data.coverage_pct, 0)} стоимости портфеля: по остальным бумагам история ещё не загружена.</p>`
        );
      }
    } catch (error) {
      failure(container, error);
    }
  }

  // ------------------------------------------------------------------
  // Лимиты
  // ------------------------------------------------------------------
  async function renderLimits() {
    const container = $('#limits-table');
    try {
      const data = await api.checkLimits(state.portfolioName);
      $('#limits-status').textContent = data.limits_total
        ? (data.breached
            ? `нарушено ${data.breached} из ${data.limits_total}`
            : `все ${data.limits_total} соблюдены`)
        : 'лимиты не заданы';

      renderTable(container, [
        { title: 'Лимит', render: (row) => `<div class="sec"><span class="sec__code">${fmt.esc(row.kind_title)}</span><span class="sec__name">${fmt.esc(row.subject)}</span></div>` },
        { title: 'Значение', className: 'num', render: (row) => `${fmt.num(row.limit_value, 2)} ${fmt.esc(row.unit)}` },
        { title: 'Факт', className: 'num', render: (row) => `<b>${fmt.num(row.actual, 2)}</b>` },
        {
          title: 'Заполнено',
          className: 'num',
          render: (row) => {
            const used = row.utilisation_pct || 0;
            const colour = row.breached ? 'var(--down)' : used > 80 ? 'var(--warn)' : 'var(--up)';
            return `<div class="meter">
              <span class="meter__val">${fmt.num(used, 0)}%</span>
              <span class="meter__bar"><span class="meter__fill" style="width:${Math.min(used, 100)}%;background:${colour}"></span></span>
            </div>`;
          },
        },
        { title: 'Запас', className: 'num', render: (row) => `<span class="${row.breached ? 'down' : 'dim'}">${fmt.num(row.headroom, 2)}</span>` },
        { title: 'Статус', render: (row) => (row.breached ? '<span class="badge badge--down">нарушен</span>' : '<span class="badge badge--up">в норме</span>') },
        { title: '', className: 'num', render: (row) => `<button class="btn btn--ghost" data-limit="${row.limit_id}" title="Снять лимит">✕</button>` },
      ], data.items, { emptyMessage: 'Лимиты не заданы — нажмите «Установить лимит»' });

      $$('#limits-table [data-limit]').forEach((button) => {
        button.addEventListener('click', async () => {
          try {
            await api.deleteLimit(button.dataset.limit);
            toast('Лимит снят');
            renderLimits();
          } catch (error) {
            toast(error.message, true);
          }
        });
      });
    } catch (error) {
      failure(container, error);
    }
  }

  async function loadLimitKinds() {
    if (state.limitKinds.length) return;
    try {
      state.limitKinds = await api.limitKinds();
      $('#l-kind').innerHTML = state.limitKinds
        .map((item) => `<option value="${item.kind}" title="${fmt.esc(item.hint)}">${fmt.esc(item.title)}, ${fmt.esc(item.unit)}</option>`)
        .join('');
    } catch (error) {
      console.warn('Виды лимитов не загружены:', error.message);
    }
  }

  async function submitLimit(event) {
    event.preventDefault();
    const message = $('#limit-msg');
    try {
      await api.createLimit({
        kind: $('#l-kind').value,
        target: $('#l-target').value.trim() || null,
        value: parseFloat($('#l-value').value),
        comment: $('#l-comment').value.trim() || null,
        portfolio: state.portfolioName || 'Основной',
      });
      message.textContent = 'Лимит установлен';
      message.className = 'form-msg form-msg--ok';
      $('#l-value').value = '';
      $('#l-target').value = '';
      renderLimits();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  /** Проверка сделки до её совершения. */
  async function checkDealAgainstLimits() {
    const message = $('#deal-msg');
    const secid = $('#d-secid').value.trim().toUpperCase();
    const quantity = parseFloat($('#d-qty').value);
    const price = parseFloat($('#d-price').value);

    if (!secid || !quantity || !price) {
      message.textContent = 'Заполните инструмент, количество и цену';
      message.className = 'form-msg form-msg--err';
      return;
    }

    try {
      const result = await api.previewTrade({
        secid, quantity, price, portfolio: $('#d-portfolio').value.trim() || null,
      });
      if (result.allowed) {
        message.textContent =
          `Лимиты соблюдены: сделка на ${fmt.rub(result.value_rub)} — это ${fmt.pct(result.value_share_pct, 1)} портфеля`;
        message.className = 'form-msg form-msg--ok';
      } else {
        const names = result.new_breaches
          .map((row) => `${row.kind_title} (${row.subject}): ${fmt.num(row.actual, 1)} при лимите ${fmt.num(row.limit_value, 1)}`)
          .join('; ');
        message.textContent = `Сделка выведет за лимиты — ${names}`;
        message.className = 'form-msg form-msg--err';
      }
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  // ------------------------------------------------------------------
  // Список наблюдения
  // ------------------------------------------------------------------
  async function renderWatchlist() {
    const container = $('#watchlist-table');
    try {
      const items = await api.watchlist();
      renderTable(container, [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'Цена', className: 'num', render: (row) => fmt.price(row.last) },
        { title: 'Изм.', className: 'num', render: (row) => changeCell(row.change_pct) },
        { title: 'Доходность', className: 'num', render: (row) => fmt.pct(row.yield_pct) },
        { title: 'Премия', className: 'num', render: (row) => premiumCell(row.spread_to_curve_bp) },
        { title: 'Оборот, ₽', className: 'num', render: (row) => fmt.money(row.turnover) },
        { title: 'Ликв.', className: 'num', render: (row) => liquidityCell(row.liquidity_score) },
        { title: '', className: 'num', render: (row) => `<button class="btn btn--ghost" data-watch="${row.id}" title="Убрать">✕</button>` },
      ], items, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Список пуст — добавьте бумагу по коду',
      });

      $$('#watchlist-table [data-watch]').forEach((button) => {
        button.addEventListener('click', async () => {
          try {
            await api.removeWatch(button.dataset.watch);
            renderWatchlist();
          } catch (error) {
            toast(error.message, true);
          }
        });
      });
    } catch (error) {
      failure(container, error);
    }
  }

  async function addToWatchlist() {
    const input = $('#w-secid');
    const secid = input.value.trim().toUpperCase();
    if (!secid) return;
    try {
      await api.addWatch({ secid });
      input.value = '';
      toast(`${secid} добавлена в наблюдение`);
      renderWatchlist();
    } catch (error) {
      toast(error.message, true);
    }
  }

  const KIND_TITLES = {
    share: 'Акции',
    bond: 'Облигации',
    index: 'Индексы',
    currency: 'Валюта',
  };

  async function submitDeal(event) {
    event.preventDefault();
    const message = $('#deal-msg');
    const payload = {
      secid: $('#d-secid').value.trim().toUpperCase(),
      side: $('#d-side').value,
      quantity: parseFloat($('#d-qty').value),
      price: parseFloat($('#d-price').value),
      accrued_interest: parseFloat($('#d-nkd').value) || 0,
      fee: parseFloat($('#d-fee').value) || 0,
      trade_date: $('#d-date').value,
      portfolio: $('#d-portfolio').value.trim() || 'Основной',
    };

    try {
      await api.createDeal(payload);
      message.textContent = 'Сделка добавлена';
      message.className = 'form-msg form-msg--ok';
      $('#d-qty').value = '';
      $('#d-price').value = '';
      renderPortfolio();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  // ------------------------------------------------------------------
  // Сигналы
  // ------------------------------------------------------------------
  async function renderSignals() {
    const alertsBox = $('#alerts-list');
    loading(alertsBox);

    try {
      const [alerts, anomalies, calendar] = await Promise.all([
        api.alerts({ limit: 25 }),
        api.anomalies({ limit: 15 }),
        api.calendar({ horizon_days: 90 }),
      ]);

      alertsBox.innerHTML = alerts.length
        ? alerts
            .map((alert) => `
              <div class="alert">
                <span class="alert__dot alert__dot--${alert.severity}"></span>
                <div>
                  <div class="alert__title">${fmt.esc(alert.title)}</div>
                  <div class="alert__detail">${fmt.esc(alert.detail)}</div>
                </div>
              </div>`)
            .join('')
        : '<div class="empty">Сигналов нет — рынок спокоен</div>';

      renderTable($('#anomalies-table'), [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'Дата', render: (row) => `<span class="dim">${fmt.date(row.trade_date)}</span>` },
        { title: 'Объём', className: 'num', render: (row) => fmt.int(row.volume) },
        { title: 'Средний', className: 'num', render: (row) => fmt.int(row.avg_volume) },
        {
          title: 'Кратность',
          className: 'num',
          render: (row) => `<span class="badge ${row.z_score > 0 ? 'badge--up' : 'badge--down'}">×${fmt.num(row.ratio_to_avg, 2)}</span>`,
        },
        { title: 'Z-оценка', className: 'num', render: (row) => fmt.num(row.z_score, 2) },
      ], anomalies, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Отклонений не обнаружено',
      });

      renderWatchlist();

      renderTable($('#calendar-table'), [
        { title: 'Дата', render: (row) => fmt.date(row.action_date) },
        {
          title: 'Через',
          className: 'num',
          render: (row) => `${row.days_left} ${fmt.plural(row.days_left, 'день', 'дня', 'дней')}`,
        },
        { title: 'Тип', render: (row) => `<span class="badge">${ACTION_TITLES[row.action_type] || row.action_type}</span>` },
        { title: 'Выпуск', render: (row) => secCell({ secid: row.secid, name: row.name }) },
        { title: 'ISIN', render: (row) => `<span class="dim" style="font-family:var(--mono);font-size:11px">${fmt.esc(row.isin)}</span>` },
        { title: 'Выплата', className: 'num', render: (row) => `${fmt.num(row.value, 2)} ${fmt.esc(row.face_unit || '')}` },
        { title: 'Фиксация', render: (row) => `<span class="dim">${row.record_date ? fmt.date(row.record_date) : '—'}</span>` },
      ], calendar, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'В ближайшие 90 дней выплат не найдено',
      });
    } catch (error) {
      failure(alertsBox, error);
    }
  }

  const ACTION_TITLES = { coupon: 'купон', amortization: 'амортизация', offer: 'оферта' };

  // ------------------------------------------------------------------
  // Источники
  // ------------------------------------------------------------------
  async function renderSources() {
    const grid = $('#sources-grid');
    loading(grid);
    try {
      const [sources, runs] = await Promise.all([api.sources(), api.runs({ limit: 40 })]);

      grid.innerHTML = sources
        .map((source) => `
          <article class="card">
            <header class="card__head">
              <h2>${fmt.esc(source.name)}</h2>
              <span class="card__hint">${fmt.esc(source.code)}</span>
            </header>
            <div class="card__body">
              <div class="section-title">Что берём</div>
              <ul style="margin:0 0 12px;padding-left:18px;color:var(--text-dim);font-size:12.5px;line-height:1.7">
                ${source.provides.map((item) => `<li>${fmt.esc(item)}</li>`).join('')}
              </ul>
              <div class="stat">
                <div class="stat__label">Доступ</div>
                <div style="font-size:12.5px;margin-top:3px">${fmt.esc(source.access)}</div>
              </div>
              ${source.note ? `<p class="card__note" style="border:none;padding:11px 0 0">${fmt.esc(source.note)}</p>` : ''}
              <a href="${fmt.esc(source.url)}" target="_blank" rel="noopener"
                 style="display:inline-block;margin-top:10px;color:var(--accent);font-size:12.5px">Документация ↗</a>
            </div>
          </article>`)
        .join('');

      renderTable($('#runs-table'), [
        { title: 'Начало', render: (row) => fmt.dateTime(row.started_at) },
        { title: 'Источник', render: (row) => `<span class="badge">${fmt.esc(row.source)}</span>` },
        { title: 'Задача', render: (row) => fmt.esc(row.task) },
        {
          title: 'Статус',
          render: (row) => {
            const map = { success: 'badge--up', error: 'badge--down', running: 'badge--warn' };
            const titles = { success: 'успешно', error: 'ошибка', running: 'выполняется' };
            return `<span class="badge ${map[row.status] || ''}">${titles[row.status] || row.status}</span>`;
          },
        },
        { title: 'Строк', className: 'num', render: (row) => fmt.int(row.rows) },
        { title: 'Время, с', className: 'num', render: (row) => fmt.num(row.duration_sec, 2) },
        { title: 'Ошибка', render: (row) => `<span class="dim">${fmt.esc(row.error || '—')}</span>` },
      ], runs, { emptyMessage: 'Сбор ещё не запускался' });
    } catch (error) {
      failure(grid, error);
    }
  }

  // ------------------------------------------------------------------
  // Карточка инструмента
  // ------------------------------------------------------------------
  async function openInstrument(secid) {
    if (!secid) return;
    const drawer = $('#drawer');
    const body = $('#drawer-body');
    drawer.hidden = false;
    $('#drawer-title').textContent = secid;
    $('#drawer-sub').textContent = 'Загрузка…';
    loading(body);

    try {
      const data = await api.instrument(secid, { history_days: 180 });
      const info = data.instrument;

      $('#drawer-title').textContent = info.secid;
      $('#drawer-sub').textContent =
        [info.full_name || info.name, info.isin, info.board].filter(Boolean).join(' · ');

      const stats = [
        { label: 'Цена', value: fmt.price(info.last) },
        { label: 'Изменение', value: `<span class="${fmt.trendClass(info.change_pct)}">${fmt.signedPct(info.change_pct)}</span>` },
        { label: 'Оборот, ₽', value: fmt.money(info.turnover) },
        { label: 'Объём, шт', value: fmt.int(info.volume) },
        { label: 'Сделок', value: fmt.int(info.num_trades) },
        { label: 'Спред', value: fmt.pct(info.spread_pct, 3) },
        { label: 'Ликвидность', value: fmt.isNum(info.liquidity_score) ? fmt.num(info.liquidity_score, 0) + ' / 100' : '—' },
        { label: 'Лот', value: fmt.int(info.lot_size) },
      ];

      if (info.kind === 'bond') {
        stats.push(
          { label: 'Доходность', value: fmt.pct(info.yield_pct) },
          { label: 'Дюрация', value: fmt.isNum(info.duration_years) ? fmt.num(info.duration_years, 2) + ' л' : '—' },
          { label: 'Премия к КБД', value: fmt.bp(info.spread_to_curve_bp) },
          { label: 'Купон', value: fmt.pct(info.coupon_percent) },
          { label: 'Погашение', value: fmt.date(info.maturity_date) },
          { label: 'НКД', value: fmt.num(info.accrued_interest, 2) }
        );
      } else {
        stats.push(
          { label: 'Капитализация', value: fmt.money(info.capitalization) + ' ₽' },
          { label: 'Ур. листинга', value: info.list_level || '—' }
        );
      }

      body.innerHTML = `
        <div class="stat-grid">
          ${stats.map((stat) => `
            <div class="stat">
              <div class="stat__label">${stat.label}</div>
              <div class="stat__value">${stat.value}</div>
            </div>`).join('')}
        </div>
        <div>
          <div class="section-title">Цена и объём торгов</div>
          <div id="drawer-chart"></div>
        </div>
        ${info.kind === 'bond' ? '<div><div class="section-title">Премия к рынку гособлигаций</div><div id="drawer-spread"></div></div>' : ''}
        ${data.cashflows.length ? '<div><div class="section-title">График выплат (данные НРД)</div><div id="drawer-cashflows" class="table-wrap"></div></div>' : ''}`;

      charts.priceVolumeChart($('#drawer-chart'), data.history, {
        height: 260,
        priceFormat: (value) => fmt.price(value),
      });

      if (info.kind === 'bond') renderSpreadHistory(info.secid);

      if (data.cashflows.length) {
        const upcoming = data.cashflows
          .filter((row) => new Date(row.action_date) >= new Date(Date.now() - 86400000))
          .slice(0, 24);
        renderTable($('#drawer-cashflows'), [
          { title: 'Дата', render: (row) => fmt.date(row.action_date) },
          { title: 'Тип', render: (row) => `<span class="badge">${ACTION_TITLES[row.action_type] || row.action_type}</span>` },
          { title: 'Выплата', className: 'num', render: (row) => `${fmt.num(row.value, 2)} ${fmt.esc(row.face_unit || '')}` },
          { title: 'Фиксация', render: (row) => (row.record_date ? fmt.date(row.record_date) : '—') },
        ], upcoming, { emptyMessage: 'Будущих выплат нет' });
      }
    } catch (error) {
      failure(body, error);
    }
  }

  /** История премии выпуска к рынку гособлигаций. */
  async function renderSpreadHistory(secid) {
    const container = $('#drawer-spread');
    if (!container) return;
    try {
      const data = await api.spreadHistory(secid, 365);
      if (!data.points.length) {
        // Причину сообщает сервер: у флоатеров доходности нет в принципе,
        // а по остальным история может быть просто ещё не загружена
        return charts.empty(container, data.reason || 'Данных для расчёта премии пока нет');
      }

      charts.lineChart(
        container,
        [{
          name: 'Премия, бп',
          color: themeColor('--accent', '#3f9d6d'),
          points: data.points.map((point) => ({
            x: new Date(point.trade_date).getTime(),
            y: point.spread_bp,
            label: `${fmt.date(point.trade_date)}: ${fmt.bp(point.spread_bp)}`,
          })),
        }],
        {
          height: 200,
          yFormat: (v) => fmt.num(v, 0),
          xFormat: (v) => fmt.dateShort(new Date(v)),
        }
      );

      const stats = data.stats || {};
      if (fmt.isNum(stats.deviation_bp)) {
        const verdict = stats.deviation_bp > 0
          ? 'бумага торгуется дешевле своей средней'
          : 'бумага торгуется дороже своей средней';
        container.insertAdjacentHTML(
          'beforeend',
          `<p class="card__note" style="border:none;padding:8px 0 0">
            Сейчас ${fmt.bp(stats.current_bp)} против средней ${fmt.bp(stats.average_bp)} за период
            (диапазон ${fmt.bp(stats.min_bp)}…${fmt.bp(stats.max_bp)}) — ${verdict}.
          </p>`
        );
      }
    } catch (error) {
      failure(container, error);
    }
  }

  function closeDrawer() {
    $('#drawer').hidden = true;
  }

  // ------------------------------------------------------------------
  // Навигация и события
  // ------------------------------------------------------------------
  /** Вкладка облигаций: анализ рынка и выгрузка по списку в одном месте. */
  async function renderBondsTab() {
    // Список параметров нужен и для режима выгрузки — грузим сразу
    renderExportParams();
    await renderAnalysis();
  }

  function switchBondMode(mode) {
    state.bondMode = mode;
    $$('#bond-mode button').forEach((button) =>
      button.classList.toggle('is-active', button.dataset.bmode === mode)
    );
    $('#pane-screen').hidden = mode !== 'screen';
    $('#pane-list').hidden = mode !== 'list';
    $('#bond-mode-hint').textContent =
      mode === 'screen'
        ? 'Отбор по всем торгуемым выпускам на текущий срез'
        : 'Свои бумаги за период: вставьте список из Excel';

    if (mode === 'screen' && !state.analysisLoaded) renderAnalysis();
  }

  const RENDERERS = {
    overview: renderOverview,
    instruments: renderInstruments,
    bonds: renderBondsTab,
    portfolio: renderPortfolio,
    signals: renderSignals,
    sources: renderSources,
  };

  function switchView(name) {
    state.view = name;
    $$('.tab').forEach((tab) => tab.classList.toggle('tab--active', tab.dataset.view === name));
    $$('.view').forEach((view) => view.classList.toggle('view--active', view.id === `view-${name}`));

    // Вкладку рисуем при первом открытии, дальше — по кнопке «Обновить»
    if (!state.loaded[name]) {
      state.loaded[name] = true;
      RENDERERS[name]();
    }
  }

  function refresh() {
    state.loaded = {};
    state.loaded[state.view] = true;
    RENDERERS[state.view]();
    toast('Данные обновлены');
  }

  async function collect() {
    const button = $('#btn-collect');
    button.disabled = true;
    button.textContent = 'Сбор…';
    try {
      await api.collect(true);
      toast('Сбор запущен — данные появятся через несколько секунд');
      // Даём сборщику время дойти до биржи и записать срез
      setTimeout(() => {
        state.loaded = {};
        state.loaded[state.view] = true;
        RENDERERS[state.view]();
      }, 9000);
    } catch (error) {
      toast(error.message, true);
    } finally {
      setTimeout(() => {
        button.disabled = false;
        button.textContent = 'Собрать данные';
      }, 9000);
    }
  }

  /** Отложенный вызов — чтобы не дёргать API на каждое нажатие клавиши. */
  function debounce(fn, delay = 350) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  function init() {
    applyTheme(initialTheme());

    const today = new Date();
    const isoToday = today.toISOString().slice(0, 10);
    const monthAgo = new Date(today.getTime() - 30 * 86400000).toISOString().slice(0, 10);
    [['#d-date', isoToday], ['#e-from', monthAgo], ['#e-to', isoToday]].forEach(
      ([selector, value]) => { const node = $(selector); if (node) node.value = value; }
    );

    $$('.tab').forEach((tab) => {
      tab.addEventListener('click', () => switchView(tab.dataset.view));
    });

    on('#btn-theme', 'click', () => {
      const current = document.documentElement.getAttribute('data-theme');
      applyTheme(current === 'light' ? 'dark' : 'light');
    });

    on('#btn-refresh', 'click', refresh);
    on('#btn-collect', 'click', collect);
    on('#deal-form', 'submit', submitDeal);

    // Анализ облигаций
    const onAnalysisFilter = debounce(renderAnalysis);
    ['#a-search', '#a-minyield', '#a-maxyield', '#a-mindur', '#a-maxdur',
     '#a-matfrom', '#a-matto', '#a-turnover', '#a-risk'].forEach((selector) =>
      on(selector, 'input', onAnalysisFilter)
    );
    ['#a-coupon', '#a-level', '#a-currency', '#a-offer', '#a-amort', '#a-sort'].forEach(
      (selector) => on(selector, 'change', renderAnalysis)
    );
    on('#analysis-xlsx', 'click', () => downloadAnalysis('xlsx'));
    on('#analysis-csv', 'click', () => downloadAnalysis('csv'));
    on('#a-save-screen', 'click', saveScreen);
    on('#a-screens', 'change', (event) => {
      if (event.target.value) applyScreen(event.target.value);
    });

    // Лимиты и проверка сделки
    on('#limit-add', 'click', async () => {
      await loadLimitKinds();
      const form = $('#limit-form');
      form.hidden = !form.hidden;
    });
    on('#limit-cancel', 'click', () => { $('#limit-form').hidden = true; });
    on('#limit-form', 'submit', submitLimit);
    on('#deal-check', 'click', checkDealAgainstLimits);

    // Список наблюдения
    on('#w-add', 'click', addToWatchlist);
    on('#w-secid', 'keydown', (event) => {
      if (event.key === 'Enter') addToWatchlist();
    });

    // Выгрузка
    on('#e-run', 'click', runExport);
    on('#e-xlsx', 'click', () => downloadExport('xlsx'));
    on('#e-csv', 'click', () => downloadExport('csv'));
    on('#e-all', 'click', () => {
      const boxes = $$('#e-params input[type="checkbox"]');
      const allChecked = boxes.every((box) => box.checked);
      boxes.forEach((box) => { box.checked = !allChecked; });
      $('#e-all').textContent = allChecked ? 'Выбрать все' : 'Снять все';
    });
    $$('#e-mode button').forEach((button) => {
      button.addEventListener('click', () => {
        state.exportMode = button.dataset.mode;
        $$('#e-mode button').forEach((other) =>
          other.classList.toggle('is-active', other === button)
        );
        // Форма таблицы изменилась — прежний результат больше не соответствует
        if (state.exportReady) runExport();
      });
    });

    const onInstrumentFilter = debounce(renderInstruments);
    ['#f-search', '#f-turnover', '#f-liquidity'].forEach((selector) =>
      on(selector, 'input', onInstrumentFilter)
    );
    ['#f-kind', '#f-sort'].forEach((selector) =>
      on(selector, 'change', renderInstruments)
    );

    $$('#bond-mode button').forEach((button) => {
      button.addEventListener('click', () => switchBondMode(button.dataset.bmode));
    });

    $$('[data-close]').forEach((node) => node.addEventListener('click', closeDrawer));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeDrawer();
    });

    switchView('overview');
  }

  document.addEventListener('DOMContentLoaded', () => {
    try {
      init();
    } catch (error) {
      // Тема применяется первой, чтобы интерфейс не остался нечитаемым
      console.error('Ошибка инициализации интерфейса:', error);
      applyTheme(initialTheme());
    }
  });
})();
