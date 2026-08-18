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
    curveTilt: 'scenarios',
    analysisLoaded: false,
    // Сортировка таблицы облигаций: по какому полю и в какую сторону
    analysisSort: { by: 'yield_pct', order: 'desc' },
    screens: [],
    limitKinds: [],
    picked: { instruments: {}, analysis: {} },
    rows: { instruments: {}, analysis: {} },
    buyRows: [],
    portfolios: [],
    cashHorizon: 180,
    historyDays: 365,
    accounts: [],
    importDeals: [],
    authEnabled: false,
    user: null,
    autoTimer: null,
    intradayInterval: 10,
    drawerSecid: null,
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

  // ------------------------------------------------------------------
  // Отметка бумаг для добавления в портфель
  // ------------------------------------------------------------------
  /** Колонка с флажком и кнопкой быстрого добавления одной бумаги. */
  function pickColumn(scope) {
    return {
      title: `<input type="checkbox" data-pick-all="${scope}" title="Отметить все">`,
      className: 'pick',
      render: (row) => {
        const picked = state.picked[scope] && state.picked[scope][row.secid];
        return `<input type="checkbox" data-pick="${scope}" value="${fmt.esc(row.secid)}"${picked ? ' checked' : ''}>`;
      },
    };
  }

  function buyColumn(scope) {
    return {
      title: '',
      className: 'num',
      render: (row) =>
        `<button class="btn btn--ghost" data-buy-one="${fmt.esc(row.secid)}" title="Добавить в портфель">+</button>`,
    };
  }

  /** Запомнить строки витрины, чтобы окно знало цену и НКД по коду. */
  function rememberRows(scope, rows) {
    state.rows[scope] = {};
    (rows || []).forEach((row) => { state.rows[scope][row.secid] = row; });
  }

  function pickedList(scope) {
    return Object.keys(state.picked[scope] || {}).filter(
      (secid) => state.picked[scope][secid]
    );
  }

  function refreshPickState(scope) {
    const count = pickedList(scope).length;
    const button = $(scope === 'instruments' ? '#instruments-buy' : '#analysis-buy');
    if (button) {
      button.disabled = count === 0;
      button.textContent = count ? `В портфель (${count})` : 'В портфель';
    }
  }

  /** Навесить обработчики флажков и кнопок «+» после отрисовки таблицы. */
  function wirePicking(container, scope) {
    container.querySelectorAll(`[data-pick="${scope}"]`).forEach((box) => {
      box.addEventListener('click', (event) => event.stopPropagation());
      box.addEventListener('change', () => {
        state.picked[scope] = state.picked[scope] || {};
        state.picked[scope][box.value] = box.checked;
        refreshPickState(scope);
      });
    });

    const all = container.querySelector(`[data-pick-all="${scope}"]`);
    if (all) {
      all.addEventListener('click', (event) => event.stopPropagation());
      all.addEventListener('change', () => {
        state.picked[scope] = state.picked[scope] || {};
        container.querySelectorAll(`[data-pick="${scope}"]`).forEach((box) => {
          box.checked = all.checked;
          state.picked[scope][box.value] = all.checked;
        });
        refreshPickState(scope);
      });
    }

    container.querySelectorAll('[data-buy-one]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        openBuyModal(scope, [button.dataset.buyOne]);
      });
    });
    refreshPickState(scope);
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

    // Заголовок сортируемой колонки кликабелен, а стрелка показывает,
    // по какому полю и в какую сторону идёт сортировка сейчас
    const sort = options.sort;
    const head = columns
      .map((col) => {
        const classes = [col.className || ''];
        if (!col.sortBy || !sort) {
          return `<th class="${classes.join(' ')}">${col.title}</th>`;
        }
        classes.push('sortable');
        const active = sort.by === col.sortBy;
        if (active) classes.push('sortable--active');
        const arrow = active ? (sort.order === 'asc' ? '▲' : '▼') : '⇅';
        return `<th class="${classes.join(' ')}" data-sort="${col.sortBy}"
                    title="Отсортировать по столбцу">${col.title} <span class="sortable__mark">${arrow}</span></th>`;
      })
      .join('');
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

    if (options.sort && options.onSort) {
      container.querySelectorAll('th[data-sort]').forEach((th) => {
        th.addEventListener('click', () => {
          const field = th.dataset.sort;
          // Первый клик по новому столбцу — по убыванию: чаще нужен
          // максимум, а не минимум. Повторный переворачивает порядок
          const order = options.sort.by === field && options.sort.order === 'desc'
            ? 'asc' : 'desc';
          options.onSort(field, order);
        });
      });
    }

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
        action: 'rate-calendar',
        hint: 'Календарь заседаний Банка России по ключевой ставке',
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
        <div class="kpi${card.action ? ' kpi--clickable' : ''}"${
          card.action ? ` data-action="${card.action}" title="${fmt.esc(card.hint || '')}"` : ''
        }>
          <div class="kpi__label">${card.label}</div>
          <div class="kpi__value">${card.value}</div>
          <div class="kpi__meta">${card.meta || ''}</div>
        </div>`)
      .join('');

    kpiGrid.querySelectorAll('[data-action="rate-calendar"]').forEach((tile) => {
      tile.addEventListener('click', openRateCalendar);
    });

    $('#status').textContent = overview.updated_at
      ? `срез ${fmt.dateTime(overview.updated_at)}`
      : 'нет данных';

    renderCurve(overview.curve);
    renderSeriesCharts();
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
    // Запоминаем срез: он же перерисовывается при переходе в полный экран
    if (curve) state.lastCurve = curve;
    if (!curve || !curve.points || !curve.points.length) {
      return charts.empty(container, 'Кривая не загружена');
    }
    $('#curve-date').textContent = `на ${fmt.date(curve.curve_date)}`;
    bindStaticFullscreen();

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
        height: $('#card-curve').classList.contains('card--full') ? 520 : 220,
        yFormat: (v) => fmt.num(v, 1) + '%',
        xFormat: (v) => (v < 1 ? `${v}` : `${Math.round(v)}л`),
        dots: true,
      }
    );
  }

  // ------------------------------------------------------------------
  // Карточки графиков обзора рынка
  // ------------------------------------------------------------------
  //
  // Каждая карточка живёт по одному сценарию: период → набор показателей →
  // перерисовка. Описание берётся с сервера (/api/series/catalog), поэтому
  // новый график добавляется одной записью в каталоге, без правок здесь.

  /** Готовые периоды. Пустой from означает «с начала данных». */
  const CHART_PERIODS = [
    { code: '1m', title: '1 мес', days: 30 },
    { code: '3m', title: '3 мес', days: 90 },
    { code: '6m', title: '6 мес', days: 180 },
    { code: '12m', title: '12 мес', days: 365 },
    { code: '3y', title: '3 года', days: 1095 },
  ];

  /** Состояние карточек: выбранный период и включённые показатели. */
  const chartState = new Map();

  function isoDate(date) {
    return date.toISOString().slice(0, 10);
  }

  function periodRange(state) {
    if (state.period === 'custom') {
      return { date_from: state.from || null, date_to: state.to || null };
    }
    const preset = CHART_PERIODS.find((item) => item.code === state.period)
      || CHART_PERIODS[3];
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - preset.days);
    return { date_from: isoDate(from), date_to: isoDate(to) };
  }

  /** Разметка карточки: шапка, период, показатели и место под график. */
  function chartShell(card, spec, state) {
    const range = periodRange(state);
    const periods = CHART_PERIODS.map((item) =>
      `<button class="btn btn--sm${state.period === item.code ? ' btn--primary' : ''}"
               data-period="${item.code}" type="button">${item.title}</button>`
    ).join('');

    // Показатели показываем только там, где есть из чего выбирать
    const metrics = spec.metrics.length > 1
      ? `<div class="chart-metrics">${spec.metrics.map((metric, index) => `
          <label class="chart-metrics__item">
            <input type="checkbox" data-metric="${metric.code}"
                   ${state.metrics.includes(metric.code) ? 'checked' : ''}>
            <span class="chart-metrics__dot" style="background:${charts.seriesColor(index)}"></span>
            <span>${fmt.esc(metric.title)}${metric.kind === 'bar' ? ' ▮' : ''}</span>
          </label>`).join('')}</div>`
      : '';

    card.innerHTML = `
      <header class="card__head">
        <h2>${fmt.esc(spec.title)}</h2>
        <div class="card__tools">
          <button class="btn btn--sm" data-action="xlsx" type="button">Excel</button>
          <button class="btn btn--sm" data-action="full" type="button" title="На весь экран">⛶</button>
        </div>
      </header>
      <div class="chart-controls">
        ${periods}
        <button class="btn btn--sm${state.period === 'custom' ? ' btn--primary' : ''}"
                data-period="custom" type="button">Период</button>
        <span class="chart-controls__dates">
          <input type="date" data-role="from" value="${range.date_from || ''}">
          <input type="date" data-role="to" value="${range.date_to || ''}">
        </span>
      </div>
      ${metrics}
      <div class="card__body" data-role="canvas"></div>
      <p class="card__note">${fmt.esc(spec.note || '')}</p>`;
  }

  async function drawChartCard(card, spec, state) {
    const canvas = card.querySelector('[data-role="canvas"]');
    const params = Object.assign({ metric: state.metrics }, periodRange(state));
    try {
      const data = await api.series(spec.code, params);
      const series = data.series.map((serie, index) => ({
        code: serie.code,
        title: serie.title,
        kind: serie.kind,
        unit: serie.unit,
        digits: serie.digits,
        // Цвет закреплён за позицией показателя в каталоге, а не за порядком
        // в ответе: иначе он менялся бы при включении и выключении галочек
        color: charts.seriesColor(
          spec.metrics.findIndex((metric) => metric.code === serie.code)
        ),
        points: serie.points.map((point) => ({
          x: new Date(point.date).getTime(),
          y: point.value,
        })),
      }));

      charts.timeSeriesChart(canvas, series, {
        height: card.classList.contains('card--full') ? 520 : 260,
        emptyMessage: 'За выбранный период данных нет',
      });
    } catch (error) {
      failure(canvas, error);
    }
  }

  function mountChartCard(card, spec) {
    const state = chartState.get(spec.code) || {
      period: '12m',
      from: null,
      to: null,
      metrics: spec.metrics.filter((metric) => metric.default).map((metric) => metric.code),
    };
    chartState.set(spec.code, state);

    const redraw = () => {
      chartShell(card, spec, state);
      drawChartCard(card, spec, state);
    };

    card.addEventListener('click', (event) => {
      const periodButton = event.target.closest('[data-period]');
      if (periodButton) {
        state.period = periodButton.dataset.period;
        if (state.period === 'custom') {
          // Заполняем поля текущим окном, чтобы было от чего оттолкнуться
          const range = periodRange({ period: '12m' });
          state.from = state.from || range.date_from;
          state.to = state.to || range.date_to;
        }
        return redraw();
      }

      const action = event.target.closest('[data-action]');
      if (!action) return;
      if (action.dataset.action === 'full') {
        toggleFullscreen(card, () => drawChartCard(card, spec, state));
      } else if (action.dataset.action === 'xlsx') {
        api.seriesDownload(spec.code, Object.assign({ metric: state.metrics }, periodRange(state)))
          .then((name) => toast(`Сохранено: ${name}`))
          .catch((error) => toast(error.message, true));
      }
    });

    card.addEventListener('change', (event) => {
      const metric = event.target.closest('[data-metric]');
      if (metric) {
        const code = metric.dataset.metric;
        state.metrics = metric.checked
          ? [...state.metrics, code]
          : state.metrics.filter((item) => item !== code);
        return drawChartCard(card, spec, state);
      }
      const dateInput = event.target.closest('[data-role="from"], [data-role="to"]');
      if (dateInput) {
        // Вторую границу подставляем из текущего окна: иначе после правки
        // одной даты второе поле осталось бы пустым и период выглядел бы
        // незаданным, хотя сервер молча взял бы значение по умолчанию
        const range = periodRange(state);
        state.from = state.from || range.date_from;
        state.to = state.to || range.date_to;
        state[dateInput.dataset.role] = dateInput.value || null;
        state.period = 'custom';
        return redraw();
      }
    });

    redraw();
  }

  /** Развернуть карточку на весь экран и обратно. */
  function toggleFullscreen(card, onChange) {
    const full = card.classList.toggle('card--full');
    document.body.classList.toggle('has-full-card', full);
    if (full) {
      const escape = (event) => {
        if (event.key !== 'Escape') return;
        document.removeEventListener('keydown', escape);
        card.classList.remove('card--full');
        document.body.classList.remove('has-full-card');
        if (onChange) onChange();
      };
      document.addEventListener('keydown', escape);
    }
    if (onChange) onChange();
  }

  /** Кнопки «на весь экран» у карточек, которые рисуются не по каталогу. */
  function bindStaticFullscreen() {
    $$('[data-fullscreen]').forEach((button) => {
      if (button.dataset.bound) return;
      button.dataset.bound = '1';
      button.addEventListener('click', () => {
        const card = document.getElementById(button.dataset.fullscreen);
        if (card) toggleFullscreen(card, () => renderCurve(state.lastCurve));
      });
    });
  }

  let seriesCatalog = null;

  async function renderSeriesCharts() {
    const cards = $$('.chart-card');
    if (!cards.length) return;
    try {
      if (!seriesCatalog) seriesCatalog = await api.seriesCatalog();
    } catch (error) {
      cards.forEach((card) => failure(card, error));
      return;
    }
    cards.forEach((card) => {
      const spec = seriesCatalog.find((item) => item.code === card.dataset.chart);
      if (!spec) return;
      // Обработчики вешаются один раз: карточка перерисовывает себя сама
      if (card.dataset.mounted) {
        const state = chartState.get(spec.code);
        return void drawChartCard(card, spec, state);
      }
      card.dataset.mounted = '1';
      mountChartCard(card, spec);
    });
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
        { title: 'Оборот, ₽', className: 'num', sortBy: 'turnover', render: (row) => fmt.money(row.turnover) },
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

      rememberRows('instruments', data.items);
      renderTable(container, [
        pickColumn('instruments'),
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'ISIN', render: (row) => `<span class="dim" style="font-family:var(--mono);font-size:11px">${fmt.esc(row.isin || '—')}</span>` },
        { title: 'Цена', className: 'num', render: (row) => fmt.price(row.last) },
        { title: 'Изм.', className: 'num', render: (row) => changeCell(row.change_pct) },
        { title: 'Оборот, ₽', className: 'num', render: (row) => fmt.money(row.turnover) },
        { title: 'Объём, шт', className: 'num', render: (row) => fmt.int(row.volume) },
        { title: 'Сделок', className: 'num', render: (row) => fmt.int(row.num_trades) },
        { title: 'Спред', className: 'num', render: (row) => fmt.pct(row.spread_pct, 3) },
        { title: 'Ликвидность', className: 'num', render: (row) => liquidityCell(row.liquidity_score) },
        buyColumn('instruments'),
      ], data.items, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Ничего не найдено — ослабьте фильтры',
      });
      wirePicking(container, 'instruments');
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
      // Сортировка идёт на сервере: он видит весь рынок, а на экране
      // только первые триста строк — иначе «сверху» оказалось бы не то
      sort_by: state.analysisSort.by || $('#a-sort').value,
      order: state.analysisSort.order,
      limit: 300,
    };
    const coupon = pick('#a-coupon');
    if (coupon) params.coupon_type = [coupon];
    const benchmark = pick('#a-benchmark');
    if (benchmark) params.benchmark = [benchmark];
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
        // Базы купона — только встретившиеся в данных
        const benchmarks = $('#a-benchmark');
        (options.benchmarks || []).forEach((item) => {
          const option = document.createElement('option');
          option.value = item.code;
          option.textContent = item.title;
          benchmarks.appendChild(option);
        });
      }

      const data = await api.bondAnalysis(analysisParams());
      state.analysisLoaded = true;
      $('#analysis-count').textContent =
        `${fmt.int(data.total)} ${fmt.plural(data.total, 'выпуск', 'выпуска', 'выпусков')}` +
        (data.curve_date ? ` · КБД на ${fmt.date(data.curve_date)}` : '');

      rememberRows('analysis', data.items);
      renderTable(container, [
        pickColumn('analysis'),
        { title: 'Выпуск', render: (row) => secCell(row) },
        { title: 'ISIN', render: (row) => `<span class="dim" style="font-family:var(--mono);font-size:11px">${fmt.esc(row.isin || '—')}</span>` },
        { title: 'Погашение', sortBy: 'maturity_date', render: (row) => `<span class="dim">${fmt.date(row.maturity_date)}</span>` },
        { title: 'Лет', className: 'num', sortBy: 'years_to_maturity', render: (row) => fmt.num(row.years_to_maturity, 2) },
        { title: 'Цена, %', className: 'num', sortBy: 'last', render: (row) => fmt.price(row.last) },
        { title: 'СВЦ вчера', className: 'num', sortBy: 'prev_wa_price', render: (row) => fmt.price(row.prev_wa_price) },
        {
          title: 'НКД расч.',
          className: 'num',
          render: (row) =>
            `<span title="${row.settle_date ? 'на дату расчётов ' + fmt.date(row.settle_date) : 'на дату расчётов'}">${fmt.num(row.accrued_interest, 2)}</span>`,
        },
        {
          title: 'НКД сегодня',
          className: 'num',
          render: (row) => {
            if (!fmt.isNum(row.accrued_today)) return '<span class="dim">—</span>';
            // У флоатера ставка меняется внутри периода: значение на любую
            // дату, кроме расчётной, — оценка, и это должно быть видно
            const hint = row.accrued_estimate
              ? `Оценка: плавающий купон, ставка меняется внутри периода. Прошло ${row.accrued_days_passed} дн., до купона ${row.accrued_days_left} дн.`
              : `Прошло ${row.accrued_days_passed} дн., до купона ${row.accrued_days_left} дн.`;
            const value = fmt.num(row.accrued_today, 2);
            return `<span title="${fmt.esc(hint)}">${
              row.accrued_estimate ? `<span class="approx">≈</span>${value}` : value
            }</span>`;
          },
        },
        { title: 'Полная цена', className: 'num', sortBy: 'dirty_price', render: (row) => fmt.num(row.dirty_price, 2) },
        { title: 'Доходность', className: 'num', sortBy: 'yield_pct', render: (row) => `<b>${fmt.pct(row.yield_pct)}</b>` },
        { title: 'Текущая', className: 'num', sortBy: 'current_yield_pct', render: (row) => fmt.pct(row.current_yield_pct) },
        {
          title: 'После налога',
          className: 'num',
          render: (row) => `<span class="dim">${fmt.pct(row.after_tax_yield_pct)}</span>`,
        },
        { title: 'Премия', className: 'num', sortBy: 'spread_to_curve_bp', render: (row) => premiumCell(row.spread_to_curve_bp) },
        { title: 'Дюрация', className: 'num', sortBy: 'duration_years', render: (row) => (fmt.isNum(row.duration_years) ? fmt.num(row.duration_years, 2) + ' л' : '—') },
        { title: 'Тип купона', render: (row) => `<span class="badge">${fmt.esc(row.coupon_type_title || '—')}</span>` },
        {
          title: 'База купона',
          sortBy: 'coupon_benchmark',
          render: (row) => {
            if (row.coupon_base) {
              return `<span class="badge badge--accent" title="Плавающий купон привязан к этой ставке">${fmt.esc(row.coupon_base)}</span>`;
            }
            // Пусто по двум разным причинам, и их стоит различать
            const floating = row.coupon_type === 'float' || row.coupon_type === 'structured';
            return floating
              ? '<span class="dim" title="Карточка выпуска ещё не запрошена — база появится после следующего сбора">загружается…</span>'
              : '<span class="dim">—</span>';
          },
        },
        { title: 'Аморт.', render: (row) => (row.has_amortization ? '<span class="badge badge--accent">да</span>' : '<span class="dim">нет</span>') },
        { title: 'Оферта', render: (row) => (row.has_offer ? `<span class="badge badge--warn">${row.offer_date ? fmt.date(row.offer_date) : 'есть'}</span>` : '<span class="dim">нет</span>') },
        { title: 'Оборот, ₽', className: 'num', render: (row) => fmt.money(row.turnover) },
        { title: 'Ликв.', className: 'num', sortBy: 'liquidity_score', render: (row) => liquidityCell(row.liquidity_score) },
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
        buyColumn('analysis'),
      ], data.items, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Нет выпусков под заданные условия — ослабьте фильтры',
        sort: state.analysisSort,
        onSort: (by, order) => {
          state.analysisSort = { by, order };
          // Выпадающий список сортировки держим в согласии с таблицей
          const select = $('#a-sort');
          if (select && [...select.options].some((o) => o.value === by)) select.value = by;
          renderAnalysis();
        },
      });
      wirePicking(container, 'analysis');

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
  // Добавление бумаг в портфель прямо из витрины
  // ------------------------------------------------------------------
  /**
   * Открыть окно ввода количества.
   * Цена и НКД подставляются из текущего среза, чтобы не искать их вручную.
   */
  function openBuyModal(scope, secids) {
    const rows = (secids || [])
      .map((secid) => (state.rows[scope] || {})[secid])
      .filter(Boolean);
    if (!rows.length) return toast('Не удалось определить бумаги', true);

    state.buyRows = rows;
    $('#buy-sub').textContent =
      rows.length === 1
        ? `${rows[0].secid} · ${rows[0].name || ''}`
        : `Выбрано ${rows.length} ${fmt.plural(rows.length, 'бумага', 'бумаги', 'бумаг')}`;
    $('#buy-check').innerHTML = '';
    $('#buy-date').value = new Date().toISOString().slice(0, 10);

    renderTable($('#buy-rows'), [
      { title: 'Бумага', render: (row) => secCell(row) },
      {
        title: 'Цена',
        className: 'num',
        render: (row) =>
          `<input class="qty-input" data-price="${fmt.esc(row.secid)}" type="number" step="any" min="0" value="${row.last ?? row.wa_price ?? ''}">`,
      },
      {
        title: 'НКД',
        className: 'num',
        render: (row) =>
          row.kind === 'bond'
            ? `<input class="qty-input" data-nkd="${fmt.esc(row.secid)}" type="number" step="any" min="0" value="${row.accrued_interest ?? 0}">`
            : '<span class="dim">—</span>',
      },
      {
        title: 'Количество',
        className: 'num',
        render: (row) =>
          `<input class="qty-input" data-qty="${fmt.esc(row.secid)}" type="number" step="any" min="0" placeholder="0">`,
      },
      { title: 'Лот', className: 'num', render: (row) => fmt.int(row.lot_size) },
      { title: 'Сумма, ₽', className: 'num', render: (row) => `<span data-sum="${fmt.esc(row.secid)}" class="dim">—</span>` },
    ], rows, { emptyMessage: 'Нечего добавлять' });

    // Пересчёт суммы при вводе: у облигаций цена в процентах от номинала
    const recalc = () => {
      let total = 0;
      state.buyRows.forEach((row) => {
        const price = parseFloat(($(`[data-price="${row.secid}"]`) || {}).value) || 0;
        const nkd = parseFloat(($(`[data-nkd="${row.secid}"]`) || {}).value) || 0;
        const qty = parseFloat(($(`[data-qty="${row.secid}"]`) || {}).value) || 0;
        const multiplier =
          row.kind === 'bond' && row.face_value ? row.face_value / 100 : 1;
        const fx = row.fx_rate || 1;
        const sum = qty * price * multiplier * fx + qty * nkd;
        const cell = $(`[data-sum="${row.secid}"]`);
        if (cell) {
          cell.textContent = qty ? fmt.money(sum) : '—';
          cell.className = qty ? '' : 'dim';
        }
        total += sum;
      });
      $('#buy-total').textContent = total ? `Итого ${fmt.rub(total)}` : '';
      state.buyTotal = total;
    };

    $$('#buy-rows input').forEach((input) => input.addEventListener('input', recalc));
    recalc();

    $('#buy-modal').hidden = false;
    const firstQty = $('#buy-rows [data-qty]');
    if (firstQty) firstQty.focus();
  }

  function closeBuyModal() {
    $('#buy-modal').hidden = true;
    state.buyRows = [];
  }

  /** Собрать сделки из окна. Бумаги с нулевым количеством пропускаем. */
  function collectBuyDeals() {
    const side = $('#buy-side').value;
    const tradeDate = $('#buy-date').value;
    const portfolio = $('#buy-portfolio').value.trim() || 'Основной';
    const fee = parseFloat($('#buy-fee').value) || 0;

    return state.buyRows
      .map((row) => {
        const quantity = parseFloat(($(`[data-qty="${row.secid}"]`) || {}).value) || 0;
        const price = parseFloat(($(`[data-price="${row.secid}"]`) || {}).value) || 0;
        const nkd = parseFloat(($(`[data-nkd="${row.secid}"]`) || {}).value) || 0;
        if (quantity <= 0 || price <= 0) return null;
        return {
          secid: row.secid,
          side,
          quantity,
          price,
          accrued_interest: nkd,
          fee,
          trade_date: tradeDate,
          portfolio,
        };
      })
      .filter(Boolean);
  }

  async function verifyBuyAgainstLimits() {
    const deals = collectBuyDeals();
    const box = $('#buy-check');
    if (!deals.length) {
      box.innerHTML = '<div class="warnings"><div class="warnings__item">Укажите количество хотя бы по одной бумаге</div></div>';
      return;
    }

    box.innerHTML = '<div class="empty">Проверяю лимиты…</div>';
    try {
      // Лимиты проверяются по каждой бумаге отдельно: сервер считает
      // гипотетическую позицию поверх текущего портфеля
      const results = await Promise.all(
        deals.map((deal) =>
          api.previewTrade({
            secid: deal.secid,
            quantity: deal.quantity,
            price: deal.price,
            portfolio: deal.portfolio,
          })
        )
      );

      const problems = results.flatMap((result, index) =>
        result.new_breaches.map((row) => ({ secid: deals[index].secid, ...row }))
      );

      box.innerHTML = problems.length
        ? `<div class="warnings">${problems
            .map((row) => `<div class="warnings__item">${fmt.esc(row.secid)} — ${fmt.esc(row.kind_title)} (${fmt.esc(row.subject)}): ${fmt.num(row.actual, 1)} при лимите ${fmt.num(row.limit_value, 1)}</div>`)
            .join('')}</div>`
        : '<div class="warnings" style="background:var(--accent-soft)"><div class="warnings__item" style="color:var(--accent-strong)">Лимиты соблюдены</div></div>';
    } catch (error) {
      box.innerHTML = `<div class="warnings"><div class="warnings__item">${fmt.esc(error.message)}</div></div>`;
    }
  }

  async function submitBuy() {
    const deals = collectBuyDeals();
    if (!deals.length) {
      return toast('Укажите количество хотя бы по одной бумаге', true);
    }

    const button = $('#buy-submit');
    button.disabled = true;
    button.textContent = 'Добавляю…';
    try {
      const result = await api.createDealsBulk({ deals });
      if (result.error_count) {
        const details = result.errors
          .map((row) => `${row.secid}: ${row.detail}`)
          .join('; ');
        toast(`Добавлено ${result.created_count}, не удалось ${result.error_count} — ${details}`, true);
      } else {
        toast(`Добавлено ${result.created_count} ${fmt.plural(result.created_count, 'сделка', 'сделки', 'сделок')}`);
      }

      // Отметки сбрасываем: бумаги уже в портфеле
      state.picked = { instruments: {}, analysis: {} };
      refreshPickState('instruments');
      refreshPickState('analysis');
      closeBuyModal();

      // Портфель пересчитается при следующем открытии вкладки
      state.loaded.portfolio = false;
      if (state.view === 'portfolio') renderPortfolio();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = 'Добавить сделки';
    }
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
              <label class="check"${item.hint ? ` title="${fmt.esc(item.hint)}"` : ''}>
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

      // Выходные и сегодняшний день: торгов не было, но НКД накопился.
      // Приглушаем строку, чтобы прочерки в ценах не выглядели пропажей данных.
      container.querySelectorAll('tbody tr').forEach((tr, index) => {
        if (data.rows[index] && data.rows[index].no_trades) tr.classList.add('row--quiet');
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
      renderHistory();
    } catch (error) {
      failure(kpi, error);
    }
  }

  // ------------------------------------------------------------------
  // История стоимости портфеля
  // ------------------------------------------------------------------
  async function renderHistory() {
    const container = $('#history-chart');
    if (!container) return;
    loading(container);

    try {
      const data = await api.portfolioHistory(state.portfolioName, state.historyDays);
      const note = $('#history-note');
      if (note) note.textContent = data.note;

      const summary = $('#history-summary');
      if (summary) {
        summary.innerHTML = data.snapshots
          ? `${data.snapshots} ${fmt.plural(data.snapshots, 'снимок', 'снимка', 'снимков')} ` +
            `с ${fmt.date(data.first_date)}` +
            (fmt.isNum(data.total_return_pct)
              ? ` · <span class="${fmt.trendClass(data.total_return_pct)}">${fmt.signedPct(data.total_return_pct)}</span>`
              : '') +
            (fmt.isNum(data.max_drawdown_pct) && data.max_drawdown_pct < 0
              ? ` · просадка <span class="down">${fmt.pct(data.max_drawdown_pct)}</span>`
              : '')
          : '<span class="dim">снимков пока нет</span>';
      }

      charts.lineChart(
        container,
        [
          {
            name: 'Стоимость',
            points: data.points.map((point) => ({
              x: new Date(point.date).getTime(),
              y: point.total_value,
              label: `${fmt.date(point.date)}: ${fmt.rub(point.total_value)}`,
            })),
          },
        ],
        {
          height: 220,
          yFormat: (v) => fmt.money(v),
          xFormat: (v) => fmt.dateShort(v),
          emptyMessage:
            'Снимки накапливаются со дня запуска. Нажмите «Зафиксировать стоимость», чтобы записать первую точку.',
        }
      );
    } catch (error) {
      failure(container, error);
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
      renderOffers();

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

  /** Ближайшие оферты по своим бумагам. */
  async function renderOffers() {
    const container = $('#offers-table');
    if (!container) return;
    try {
      const rows = await api.offers(state.portfolioName, 180);
      renderTable(container, [
        { title: 'Выпуск', render: (row) => secCell(row) },
        { title: 'Дата оферты', render: (row) => fmt.date(row.offer_date) },
        {
          title: 'Осталось',
          className: 'num',
          render: (row) =>
            `<span class="badge badge--${row.severity === 'critical' ? 'down' : 'warn'}">` +
            `${row.days_left} ${fmt.plural(row.days_left, 'день', 'дня', 'дней')}</span>`,
        },
        { title: 'Количество', className: 'num', render: (row) => fmt.int(row.quantity) },
        { title: 'Оценка', className: 'num', render: (row) => fmt.rub(row.market_value_rub) },
        {
          title: 'Приём заявок',
          render: (row) =>
            row.accept_from || row.accept_until
              ? `<span class="dim">${row.accept_from ? fmt.date(row.accept_from) : '—'} — ${row.accept_until ? fmt.date(row.accept_until) : '—'}</span>`
              : '<span class="dim">уточняйте у брокера</span>',
        },
        { title: 'Источник', render: (row) => `<span class="dim">${fmt.esc(row.source)}</span>` },
      ], rows, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Оферт по вашим бумагам в ближайшие полгода нет',
      });
    } catch (error) {
      failure(container, error);
    }
  }

  // ------------------------------------------------------------------
  // Деньги
  // ------------------------------------------------------------------
  const PLACEMENT_TITLES = {
    deposit: 'депозит', repo: 'РЕПО', reverse_repo: 'обратное РЕПО', loan: 'кредит',
  };

  //: Виды движений, которые заводят руками. Расчётные виды приходят из
  //: календаря уже с готовым названием в kind_title.
  const MANUAL_FLOW_TITLES = {
    deposit: 'пополнение', withdrawal: 'вывод', fee: 'комиссия',
    tax: 'налог', other: 'прочее',
  };

  async function renderCash() {
    const kpi = $('#cash-kpi');
    loading(kpi);

    try {
      const [position, calendar, accounts, placements, flows] = await Promise.all([
        api.cashPosition(state.portfolioName),
        api.cashCalendar(state.portfolioName, state.cashHorizon),
        api.cashAccounts(state.portfolioName),
        api.placements(state.portfolioName),
        api.cashFlows({ limit: 40 }),
      ]);

      state.accounts = accounts;
      fillAccountSelect(accounts);

      const cards = [
        {
          label: 'Свободные деньги',
          value: fmt.rub(position.total_cash_rub),
          meta: `${accounts.length} ${fmt.plural(accounts.length, 'счёт', 'счёта', 'счетов')}`,
        },
        {
          label: 'Размещено',
          value: fmt.rub(position.placed_out_rub),
          meta: fmt.isNum(position.weighted_placement_rate)
            ? `средняя ставка ${fmt.pct(position.weighted_placement_rate)}`
            : 'депозиты и обратное РЕПО',
        },
        {
          label: 'Начислено процентов',
          value: fmt.rub(position.accrued_interest_rub),
          meta: 'с начала размещений, база 365 дней',
        },
        {
          label: 'Привлечено',
          value: fmt.rub(position.borrowed_rub),
          meta: 'РЕПО и кредиты — вернуть придётся',
        },
        {
          label: 'Итого ликвидность',
          value: fmt.rub(position.total_liquidity_rub),
          meta: 'деньги плюс размещения минус долг',
        },
        {
          label: 'Минимальный остаток',
          value: `<span class="${calendar.has_gap ? 'down' : ''}">${fmt.rub(calendar.lowest_balance)}</span>`,
          meta: calendar.has_gap
            ? `кассовый разрыв ${fmt.date(calendar.gap_date)}`
            : `за ${state.cashHorizon} дней разрывов нет`,
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

      const gapHint = $('#cash-gap-hint');
      if (gapHint) {
        gapHint.innerHTML = calendar.has_gap
          ? `<span class="down">Разрыв ${fmt.date(calendar.gap_date)}</span>`
          : `<span class="dim">Остаток на конец периода ${fmt.rub(calendar.closing_balance)}</span>`;
      }

      // Остаток нарастающим итогом: видно, где кривая уходит под ноль
      charts.lineChart(
        $('#cash-calendar-chart'),
        [{
          name: 'Остаток',
          points: calendar.events.map((event) => ({
            x: new Date(event.flow_date).getTime(),
            y: event.balance_after,
            label: `${fmt.date(event.flow_date)}: ${fmt.rub(event.balance_after)}`,
          })),
        }],
        {
          height: 200,
          yFormat: (v) => fmt.money(v),
          xFormat: (v) => fmt.dateShort(new Date(v)),
          emptyMessage: 'Ожидаемых движений денег нет',
        }
      );

      renderTable($('#cash-calendar-table'), [
        { title: 'Дата', render: (row) => fmt.date(row.flow_date) },
        {
          title: 'Тип',
          render: (row) => `<span class="badge">${fmt.esc(row.kind_title || row.kind)}</span>`,
        },
        { title: 'Основание', render: (row) => fmt.esc(row.comment || '—') },
        {
          title: 'Сумма',
          className: 'num',
          render: (row) => `<span class="${fmt.trendClass(row.amount)}">${fmt.rub(row.amount)}</span>`,
        },
        {
          title: 'Остаток после',
          className: 'num',
          render: (row) =>
            `<span class="${row.balance_after < 0 ? 'down' : ''}">${fmt.rub(row.balance_after)}</span>`,
        },
      ], calendar.events, { emptyMessage: 'Движений в этом горизонте нет' });

      renderTable($('#accounts-table'), [
        { title: 'Счёт', render: (row) => fmt.esc(row.name) },
        { title: 'Банк', render: (row) => `<span class="dim">${fmt.esc(row.bank || '—')}</span>` },
        { title: 'Валюта', render: (row) => fmt.esc(row.currency) },
        { title: 'Остаток', className: 'num', render: (row) => fmt.num(row.balance, 2) },
        { title: 'В рублях', className: 'num', render: (row) => fmt.rub(row.balance_rub) },
        {
          title: '',
          className: 'num',
          render: (row) => `<button class="btn btn--ghost" data-drop-account="${row.id}" title="Удалить счёт">×</button>`,
        },
      ], position.accounts, { emptyMessage: 'Счетов пока нет' });

      bindRemoval('#accounts-table', 'dropAccount', api.deleteAccount, renderCash, 'Счёт удалён');

      renderTable($('#flows-table'), [
        { title: 'Дата', render: (row) => fmt.date(row.flow_date) },
        {
          title: 'Вид',
          render: (row) =>
            `<span class="badge">${fmt.esc(MANUAL_FLOW_TITLES[row.kind] || row.kind)}</span>` +
            (row.is_planned ? ' <span class="badge badge--warn">план</span>' : ''),
        },
        {
          title: 'Сумма',
          className: 'num',
          render: (row) => `<span class="${fmt.trendClass(row.amount)}">${fmt.num(row.amount, 2)}</span>`,
        },
        { title: 'Основание', render: (row) => `<span class="dim">${fmt.esc(row.comment || '—')}</span>` },
        {
          title: '',
          className: 'num',
          render: (row) => `<button class="btn btn--ghost" data-drop-flow="${row.id}" title="Удалить">×</button>`,
        },
      ], flows, { emptyMessage: 'Движений нет' });

      bindRemoval('#flows-table', 'dropFlow', api.deleteFlow, renderCash, 'Движение удалено');

      renderTable($('#placements-table'), [
        {
          title: 'Вид',
          render: (row) => `<span class="badge">${PLACEMENT_TITLES[row.kind] || row.kind}</span>`,
        },
        { title: 'Контрагент', render: (row) => fmt.esc(row.counterparty || '—') },
        {
          title: 'Сумма',
          className: 'num',
          render: (row) => `${fmt.num(row.amount, 2)} ${fmt.esc(row.currency)}`,
        },
        { title: 'Ставка', className: 'num', render: (row) => fmt.pct(row.rate) },
        { title: 'Начало', render: (row) => fmt.date(row.start_date) },
        { title: 'Возврат', render: (row) => fmt.date(row.end_date) },
        {
          title: 'Осталось',
          className: 'num',
          render: (row) =>
            fmt.isNum(row.days_left)
              ? `${row.days_left} ${fmt.plural(row.days_left, 'день', 'дня', 'дней')}`
              : '<span class="dim">—</span>',
        },
        { title: 'Начислено', className: 'num', render: (row) => fmt.num(row.accrued_interest, 2) },
        { title: 'К возврату', className: 'num', render: (row) => fmt.num(row.total_at_maturity, 2) },
        {
          title: '',
          className: 'num',
          render: (row) => `<button class="btn btn--ghost" data-drop-placement="${row.id}" title="Удалить">×</button>`,
        },
      ], position.placements, { emptyMessage: 'Размещений нет' });

      bindRemoval('#placements-table', 'dropPlacement', api.deletePlacement, renderCash, 'Размещение удалено');
    } catch (error) {
      failure(kpi, error);
    }
  }

  /** Кнопки удаления в таблице: один обработчик на все строки. */
  function bindRemoval(selector, datasetKey, remove, reload, message) {
    const container = $(selector);
    if (!container) return;
    container.querySelectorAll(`[data-${datasetKey.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase())}]`)
      .forEach((button) => {
        button.addEventListener('click', async (event) => {
          event.stopPropagation();
          try {
            await remove(button.dataset[datasetKey]);
            toast(message);
            reload();
          } catch (error) {
            toast(error.message, true);
          }
        });
      });
  }

  function fillAccountSelect(accounts) {
    const select = $('#fl-account');
    if (!select) return;
    const previous = select.value;
    select.innerHTML = (accounts || [])
      .map((account) => `<option value="${account.id}">${fmt.esc(account.name)} (${fmt.esc(account.currency)})</option>`)
      .join('') || '<option value="">Сначала откройте счёт</option>';
    if (previous) select.value = previous;
  }

  async function submitAccount(event) {
    event.preventDefault();
    const message = $('#account-msg');
    try {
      await api.createAccount({
        name: $('#ac-name').value.trim(),
        currency: $('#ac-currency').value,
        bank: $('#ac-bank').value.trim() || null,
        portfolio: $('#ac-portfolio').value.trim() || state.portfolioName || null,
      });
      message.textContent = 'Счёт открыт';
      message.className = 'form-msg form-msg--ok';
      $('#account-form').reset();
      $('#account-form').hidden = true;
      renderCash();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  async function submitFlow(event) {
    event.preventDefault();
    const message = $('#flow-msg');
    const accountId = Number($('#fl-account').value);
    if (!accountId) {
      message.textContent = 'Сначала откройте счёт';
      message.className = 'form-msg form-msg--err';
      return;
    }
    try {
      await api.createFlow({
        account_id: accountId,
        flow_date: $('#fl-date').value,
        amount: Number($('#fl-amount').value),
        kind: $('#fl-kind').value,
        is_planned: $('#fl-planned').checked,
        comment: $('#fl-comment').value.trim() || null,
      });
      message.textContent = 'Движение записано';
      message.className = 'form-msg form-msg--ok';
      $('#fl-amount').value = '';
      $('#fl-comment').value = '';
      renderCash();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  async function submitPlacement(event) {
    event.preventDefault();
    const message = $('#placement-msg');
    try {
      await api.createPlacement({
        kind: $('#pl-kind').value,
        counterparty: $('#pl-counterparty').value.trim() || null,
        amount: Number($('#pl-amount').value),
        currency: $('#pl-currency').value,
        rate: Number($('#pl-rate').value),
        start_date: $('#pl-start').value,
        end_date: $('#pl-end').value,
        collateral_secid: $('#pl-collateral').value.trim().toUpperCase() || null,
        portfolio: state.portfolioName || null,
      });
      message.textContent = 'Размещение записано';
      message.className = 'form-msg form-msg--ok';
      $('#placement-form').reset();
      $('#placement-form').hidden = true;
      renderCash();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  // ------------------------------------------------------------------
  // Импорт и сверка
  // ------------------------------------------------------------------
  async function renderImports() {
    const container = $('#import-columns');
    if (!container) return;
    try {
      const columns = await api.importColumns();
      renderTable(container, [
        { title: 'Поле', render: (row) => fmt.esc(row.title) },
        {
          title: 'Обязательное',
          render: (row) =>
            row.required
              ? '<span class="badge badge--warn">да</span>'
              : '<span class="dim">нет</span>',
        },
        {
          title: 'Подойдут заголовки со словами',
          render: (row) =>
            row.hints.map((hint) => `<span class="badge">${fmt.esc(hint)}</span>`).join(' '),
        },
      ], columns, { emptyMessage: 'Нет данных' });
    } catch (error) {
      failure(container, error);
    }
  }

  /** Разбор выбранного файла со сделками. */
  async function previewImportFile(file) {
    if (!file) return;
    const message = $('#import-msg');
    const container = $('#import-preview');
    $('#import-filename').textContent = file.name;
    loading(container);
    message.textContent = '';
    $('#import-apply').disabled = true;

    try {
      const result = await api.importPreview(file, $('#import-portfolio').value.trim() || null);
      state.importDeals = result.deals || [];

      if (result.errors && result.errors.length) {
        message.textContent = result.errors.join('; ');
        message.className = 'form-msg form-msg--err';
      } else {
        message.innerHTML = `Разобрано ${result.total}, из них годных <b>${result.valid}</b>` +
          (result.invalid ? `, с ошибками ${result.invalid}` : '');
        message.className = 'form-msg' + (result.invalid ? '' : ' form-msg--ok');
      }
      $('#import-apply').disabled = !result.valid;

      renderTable(container, [
        { title: 'Стр.', className: 'num', render: (row) => `<span class="dim">${row.line}</span>` },
        { title: 'Дата', render: (row) => fmt.date(row.trade_date) },
        { title: 'Бумага', render: (row) => fmt.esc(row.secid || '—') },
        {
          title: 'Направление',
          render: (row) =>
            `<span class="badge badge--${row.side === 'buy' ? 'up' : 'down'}">${row.side === 'buy' ? 'покупка' : 'продажа'}</span>`,
        },
        { title: 'Кол-во', className: 'num', render: (row) => fmt.int(row.quantity) },
        { title: 'Цена', className: 'num', render: (row) => fmt.num(row.price, 4) },
        { title: 'НКД', className: 'num', render: (row) => fmt.num(row.accrued_interest, 2) },
        { title: 'Комиссия', className: 'num', render: (row) => fmt.num(row.fee, 2) },
        { title: 'Портфель', render: (row) => fmt.esc(row.portfolio) },
        {
          title: 'Замечания',
          render: (row) =>
            row.problems.length
              ? `<span class="problems">${fmt.esc(row.problems.join('; '))}</span>`
              : '<span class="up">—</span>',
        },
      ], state.importDeals, { emptyMessage: 'Строк не найдено' });

      // Строки с замечаниями подсвечиваем: их не запишут
      container.querySelectorAll('tbody tr').forEach((tr, index) => {
        if (state.importDeals[index] && !state.importDeals[index].ok) tr.classList.add('row--bad');
      });
    } catch (error) {
      failure(container, error);
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  async function applyImport() {
    const good = (state.importDeals || []).filter((deal) => deal.ok);
    if (!good.length) return;

    const button = $('#import-apply');
    button.disabled = true;
    try {
      const result = await api.importApply(good);
      toast(`Загружено сделок: ${result.created}` +
        (result.skipped_count ? `, пропущено ${result.skipped_count}` : ''));
      state.importDeals = [];
      $('#import-preview').innerHTML = '<div class="empty">Сделки загружены в журнал</div>';
      // Портфель пересчитан — его вкладку нужно перерисовать заново
      state.loaded.portfolio = false;
      state.loaded.cash = false;
      await loadPortfolioNames();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  }

  async function runReconcile(file) {
    if (!file) return;
    const container = $('#recon-result');
    const message = $('#recon-msg');
    $('#recon-filename').textContent = file.name;
    loading(container);

    try {
      const result = await api.reconcile(file, state.portfolioName);
      if (result.errors && result.errors.length) {
        message.textContent = result.errors.join('; ');
        message.className = 'form-msg form-msg--err';
      } else {
        message.textContent = `Совпало ${result.matched_count}, расхождений ${result.difference_count}`;
        message.className = 'form-msg' + (result.difference_count ? ' form-msg--err' : ' form-msg--ok');
      }

      const rows = [...result.differences, ...result.matched];
      renderTable(container, [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'В терминале', className: 'num', render: (row) => fmt.int(row.terminal_quantity) },
        { title: 'В выписке', className: 'num', render: (row) => fmt.int(row.statement_quantity) },
        {
          title: 'Разница',
          className: 'num',
          render: (row) =>
            `<span class="${fmt.trendClass(row.difference)}">${row.difference > 0 ? '+' : ''}${fmt.int(row.difference)}</span>`,
        },
        {
          title: 'Статус',
          render: (row) =>
            row.status === 'совпадает'
              ? '<span class="badge badge--up">совпадает</span>'
              : `<span class="badge badge--down">${fmt.esc(row.status)}</span>`,
        },
      ], rows, { emptyMessage: 'Сравнивать нечего' });

      container.querySelectorAll('tbody tr').forEach((tr, index) => {
        if (rows[index] && rows[index].status !== 'совпадает') tr.classList.add('row--bad');
      });
    } catch (error) {
      failure(container, error);
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  /** Перетаскивание файла в область загрузки. */
  function wireDropzone(zoneSelector, inputSelector, pickSelector, handler) {
    const zone = $(zoneSelector);
    const input = $(inputSelector);
    if (!zone || !input) return;

    on(pickSelector, 'click', () => input.click());
    input.addEventListener('change', () => handler(input.files[0]));

    ['dragenter', 'dragover'].forEach((name) =>
      zone.addEventListener(name, (event) => {
        event.preventDefault();
        zone.classList.add('is-over');
      })
    );
    ['dragleave', 'drop'].forEach((name) =>
      zone.addEventListener(name, () => zone.classList.remove('is-over'))
    );
    zone.addEventListener('drop', (event) => {
      event.preventDefault();
      const file = event.dataTransfer && event.dataTransfer.files[0];
      if (file) handler(file);
    });
  }

  // ------------------------------------------------------------------
  // Настройки: доступ, уведомления, налоги, журнал
  // ------------------------------------------------------------------
  const ROLE_TITLES = { viewer: 'Просмотр', trader: 'Сделки и лимиты', admin: 'Администратор' };

  async function renderAdmin() {
    const info = $('#auth-info');
    loading(info);

    try {
      const [mode, taxes, events] = await Promise.all([
        api.authMode(), api.taxes(), api.events(state.portfolioName),
      ]);

      $('#auth-state').innerHTML = mode.auth_enabled
        ? '<span class="badge badge--up">вход включён</span>'
        : '<span class="badge badge--warn">вход выключен</span>';

      info.innerHTML = `<p class="card__note">${fmt.esc(mode.note)}</p>`;

      $('#taxes-info').innerHTML = `
        <div class="kpi-grid">
          <div class="kpi">
            <div class="kpi__label">Налог на прибыль</div>
            <div class="kpi__value">${fmt.pct(taxes.profit_tax_pct)}</div>
            <div class="kpi__meta">на прирост стоимости</div>
          </div>
          <div class="kpi">
            <div class="kpi__label">Налог на купон</div>
            <div class="kpi__value">${fmt.pct(taxes.coupon_tax_pct)}</div>
            <div class="kpi__meta">на купонный доход</div>
          </div>
        </div>
        <p class="card__note">${fmt.esc(taxes.note)}</p>`;

      $('#events-list').innerHTML = events.length
        ? events
            .map((event) => `
              <div class="alert">
                <span class="alert__dot alert__dot--${event.severity}"></span>
                <div>
                  <div class="alert__title">${fmt.esc(event.title)}</div>
                  <div class="alert__detail">${fmt.esc(event.detail)}</div>
                </div>
              </div>`)
            .join('')
        : '<div class="empty">Событий нет — рассылать нечего</div>';

      // Пользователи, правила и журнал доступны только администратору:
      // при роли ниже сервер ответит 403, и это не ошибка интерфейса
      await Promise.all([renderUsers(), renderRules(), renderAudit()]);
    } catch (error) {
      failure(info, error);
    }
  }

  function denied(container, error) {
    if (!container) return;
    const forbidden = /Недостаточно прав|вход в систему/i.test(error.message || '');
    container.innerHTML = `<div class="empty">${
      forbidden ? fmt.esc(error.message) : 'Не удалось загрузить: ' + fmt.esc(error.message)
    }</div>`;
  }

  async function renderUsers() {
    const container = $('#users-table');
    try {
      const users = await api.users();
      $('#user-form').hidden = false;
      $('#password-form').hidden = false;
      renderTable(container, [
        { title: 'Логин', render: (row) => fmt.esc(row.login) },
        { title: 'Имя', render: (row) => `<span class="dim">${fmt.esc(row.full_name || '—')}</span>` },
        { title: 'Роль', render: (row) => `<span class="badge">${ROLE_TITLES[row.role] || row.role}</span>` },
        {
          title: 'Статус',
          render: (row) =>
            row.active
              ? '<span class="badge badge--up">работает</span>'
              : '<span class="badge badge--down">отключён</span>',
        },
        {
          title: '',
          className: 'num',
          render: (row) => `<button class="btn btn--ghost" data-drop-user="${row.id}" title="Отключить доступ">×</button>`,
        },
      ], users, { emptyMessage: 'Пользователей нет' });
      bindRemoval('#users-table', 'dropUser', api.disableUser, renderUsers, 'Доступ отключён');
    } catch (error) {
      $('#user-form').hidden = true;
      // Свой пароль меняет любой вошедший, поэтому форму оставляем —
      // прячем только если вход вообще выключен
      $('#password-form').hidden = !state.authEnabled;
      denied(container, error);
    }
  }

  async function renderRules() {
    const container = $('#rules-table');
    try {
      const rules = await api.notificationRules();
      renderTable(container, [
        { title: 'Название', render: (row) => fmt.esc(row.name) },
        {
          title: 'Куда',
          render: (row) => `<span class="dim" style="font-size:11px">${fmt.esc(row.webhook_url)}</span>`,
        },
        {
          title: 'События',
          render: (row) =>
            (row.events || '').split(',').filter(Boolean)
              .map((code) => `<span class="badge">${EVENT_TITLES[code] || code}</span>`).join(' '),
        },
        {
          title: '',
          className: 'num',
          render: (row) => `<button class="btn btn--ghost" data-drop-rule="${row.id}" title="Удалить">×</button>`,
        },
      ], rules, { emptyMessage: 'Правил нет — уведомления никуда не уходят' });
      bindRemoval('#rules-table', 'dropRule', api.deleteRule, renderRules, 'Правило удалено');
    } catch (error) {
      denied(container, error);
    }
  }

  const EVENT_TITLES = {
    limit_breach: 'нарушение лимита',
    offer_soon: 'скоро оферта',
    cash_gap: 'кассовый разрыв',
    price_move: 'движение цены',
    volume_anomaly: 'аномалия объёма',
  };

  async function renderAudit() {
    const container = $('#audit-table');
    try {
      const records = await api.audit({ limit: 200 });
      renderTable(container, [
        { title: 'Когда', render: (row) => fmt.dateTime(row.created_at) },
        { title: 'Кто', render: (row) => fmt.esc(row.user_login || '—') },
        { title: 'Действие', render: (row) => `<span class="badge">${fmt.esc(row.action)}</span>` },
        { title: 'Объект', render: (row) => `<span class="dim">${fmt.esc(row.entity)}${row.entity_id ? ' #' + fmt.esc(row.entity_id) : ''}</span>` },
        { title: 'Подробности', render: (row) => fmt.esc(row.detail || '—') },
      ], records, { emptyMessage: 'Журнал пуст' });
    } catch (error) {
      denied(container, error);
    }
  }

  async function submitUser(event) {
    event.preventDefault();
    const message = $('#user-msg');
    try {
      await api.createUser({
        login: $('#u-login').value.trim(),
        password: $('#u-password').value,
        full_name: $('#u-name').value.trim() || null,
        role: $('#u-role').value,
      });
      message.textContent = 'Пользователь заведён';
      message.className = 'form-msg form-msg--ok';
      $('#user-form').reset();
      renderUsers();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  async function submitPassword(event) {
    event.preventDefault();
    const message = $('#password-msg');
    const login = $('#p-login').value.trim();
    try {
      const result = await api.changePassword({
        password: $('#p-password').value,
        login: login || null,
      });
      message.textContent = `${result.detail}: ${result.login}`;
      message.className = 'form-msg form-msg--ok';
      $('#password-form').reset();
      // Смена пароля закрывает все входы, включая текущий — предупреждаем
      if (!login || login.toLowerCase() === (state.user && state.user.login)) {
        toast('Пароль изменён. Войдите заново с новым паролем.');
      }
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  async function submitRule(event) {
    event.preventDefault();
    const message = $('#rule-msg');
    const events = $$('#r-events input:checked').map((box) => box.value);
    if (!events.length) {
      message.textContent = 'Выберите хотя бы одно событие';
      message.className = 'form-msg form-msg--err';
      return;
    }
    try {
      await api.createRule({
        name: $('#r-name').value.trim(),
        webhook_url: $('#r-url').value.trim(),
        events,
      });
      message.textContent = 'Правило сохранено';
      message.className = 'form-msg form-msg--ok';
      $('#rule-form').reset();
      $('#rule-form').hidden = true;
      renderRules();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  /** Проверка рассылки без отправки: показывает, что ушло бы адресатам. */
  async function testNotifications() {
    try {
      const result = await api.sendNotifications(state.portfolioName, true);
      toast(
        result.events_found
          ? `Событий ${result.events_found}, подошло под правила ${result.sent} (без отправки)`
          : 'Событий нет — рассылать нечего'
      );
    } catch (error) {
      toast(error.message, true);
    }
  }

  // ------------------------------------------------------------------
  // Вход
  // ------------------------------------------------------------------
  function showLogin(show) {
    const modal = $('#login-modal');
    if (modal) modal.hidden = !show;
    if (show) setTimeout(() => { const node = $('#li-login'); if (node) node.focus(); }, 60);
  }

  async function submitLogin(event) {
    event.preventDefault();
    const message = $('#login-msg');
    try {
      const result = await api.login($('#li-login').value.trim(), $('#li-password').value);
      api.token(result.token);
      state.user = result;
      showLogin(false);
      $('#li-password').value = '';
      message.textContent = '';
      updateWhoami();
      // Данные грузились без токена и не пришли — начинаем заново
      state.loaded = {};
      await loadPortfolioNames();
      RENDERERS[state.view]();
    } catch (error) {
      message.textContent = error.message;
      message.className = 'form-msg form-msg--err';
    }
  }

  async function doLogout() {
    try {
      await api.logout();
    } catch (error) { /* сессия могла истечь — всё равно выходим */ }
    api.token(null);
    state.user = null;
    updateWhoami();
    showLogin(true);
  }

  function updateWhoami() {
    const badge = $('#whoami');
    const logout = $('#btn-logout');
    const enabled = state.authEnabled && state.user;
    if (badge) {
      badge.hidden = !enabled;
      if (enabled) {
        badge.textContent = `${state.user.full_name || state.user.login} · ${ROLE_TITLES[state.user.role] || state.user.role}`;
      }
    }
    if (logout) logout.hidden = !enabled;
  }

  /** Узнать режим доступа и, если нужно, попросить войти. */
  async function initAuth() {
    let mode;
    try {
      mode = await api.authMode();
    } catch (error) {
      // Терминал без входа — обычный режим работы на своей машине
      state.authEnabled = false;
      return true;
    }
    state.authEnabled = mode.auth_enabled;
    if (!mode.auth_enabled) return true;

    if (!api.token()) {
      showLogin(true);
      return false;
    }
    try {
      state.user = await api.me();
      updateWhoami();
      return true;
    } catch (error) {
      api.token(null);
      showLogin(true);
      return false;
    }
  }

  // ------------------------------------------------------------------
  // Выбор портфеля и автообновление
  // ------------------------------------------------------------------
  async function loadPortfolioNames() {
    const select = $('#portfolio-select');
    if (!select) return;
    try {
      const names = await api.portfolioNames();
      state.portfolios = names;
      select.innerHTML =
        '<option value="">Все сразу</option>' +
        names.map((name) => `<option value="${fmt.esc(name)}">${fmt.esc(name)}</option>`).join('');
      // Выбранный портфель мог исчезнуть вместе с последней сделкой
      select.value = names.includes(state.portfolioName) ? state.portfolioName : '';
      state.portfolioName = select.value || null;
    } catch (error) {
      console.warn('Список портфелей не загружен:', error.message);
    }
  }

  function changePortfolio(name) {
    state.portfolioName = name || null;
    // Портфель влияет на деньги, историю, сигналы и лимиты — сбрасываем всё,
    // текущую вкладку перерисовываем сразу
    state.loaded = {};
    state.loaded[state.view] = true;
    const field = $('#d-portfolio');
    if (field && name) field.value = name;
    RENDERERS[state.view]();
  }

  const AUTO_REFRESH_MS = 60000;

  function toggleAutoRefresh() {
    const button = $('#btn-auto');
    if (state.autoTimer) {
      clearInterval(state.autoTimer);
      state.autoTimer = null;
      if (button) {
        button.classList.remove('is-active');
        button.title = 'Автообновление раз в минуту';
      }
      toast('Автообновление выключено');
      return;
    }
    state.autoTimer = setInterval(() => {
      // Перерисовываем только открытую вкладку: остальные обновятся при показе
      if (document.hidden) return;
      state.loaded = {};
      state.loaded[state.view] = true;
      RENDERERS[state.view]();
    }, AUTO_REFRESH_MS);
    if (button) {
      button.classList.add('is-active');
      button.title = 'Выключить автообновление';
    }
    toast('Автообновление включено: раз в минуту');
  }

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
          {
            label: 'НКД на расчёты',
            value: fmt.num(info.accrued_interest, 2),
            hint: data.accrual && data.accrual.settle_date
              ? fmt.date(data.accrual.settle_date)
              : 'дата расчётов',
          },
          {
            label: 'НКД на сегодня',
            value: accrualValue(data.accrual && data.accrual.today),
            hint: accrualHint(data.accrual && data.accrual.today),
          }
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
              ${stat.hint ? `<div class="stat__hint">${fmt.esc(stat.hint)}</div>` : ''}
            </div>`).join('')}
        </div>

        <div>
          <div class="section-title">
            Ход торгов
            <span class="segmented segmented--sm" id="intraday-interval">
              <button data-minutes="1" type="button">1 мин</button>
              <button data-minutes="10" class="is-active" type="button">10 мин</button>
              <button data-minutes="60" type="button">1 час</button>
            </span>
          </div>
          <div id="intraday-stats"></div>
          <div id="intraday-chart"></div>
          <div class="section-subtitle">Лента сделок</div>
          <div id="intraday-tape" class="table-wrap" style="max-height:260px"></div>
        </div>

        ${info.kind === 'bond' ? `
        <div>
          <div class="section-title">Накопленный купонный доход</div>
          <div id="accrual-block"></div>
        </div>` : ''}

        <div>
          <div class="section-title">Цена и объём торгов</div>
          <div id="drawer-chart"></div>
        </div>
        ${info.kind === 'bond' ? '<div><div class="section-title">Премия к рынку гособлигаций</div><div id="drawer-spread"></div></div>' : ''}
        ${data.cashflows.length ? '<div><div class="section-title">График выплат (данные НРД)</div><div id="drawer-cashflows" class="table-wrap"></div></div>' : ''}`;

      state.drawerSecid = info.secid;
      renderIntraday(info.secid, state.intradayInterval);
      $$('#intraday-interval button').forEach((button) => {
        button.addEventListener('click', () => {
          state.intradayInterval = Number(button.dataset.minutes);
          $$('#intraday-interval button').forEach((other) =>
            other.classList.toggle('is-active', other === button)
          );
          renderIntraday(info.secid, state.intradayInterval);
        });
      });

      if (info.kind === 'bond') renderAccrual(info.secid, data.accrual);

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

  // ------------------------------------------------------------------
  // Ход торгов
  // ------------------------------------------------------------------
  /** Ход торгов: свечи текущей сессии, итоги и лента сделок. */
  async function renderIntraday(secid, interval) {
    const chart = $('#intraday-chart');
    const statsBox = $('#intraday-stats');
    const tape = $('#intraday-tape');
    if (!chart) return;
    loading(chart);

    let data;
    try {
      data = await api.intraday(secid, { interval, trades: 40 });
    } catch (error) {
      // Карточка могла закрыться, пока шёл запрос
      if ($('#intraday-chart') !== chart) return;
      return failure(chart, error);
    }
    // Пока грузились, пользователь мог открыть другую бумагу
    if (state.drawerSecid !== secid) return;

    const s = data.session;
    const flow = data.flow || {};
    statsBox.innerHTML = `
      <div class="stat-grid stat-grid--tight">
        ${[
          { label: 'Открытие', value: fmt.price(s.open) },
          { label: 'Последняя', value: `<b>${fmt.price(s.last)}</b>` },
          {
            label: 'От открытия',
            value: `<span class="${fmt.trendClass(s.change_pct)}">${fmt.signedPct(s.change_pct)}</span>`,
          },
          { label: 'Максимум', value: fmt.price(s.high) },
          { label: 'Минимум', value: fmt.price(s.low) },
          { label: 'СВЦ сессии', value: fmt.price(s.wa_price) },
          { label: 'Объём, шт', value: fmt.int(s.volume) },
          { label: 'Оборот, ₽', value: fmt.money(s.turnover) },
          {
            label: 'Инициатива покупки',
            value: fmt.isNum(flow.buy_share_pct) ? fmt.pct(flow.buy_share_pct, 1) : '—',
            // Доля по деньгам и счёт сделок расходятся, когда объём прошёл
            // одной заявкой, — показываем оба числа
            hint: flow.trades
              ? `${flow.buy_trades} покупок / ${flow.sell_trades} продаж из ${flow.trades}`
              : '',
          },
        ].map((stat) => `
          <div class="stat">
            <div class="stat__label">${stat.label}</div>
            <div class="stat__value">${stat.value}</div>
            ${stat.hint ? `<div class="stat__hint">${fmt.esc(stat.hint)}</div>` : ''}
          </div>`).join('')}
      </div>
      ${data.warning ? `<p class="card__note warn">${fmt.esc(data.warning)}</p>` : ''}
      <p class="card__note">${fmt.esc(data.note)}${
        data.snapshot && data.snapshot.ts
          ? ` Собранный срез — на ${fmt.dateTime(data.snapshot.ts)}.`
          : ''
      }</p>`;

    // Свечи рисуем как цену с объёмом: тот же вид, что у дневной истории,
    // только шкала времени внутридневная
    charts.priceVolumeChart(
      chart,
      data.candles.map((candle) => ({
        trade_date: candle.begin,
        close: candle.close,
        volume: candle.volume,
      })),
      {
        height: 240,
        priceFormat: (value) => fmt.price(value),
        xFormat: (value) => fmt.time(value),
        xFormatFull: (value) => fmt.dateTime(value),
        emptyMessage: 'Сегодня по этой бумаге сделок не было',
      }
    );

    renderTable(tape, [
      { title: 'Время', render: (row) => `<span class="dim">${fmt.esc(row.trade_time)}</span>` },
      { title: 'Цена', className: 'num', render: (row) => fmt.price(row.price) },
      { title: 'Кол-во', className: 'num', render: (row) => fmt.int(row.quantity) },
      { title: 'Сумма, ₽', className: 'num', render: (row) => fmt.money(row.value) },
      {
        title: 'Доходность',
        className: 'num',
        render: (row) => (fmt.isNum(row.yield_pct) ? fmt.pct(row.yield_pct) : '<span class="dim">—</span>'),
      },
      {
        title: 'Инициатор',
        render: (row) =>
          row.side === 'B'
            ? '<span class="badge badge--up">покупатель</span>'
            : row.side === 'S'
              ? '<span class="badge badge--down">продавец</span>'
              : '<span class="dim">—</span>',
      },
    ], data.trades, { emptyMessage: 'Лента пуста' });
  }

  /** Значение НКД с валютой — расчёт может быть в валюте номинала. */
  function accrualValue(row) {
    if (!row) return '<span class="dim">—</span>';
    const base = fmt.num(row.value, row.value > 100 ? 2 : 4);
    const shown = row.currency && row.currency !== 'RUB'
      ? `${base} <span class="dim">${fmt.esc(row.currency)}</span>`
      : base;
    // Знак приближения честнее, чем ровное число там, где его нет
    return row.estimate ? `<span class="approx">≈</span>${shown}` : shown;
  }

  function accrualHint(row) {
    if (!row) return '';
    return `${row.days_passed} из ${row.days_total} дней купона`;
  }

  /** Разбор НКД: на сегодня, на дату расчётов и на любую выбранную дату. */
  function renderAccrual(secid, profile) {
    const container = $('#accrual-block');
    if (!container) return;

    if (!profile || (!profile.today && !profile.settlement)) {
      container.innerHTML =
        '<div class="empty">Не хватает данных о купонах: у выпуска нет ни графика, ни величины купона в справочнике</div>';
      return;
    }

    const row = profile.today || profile.settlement;
    const floating = row.floating;
    container.innerHTML = `
      ${floating ? `
      <div class="notice notice--warn">
        <div class="notice__title">Плавающий купон — НКД на прочие даты приблизителен</div>
        <div class="notice__body">
          Ставка этого выпуска меняется внутри купонного периода, поэтому НКД
          растёт неравномерно. Биржа публикует точное значение только на дату
          расчётов${profile.settle_date ? ` (${fmt.date(profile.settle_date)})` : ''} —
          <b>${fmt.num(profile.exchange_value, 2)}</b>. Остальные даты
          рассчитаны от него по доле периода и помечены знаком «≈».
          ${fmt.isNum(row.coupon_value) ? `Купон периода по факту начисления — около ${fmt.num(row.coupon_value, 2)}.` : ''}
        </div>
      </div>` : ''}

      <div class="stat-grid stat-grid--tight">
        <div class="stat">
          <div class="stat__label">На сегодня</div>
          <div class="stat__value">${accrualValue(profile.today)}</div>
          <div class="stat__hint">${fmt.esc(accrualHint(profile.today))}</div>
        </div>
        <div class="stat">
          <div class="stat__label">На дату расчётов</div>
          <div class="stat__value">${accrualValue(profile.settlement)}</div>
          <div class="stat__hint">${profile.settle_date ? fmt.date(profile.settle_date) : 'дата неизвестна'}</div>
        </div>
        <div class="stat">
          <div class="stat__label">НКД биржи</div>
          <div class="stat__value">${fmt.num(profile.exchange_value, 2)}</div>
          <div class="stat__hint">в рублях расчётов</div>
        </div>
        <div class="stat">
          <div class="stat__label">Купонный период</div>
          <div class="stat__value">${fmt.date(row.period_start)} — ${fmt.date(row.period_end)}</div>
          <div class="stat__hint">${row.days_left} ${fmt.plural(row.days_left, 'день', 'дня', 'дней')} до купона</div>
        </div>
      </div>

      <div class="toolbar" style="border-bottom:none;padding-left:0">
        <label class="field">
          <span>НКД на дату</span>
          <input type="date" id="accrual-date">
        </label>
        <div id="accrual-picked" class="accrual-picked"></div>
      </div>

      ${profile.mismatch ? `<p class="card__note warn">${fmt.esc(profile.mismatch.note)}</p>` : ''}
      <p class="card__note">${fmt.esc(profile.exchange_note)} Расчёт по источнику: ${fmt.esc(row.source)}.</p>`;

    const input = $('#accrual-date');
    if (input) {
      input.value = new Date().toISOString().slice(0, 10);
      input.addEventListener('change', async () => {
        const output = $('#accrual-picked');
        if (!input.value) return;
        output.textContent = 'Считаю…';
        try {
          const result = await api.accrued(secid, input.value);
          const picked = result.selected || result.today;
          output.innerHTML = picked
            ? `<b>${accrualValue(picked)}</b> <span class="dim">· ${fmt.esc(accrualHint(picked))} · период ${fmt.date(picked.period_start)} — ${fmt.date(picked.period_end)}</span>`
            : '<span class="dim">На эту дату график купонов не распространяется</span>';
        } catch (error) {
          output.innerHTML = `<span class="down">${fmt.esc(error.message)}</span>`;
        }
      });
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


  // ------------------------------------------------------------------
  // Заседания Банка России по ключевой ставке
  // ------------------------------------------------------------------
  //
  // Календарь открывается кликом по плитке ставки в обзоре рынка:
  // когда следующее решение, будет ли к нему прогноз, и чем закончились
  // предыдущие заседания.

  function meetingCard(meeting, highlight) {
    const badges = [];
    if (meeting.kind === 'extraordinary') {
      badges.push('<span class="badge badge--warn">внеочередное</span>');
    }
    if (meeting.with_forecast) {
      badges.push('<span class="badge badge--accent" title="Публикуется среднесрочный прогноз Банка России">опорное</span>');
    }

    let verdict = '';
    if (meeting.past && fmt.isNum(meeting.rate)) {
      const change = meeting.rate_change;
      if (!fmt.isNum(change) || change === 0) {
        verdict = `<span class="dim">ставка сохранена — ${fmt.pct(meeting.rate, 2)}</span>`;
      } else {
        const cls = change < 0 ? 'up' : 'down';
        const word = change < 0 ? 'снижена' : 'повышена';
        verdict = `<span class="${cls}">${word} на ${fmt.num(Math.abs(change), 2)} п.п. → ${fmt.pct(meeting.rate, 2)}</span>`;
      }
    } else if (!meeting.past) {
      const days = meeting.days;
      verdict = `<span class="dim">${days === 0 ? 'сегодня' : `через ${days} ${fmt.plural(days, 'день', 'дня', 'дней')}`}</span>`;
    }

    const links = (meeting.links || [])
      .map((link) => `<a href="${fmt.esc(link.url)}" target="_blank" rel="noopener">${fmt.esc(link.title)}</a>`)
      .join(' · ');

    return `
      <div class="meeting${highlight ? ' meeting--next' : ''}">
        <div class="meeting__date">
          ${fmt.date(meeting.date)}
          ${highlight ? '<span class="badge badge--up">ближайшее</span>' : ''}
          ${badges.join(' ')}
        </div>
        <div class="meeting__verdict">${verdict}</div>
        ${links ? `<div class="meeting__links">${links}</div>` : ''}
      </div>`;
  }

  async function openRateCalendar() {
    const drawer = $('#drawer');
    $('#drawer-title').textContent = 'Ключевая ставка Банка России';
    $('#drawer-sub').textContent = 'Календарь заседаний Совета директоров';
    const body = $('#drawer-body');
    loading(body);
    drawer.hidden = false;

    let data;
    try {
      data = await api.rateCalendar();
    } catch (error) {
      return failure(body, error);
    }

    if (!data.upcoming.length && !data.past.length) {
      return charts.empty(body, 'Календарь ещё не загружен — нажмите «Собрать данные»');
    }

    const next = data.next;
    const head = `
      <div class="stat-grid">
        <div class="stat">
          <div class="stat__label">Ставка сейчас</div>
          <div class="stat__value">${fmt.pct(data.current_rate, 2)}</div>
          <div class="stat__label" style="text-transform:none">на ${fmt.date(data.current_rate_date)}</div>
        </div>
        <div class="stat">
          <div class="stat__label">Следующее заседание</div>
          <div class="stat__value">${next ? fmt.date(next.date) : '—'}</div>
          <div class="stat__label" style="text-transform:none">${
            next
              ? (next.days === 0 ? 'сегодня' : `через ${next.days} ${fmt.plural(next.days, 'день', 'дня', 'дней')}`)
              : 'календарь не опубликован'
          }</div>
        </div>
        <div class="stat">
          <div class="stat__label">Со среднесрочным прогнозом</div>
          <div class="stat__value">${
            (data.upcoming.find((m) => m.with_forecast) || {}).date
              ? fmt.date(data.upcoming.find((m) => m.with_forecast).date)
              : '—'
          }</div>
          <div class="stat__label" style="text-transform:none">опорное заседание</div>
        </div>
      </div>`;

    const upcoming = data.upcoming
      .map((meeting, index) => meetingCard(meeting, index === 0))
      .join('');
    const past = data.past.map((meeting) => meetingCard(meeting, false)).join('');

    body.innerHTML = `
      ${head}
      <h3 class="section-title" style="margin-top:18px">Предстоящие заседания</h3>
      ${upcoming || '<div class="empty">Календарь на следующий период ещё не опубликован</div>'}
      <h3 class="section-title" style="margin-top:18px">Прошедшие решения</h3>
      ${past || '<div class="empty">Нет данных</div>'}
      <p class="card__note" style="border:none;padding:12px 0 0">
        Источник: ${fmt.esc(data.source)}. Опорное заседание — то, к которому
        Банк России публикует среднесрочный прогноз: на нём чаще пересматривают
        траекторию ставки.
      </p>`;
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
    cash: renderCash,
    imports: renderImports,
    signals: renderSignals,
    admin: renderAdmin,
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
    const inMonth = new Date(today.getTime() + 30 * 86400000).toISOString().slice(0, 10);
    [['#d-date', isoToday], ['#e-from', monthAgo], ['#e-to', isoToday],
     ['#fl-date', isoToday], ['#pl-start', isoToday], ['#pl-end', inMonth]].forEach(
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
    on('#btn-auto', 'click', toggleAutoRefresh);
    on('#deal-form', 'submit', submitDeal);

    // Выбор портфеля действует на все вкладки сразу
    on('#portfolio-select', 'change', (event) => changePortfolio(event.target.value));

    // Вход и выход
    on('#login-form', 'submit', submitLogin);
    on('#btn-logout', 'click', doLogout);

    // История стоимости и отчёт
    $$('#history-range button').forEach((button) => {
      button.addEventListener('click', () => {
        state.historyDays = Number(button.dataset.days);
        $$('#history-range button').forEach((other) =>
          other.classList.toggle('is-active', other === button)
        );
        renderHistory();
      });
    });
    on('#snapshot-now', 'click', async () => {
      try {
        await api.takeSnapshot(state.portfolioName);
        toast('Стоимость зафиксирована');
        renderHistory();
      } catch (error) {
        toast(error.message, true);
      }
    });
    on('#report-download', 'click', async () => {
      const button = $('#report-download');
      button.disabled = true;
      try {
        const filename = await api.download('/api/report', {
          params: { name: state.portfolioName },
        });
        toast(`Отчёт сохранён: ${filename}`);
      } catch (error) {
        toast(error.message, true);
      } finally {
        button.disabled = false;
      }
    });

    // Деньги
    $$('#cash-horizon button').forEach((button) => {
      button.addEventListener('click', () => {
        state.cashHorizon = Number(button.dataset.days);
        $$('#cash-horizon button').forEach((other) =>
          other.classList.toggle('is-active', other === button)
        );
        renderCash();
      });
    });
    on('#account-add', 'click', () => {
      const form = $('#account-form');
      form.hidden = !form.hidden;
    });
    on('#account-cancel', 'click', () => { $('#account-form').hidden = true; });
    on('#account-form', 'submit', submitAccount);
    on('#flow-form', 'submit', submitFlow);
    on('#placement-add', 'click', () => {
      const form = $('#placement-form');
      form.hidden = !form.hidden;
    });
    on('#placement-cancel', 'click', () => { $('#placement-form').hidden = true; });
    on('#placement-form', 'submit', submitPlacement);

    // Импорт и сверка
    wireDropzone('#import-drop', '#import-file', '#import-pick', previewImportFile);
    wireDropzone('#recon-drop', '#recon-file', '#recon-pick', runReconcile);
    on('#import-apply', 'click', applyImport);

    // Настройки
    on('#user-form', 'submit', submitUser);
    on('#password-form', 'submit', submitPassword);
    on('#rule-add', 'click', () => {
      const form = $('#rule-form');
      form.hidden = !form.hidden;
    });
    on('#rule-cancel', 'click', () => { $('#rule-form').hidden = true; });
    on('#rule-form', 'submit', submitRule);
    on('#notify-test', 'click', testNotifications);

    // Анализ облигаций
    const onAnalysisFilter = debounce(renderAnalysis);
    ['#a-search', '#a-minyield', '#a-maxyield', '#a-mindur', '#a-maxdur',
     '#a-matfrom', '#a-matto', '#a-turnover', '#a-risk'].forEach((selector) =>
      on(selector, 'input', onAnalysisFilter)
    );
    ['#a-coupon', '#a-benchmark', '#a-level', '#a-currency', '#a-offer', '#a-amort'].forEach(
      (selector) => on(selector, 'change', renderAnalysis)
    );
    // Список сортировки и стрелки в шапке таблицы — одно и то же состояние
    on('#a-sort', 'change', (event) => {
      state.analysisSort = { by: event.target.value, order: state.analysisSort.order };
      renderAnalysis();
    });
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

    // Форма кривой в сценариях переоценки
    $$('#curve-tilt button').forEach((button) => {
      button.addEventListener('click', () => {
        state.curveTilt = button.dataset.tilt;
        $$('#curve-tilt button').forEach((other) =>
          other.classList.toggle('is-active', other === button)
        );
        renderPortfolio();
      });
    });

    // Добавление в портфель из витрин
    on('#instruments-buy', 'click', () => openBuyModal('instruments', pickedList('instruments')));
    on('#analysis-buy', 'click', () => openBuyModal('analysis', pickedList('analysis')));
    on('#buy-submit', 'click', submitBuy);
    on('#buy-verify', 'click', verifyBuyAgainstLimits);
    $$('[data-buy-close]').forEach((node) => node.addEventListener('click', closeBuyModal));

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
      if (event.key !== 'Escape') return;
      // Форма входа закрытию не подлежит: без неё терминал бесполезен
      if (!$('#login-modal').hidden) return;
      // Окно добавления перекрывает карточку, поэтому закрываем его первым
      if (!$('#buy-modal').hidden) closeBuyModal();
      else closeDrawer();
    });

    // Истёкшая сессия: клиент API просит показать вход заново
    window.onAuthRequired = () => {
      state.user = null;
      updateWhoami();
      showLogin(true);
    };

    switchView('overview');

    // Проверка доступа и список портфелей — после первой отрисовки, чтобы
    // интерфейс появлялся сразу, а не ждал ответа сервера
    initAuth().then((allowed) => {
      if (allowed) loadPortfolioNames();
    });
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
