/* Казначейский терминал — логика интерфейса. */
(function () {
  'use strict';

  const state = {
    view: 'overview',
    loaded: {},
    portfolioName: null,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

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
        color: '#2f81f7',
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
          { name: 'Ключевая ставка', color: '#2f81f7', points: toPoints(keyRate) },
          { name: 'RUONIA', color: '#d29922', points: toPoints(ruonia) },
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
  // Облигации
  // ------------------------------------------------------------------
  async function renderBonds() {
    const container = $('#bonds-table');
    loading(container);

    const params = {
      kind: ['bond'],
      search: $('#b-search').value.trim(),
      min_yield: parseFloat($('#b-minyield').value) || null,
      max_yield: parseFloat($('#b-maxyield').value) || null,
      max_duration_years: parseFloat($('#b-maxdur').value) || null,
      min_turnover: (parseFloat($('#b-turnover').value) || 0) * 1e6,
      sort_by: $('#b-sort').value,
      limit: 200,
    };

    try {
      const data = await api.instruments(params);
      $('#bonds-count').textContent =
        `${fmt.int(data.total)} ${fmt.plural(data.total, 'выпуск', 'выпуска', 'выпусков')}` +
        (data.curve_date ? ` · КБД на ${fmt.date(data.curve_date)}` : '');

      renderTable(container, [
        { title: 'Выпуск', render: (row) => secCell(row) },
        { title: 'Погашение', render: (row) => `<span class="dim">${fmt.date(row.maturity_date)}</span>` },
        { title: 'Цена, %', className: 'num', render: (row) => fmt.price(row.last) },
        { title: 'Доходность', className: 'num', render: (row) => `<b>${fmt.pct(row.yield_pct)}</b>` },
        { title: 'КБД', className: 'num', render: (row) => fmt.pct(row.curve_yield_pct) },
        {
          title: 'Премия',
          className: 'num',
          render: (row) => {
            const value = row.spread_to_curve_bp;
            if (!fmt.isNum(value)) return '<span class="dim">—</span>';
            // Премия свыше 1000 бп почти всегда означает не доходность,
            // а проблемы у эмитента: помечаем отдельно, чтобы её не покупали
            // «по верхней строчке сортировки».
            let cls = 'badge--up';
            let hint = 'Премия к безрисковой кривой';
            if (value > 1000) {
              cls = 'badge--down';
              hint = 'Аномальная премия: вероятны проблемы у эмитента — проверьте кредитное качество';
            } else if (value > 300) {
              cls = 'badge--warn';
              hint = 'Повышенная премия: требуется оценка кредитного риска';
            } else if (value < 0) {
              cls = '';
              hint = 'Доходность ниже кривой';
            }
            const mark = value > 1000 ? ' ⚠' : '';
            return `<span class="badge ${cls}" title="${hint}">${fmt.bp(value)}${mark}</span>`;
          },
        },
        { title: 'Дюрация', className: 'num', render: (row) => (fmt.isNum(row.duration_years) ? fmt.num(row.duration_years, 2) + ' л' : '—') },
        { title: 'Купон', className: 'num', render: (row) => fmt.pct(row.coupon_percent) },
        { title: 'Оборот, ₽', className: 'num', render: (row) => fmt.money(row.turnover) },
        { title: 'Ур.', className: 'num', render: (row) => `<span class="badge">${row.list_level || '—'}</span>` },
      ], data.items, {
        rowKey: (row) => row.secid,
        onRowClick: openInstrument,
        emptyMessage: 'Нет выпусков под заданные условия',
      });
    } catch (error) {
      failure(container, error);
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

      const cards = [
        { label: 'Стоимость портфеля', value: fmt.rub(summary.total_value), meta: `${summary.positions_open} ${fmt.plural(summary.positions_open, 'позиция', 'позиции', 'позиций')}` },
        {
          label: 'Нереализованный P&L',
          value: `<span class="${fmt.trendClass(summary.unrealized_pnl)}">${fmt.rub(summary.unrealized_pnl)}</span>`,
          meta: fmt.signedPct(summary.unrealized_pnl_pct),
        },
        {
          label: 'Реализованный P&L',
          value: `<span class="${fmt.trendClass(summary.realized_pnl)}">${fmt.rub(summary.realized_pnl)}</span>`,
          meta: `комиссии ${fmt.rub(summary.fees)}`,
        },
        {
          label: 'Итого результат',
          value: `<span class="${fmt.trendClass(summary.net_pnl)}">${fmt.rub(summary.net_pnl)}</span>`,
          meta: 'с учётом комиссий',
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
        summary.allocation.map((item) => ({
          label: KIND_TITLES[item.kind] || item.kind,
          value: item.value,
          share: item.share_pct,
        })),
        { valueFormat: (v) => fmt.money(v) + ' ₽', emptyMessage: 'Нет открытых позиций' }
      );

      charts.barsHorizontal(
        $('#sensitivity-chart'),
        sensitivity.scenarios.map((item) => ({
          label: `${item.shift_bp > 0 ? '+' : ''}${item.shift_bp} бп`,
          value: item.impact_rub,
          share: item.impact_pct,
        })),
        {
          valueFormat: (v) => fmt.money(v) + ' ₽',
          colorBySign: true,
          emptyMessage: 'Нет облигаций в портфеле',
        }
      );

      renderTable($('#positions-table'), [
        { title: 'Бумага', render: (row) => secCell(row) },
        { title: 'Кол-во', className: 'num', render: (row) => fmt.int(row.quantity) },
        { title: 'Средняя', className: 'num', render: (row) => fmt.price(row.avg_price) },
        { title: 'Текущая', className: 'num', render: (row) => fmt.price(row.last_price) },
        { title: 'Оценка, ₽', className: 'num', render: (row) => fmt.money(row.market_value) },
        {
          title: 'P&L, ₽',
          className: 'num',
          render: (row) => `<span class="${fmt.trendClass(row.unrealized_pnl)}">${fmt.money(row.unrealized_pnl)}</span>`,
        },
        {
          title: 'P&L, %',
          className: 'num',
          render: (row) => `<span class="${fmt.trendClass(row.unrealized_pnl_pct)}">${fmt.signedPct(row.unrealized_pnl_pct)}</span>`,
        },
        { title: 'Доля', className: 'num', render: (row) => fmt.pct(row.weight_pct, 1) },
        { title: 'Дюрация', className: 'num', render: (row) => (fmt.isNum(row.duration_years) ? fmt.num(row.duration_years, 2) + ' л' : '—') },
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
    } catch (error) {
      failure(kpi, error);
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
        ${data.cashflows.length ? '<div><div class="section-title">График выплат (данные НРД)</div><div id="drawer-cashflows" class="table-wrap"></div></div>' : ''}`;

      charts.priceVolumeChart($('#drawer-chart'), data.history, {
        height: 260,
        priceFormat: (value) => fmt.price(value),
      });

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

  function closeDrawer() {
    $('#drawer').hidden = true;
  }

  // ------------------------------------------------------------------
  // Навигация и события
  // ------------------------------------------------------------------
  const RENDERERS = {
    overview: renderOverview,
    instruments: renderInstruments,
    bonds: renderBonds,
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
    $('#d-date').value = new Date().toISOString().slice(0, 10);

    $$('.tab').forEach((tab) => {
      tab.addEventListener('click', () => switchView(tab.dataset.view));
    });

    $('#btn-refresh').addEventListener('click', refresh);
    $('#btn-collect').addEventListener('click', collect);
    $('#deal-form').addEventListener('submit', submitDeal);

    const onInstrumentFilter = debounce(renderInstruments);
    ['#f-search', '#f-turnover', '#f-liquidity'].forEach((selector) =>
      $(selector).addEventListener('input', onInstrumentFilter)
    );
    ['#f-kind', '#f-sort'].forEach((selector) =>
      $(selector).addEventListener('change', renderInstruments)
    );

    const onBondFilter = debounce(renderBonds);
    ['#b-search', '#b-minyield', '#b-maxyield', '#b-maxdur', '#b-turnover'].forEach((selector) =>
      $(selector).addEventListener('input', onBondFilter)
    );
    $('#b-sort').addEventListener('change', renderBonds);

    $$('[data-close]').forEach((node) => node.addEventListener('click', closeDrawer));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeDrawer();
    });

    switchView('overview');
  }

  document.addEventListener('DOMContentLoaded', init);
})();
