/* Лёгкие SVG-графики без внешних библиотек. */
(function (global) {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';
  const VIEW_W = 800;

  function el(name, attrs, text) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value !== null && value !== undefined) node.setAttribute(key, value);
    });
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function empty(container, message) {
    container.innerHTML = `<div class="empty">${message || 'Нет данных'}</div>`;
  }

  /** Подобрать «круглые» границы оси, чтобы подписи читались. */
  function niceBounds(min, max) {
    if (min === max) {
      const pad = Math.abs(min) * 0.1 || 1;
      return { min: min - pad, max: max + pad };
    }
    const span = max - min;
    const pad = span * 0.08;
    return { min: min - pad, max: max + pad };
  }

  function createSvg(container, height) {
    container.innerHTML = '';
    const svg = el('svg', {
      class: 'chart',
      viewBox: `0 0 ${VIEW_W} ${height}`,
      preserveAspectRatio: 'none',
      height,
    });
    container.appendChild(svg);
    return svg;
  }

  function drawGrid(svg, geom, yMin, yMax, yFormat, ticks = 4) {
    for (let i = 0; i <= ticks; i += 1) {
      const ratio = i / ticks;
      const y = geom.top + geom.h * ratio;
      const value = yMax - (yMax - yMin) * ratio;
      svg.appendChild(
        el('line', {
          class: 'chart__grid',
          x1: geom.left, x2: geom.left + geom.w, y1: y, y2: y,
        })
      );
      svg.appendChild(
        el('text', {
          class: 'chart__axis',
          x: geom.left - 7,
          y: y + 3.5,
          'text-anchor': 'end',
        }, yFormat(value))
      );
    }
  }

  /**
   * Линейный график одной или нескольких серий.
   * series: [{ name, color, points: [{x, y, label}] }]
   */
  function lineChart(container, series, options = {}) {
    const opts = Object.assign(
      {
        height: 220,
        yFormat: (v) => fmt.num(v, 1),
        xFormat: (v) => String(v),
        area: true,
        dots: false,
        xTicks: 6,
        minLabelGap: 58,
      },
      options
    );

    const active = (series || []).filter((s) => s.points && s.points.length);
    if (!active.length) return empty(container, opts.emptyMessage);

    const svg = createSvg(container, opts.height);
    const geom = { left: 52, top: 12, w: VIEW_W - 52 - 14, h: opts.height - 12 - 26 };

    const allX = active.flatMap((s) => s.points.map((p) => p.x));
    const allY = active.flatMap((s) => s.points.map((p) => p.y));
    const xMin = Math.min(...allX);
    const xMax = Math.max(...allX);
    const bounds = niceBounds(Math.min(...allY), Math.max(...allY));

    const scaleX = (x) => (xMax === xMin ? geom.left : geom.left + ((x - xMin) / (xMax - xMin)) * geom.w);
    const scaleY = (y) => geom.top + geom.h - ((y - bounds.min) / (bounds.max - bounds.min)) * geom.h;

    drawGrid(svg, geom, bounds.min, bounds.max, opts.yFormat);

    // Градиент для заливки под линией — только для одиночной серии
    if (opts.area && active.length === 1) {
      const defs = el('defs');
      const grad = el('linearGradient', { id: 'area-grad', x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.appendChild(el('stop', { offset: '0%', 'stop-color': active[0].color || 'currentColor', 'stop-opacity': 0.28 }));
      grad.appendChild(el('stop', { offset: '100%', 'stop-color': active[0].color || 'currentColor', 'stop-opacity': 0 }));
      defs.appendChild(grad);
      svg.appendChild(defs);
    }

    active.forEach((serie) => {
      const points = [...serie.points].sort((a, b) => a.x - b.x);
      const path = points
        .map((p, i) => `${i === 0 ? 'M' : 'L'}${scaleX(p.x).toFixed(2)},${scaleY(p.y).toFixed(2)}`)
        .join(' ');

      if (opts.area && active.length === 1) {
        const areaPath =
          `${path} L${scaleX(points[points.length - 1].x).toFixed(2)},${geom.top + geom.h}` +
          ` L${scaleX(points[0].x).toFixed(2)},${geom.top + geom.h} Z`;
        svg.appendChild(el('path', { class: 'chart__area', d: areaPath }));
      }

      // Цвет задаём инлайновым стилем, а не атрибутом: правило .chart__line
      // в таблице стилей перебивает атрибут stroke, и все серии слились бы
      // в один цвет
      svg.appendChild(
        el('path', {
          class: 'chart__line',
          d: path,
          style: serie.color ? `stroke:${serie.color}` : null,
        })
      );

      if (opts.dots || points.length <= 14) {
        points.forEach((p) => {
          const dot = el('circle', {
            class: 'chart__dot',
            cx: scaleX(p.x),
            cy: scaleY(p.y),
            r: 3,
            style: serie.color ? `fill:${serie.color}` : null,
          });
          dot.appendChild(el('title', {}, p.label || `${opts.xFormat(p.x)}: ${opts.yFormat(p.y)}`));
          svg.appendChild(dot);
        });
      }
    });

    // Подписи оси X. Точки бывают расположены неравномерно (кривая доходности
    // сгущается на коротком конце), поэтому пропускаем подписи, которые встали
    // бы вплотную к предыдущей.
    const reference = [...active[0].points].sort((a, b) => a.x - b.x);
    const step = Math.max(1, Math.floor(reference.length / opts.xTicks));
    let lastLabelX = -Infinity;
    reference.forEach((p, i) => {
      if (i % step !== 0 && i !== reference.length - 1) return;
      const x = scaleX(p.x);
      if (x - lastLabelX < opts.minLabelGap) return;
      lastLabelX = x;
      svg.appendChild(
        el('text', {
          class: 'chart__axis',
          x,
          y: opts.height - 8,
          'text-anchor': 'middle',
        }, opts.xFormat(p.x))
      );
    });

    if (active.length > 1) {
      const legend = document.createElement('div');
      legend.className = 'legend';
      active.forEach((serie) => {
        const item = document.createElement('span');
        item.className = 'legend__item';
        item.innerHTML =
          `<span class="legend__swatch" style="background:${serie.color || 'var(--accent)'}"></span>` +
          fmt.esc(serie.name || '');
        legend.appendChild(item);
      });
      container.appendChild(legend);
    }
    return svg;
  }

  // --------------------------------------------------------------------
  // График временных рядов с наведением
  // --------------------------------------------------------------------

  /** Палитра для серий: различима и в тёмной, и в светлой теме. */
  const SERIES_COLORS = [
    '#3f9d6d', '#d9a441', '#6bb6d8', '#c76ca0',
    '#8f8ae0', '#d4744f', '#5aa9a0', '#a8a13c',
  ];

  function seriesColor(index) {
    return SERIES_COLORS[index % SERIES_COLORS.length];
  }

  /** Значение для подсказки: миллиарды рублей не пишем всеми цифрами. */
  function tipValue(value, serie) {
    const digits = serie.digits ?? 2;
    const text = Math.abs(value) >= 1e6 ? fmt.money(value, digits) : fmt.num(value, digits);
    return serie.unit ? `${text} ${serie.unit}` : text;
  }

  /** Подпись даты: внутри года — день и месяц, на длинном периоде — месяц и год. */
  function dateTickLabel(ms, spanDays) {
    const d = new Date(ms);
    // Внутри одной сессии дата у всех точек одна — подписываем время,
    // иначе ось превращается в столбик одинаковых чисел
    if (spanDays <= 2) {
      return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
    }
    if (spanDays > 400) {
      return d.toLocaleDateString('ru-RU', { month: 'short', year: '2-digit' });
    }
    if (spanDays > 60) {
      return d.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
    }
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' });
  }

  /**
   * Линии и столбцы на одном полотне с подсказкой при наведении.
   *
   * series: [{ code, title, kind: 'line'|'bar', color, unit, digits,
   *            points: [{ x: миллисекунды, y: число }] }]
   * Линии идут по левой оси, столбцы — по правой: у объёма и ставки
   * разные размерности, на одной шкале ставка превратилась бы в прямую.
   */
  function timeSeriesChart(container, seriesList, options = {}) {
    const opts = Object.assign({ height: 260, emptyMessage: 'Нет данных' }, options);

    const active = (seriesList || []).filter((s) => s.points && s.points.length);
    if (!active.length) return empty(container, opts.emptyMessage);

    const lines = active.filter((s) => s.kind !== 'bar');
    const bars = active.filter((s) => s.kind === 'bar');

    container.innerHTML = '';
    const frame = document.createElement('div');
    frame.className = 'chart-frame';
    container.appendChild(frame);

    const svg = el('svg', {
      class: 'chart',
      viewBox: `0 0 ${VIEW_W} ${opts.height}`,
      preserveAspectRatio: 'none',
      height: opts.height,
    });
    frame.appendChild(svg);

    const right = bars.length ? 58 : 14;
    const geom = { left: 56, top: 12, w: VIEW_W - 56 - right, h: opts.height - 12 - 26 };

    const allX = active.flatMap((s) => s.points.map((p) => p.x));
    const xMin = Math.min(...allX);
    const xMax = Math.max(...allX);
    const spanDays = (xMax - xMin) / 86400000;
    const scaleX = (x) =>
      xMax === xMin ? geom.left + geom.w / 2 : geom.left + ((x - xMin) / (xMax - xMin)) * geom.w;

    // Левая ось — по линиям, правая — по столбцам
    const lineValues = lines.flatMap((s) => s.points.map((p) => p.y));
    const leftBounds = lineValues.length
      ? niceBounds(Math.min(...lineValues), Math.max(...lineValues))
      : { min: 0, max: 1 };
    const barMax = bars.length
      ? Math.max(...bars.flatMap((s) => s.points.map((p) => p.y)), 0)
      : 1;

    const scaleLeft = (y) =>
      geom.top + geom.h - ((y - leftBounds.min) / (leftBounds.max - leftBounds.min)) * geom.h;
    const scaleBar = (y) => geom.top + geom.h - (y / (barMax || 1)) * geom.h;

    const leftDigits = lines.length ? lines[0].digits ?? 2 : 2;
    const leftUnit = lines.length ? lines[0].unit || '' : '';
    drawGrid(svg, geom, leftBounds.min, leftBounds.max,
      (v) => fmt.num(v, leftDigits > 2 ? 2 : leftDigits) + (leftUnit === '%' ? '%' : ''));

    // Подписи правой оси — только края и середина, чтобы не спорили с левой
    if (bars.length) {
      for (let i = 0; i <= 2; i += 1) {
        const value = (barMax / 2) * i;
        svg.appendChild(
          el('text', {
            class: 'chart__axis',
            x: geom.left + geom.w + 7,
            y: scaleBar(value) + 3.5,
            'text-anchor': 'start',
          }, fmt.money(value, 0))
        );
      }
    }

    // Столбцы рисуем первыми — линии должны быть поверх
    bars.forEach((serie, index) => {
      const slotW = Math.max(1, geom.w / Math.max(serie.points.length, 1));
      const width = Math.max(1, Math.min(slotW * (bars.length > 1 ? 0.4 : 0.75), 26));
      const offset = bars.length > 1 ? (index - (bars.length - 1) / 2) * width : 0;
      const color = serie.color || seriesColor(lines.length + index);
      serie.points.forEach((p) => {
        const y = scaleBar(p.y);
        svg.appendChild(
          el('rect', {
            x: scaleX(p.x) - width / 2 + offset,
            y,
            width,
            height: Math.max(1, geom.top + geom.h - y),
            rx: 1,
            style: `fill:${color};opacity:0.5`,
          })
        );
      });
    });

    lines.forEach((serie, index) => {
      const points = [...serie.points].sort((a, b) => a.x - b.x);
      const color = serie.color || seriesColor(index);
      const path = points
        .map((p, i) => `${i === 0 ? 'M' : 'L'}${scaleX(p.x).toFixed(2)},${scaleLeft(p.y).toFixed(2)}`)
        .join(' ');
      svg.appendChild(
        el('path', {
          class: `chart__line${serie.dashed ? ' chart__line--dashed' : ''}`,
          d: path,
          style: `stroke:${color}`,
        })
      );
    });

    // Подписи оси X
    const ticks = Math.min(7, Math.max(2, Math.round(geom.w / 110)));
    for (let i = 0; i <= ticks; i += 1) {
      const x = geom.left + (geom.w * i) / ticks;
      const value = xMin + ((xMax - xMin) * i) / ticks;
      svg.appendChild(
        el('text', {
          class: 'chart__axis',
          x,
          y: opts.height - 8,
          'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle',
        }, dateTickLabel(value, spanDays))
      );
    }

    // ---- Наведение: вертикаль, точки и подсказка «дата: значение» ----
    // Прозрачность меняем атрибутом, поэтому и стартовое значение — атрибут:
    // инлайновый style его перебил бы, и перекрестье не появилось бы
    const hover = el('g', { style: 'pointer-events:none', opacity: '0' });
    const rule = el('line', {
      class: 'chart__rule', y1: geom.top, y2: geom.top + geom.h, x1: 0, x2: 0,
    });
    hover.appendChild(rule);
    const markers = active.map((serie, index) =>
      el('circle', {
        r: 3.5,
        style: `fill:${serie.color || seriesColor(index)}`,
        class: 'chart__marker',
      })
    );
    markers.forEach((marker) => hover.appendChild(marker));
    svg.appendChild(hover);

    const tip = document.createElement('div');
    tip.className = 'chart-tip';
    tip.hidden = true;
    frame.appendChild(tip);

    // Общая шкала дат: по ней ищем ближайшую точку под курсором
    const stamps = [...new Set(allX)].sort((a, b) => a - b);
    const byStamp = active.map((serie) => {
      const map = new Map();
      serie.points.forEach((p) => map.set(p.x, p.y));
      return map;
    });

    function hide() {
      hover.setAttribute('opacity', '0');
      tip.hidden = true;
    }

    function move(event) {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      // viewBox растянут по ширине контейнера — переводим пиксели в координаты
      const localX = ((event.clientX - rect.left) / rect.width) * VIEW_W;
      if (localX < geom.left - 4 || localX > geom.left + geom.w + 4) return hide();

      const ratio = (localX - geom.left) / geom.w;
      const target = xMin + ratio * (xMax - xMin);
      let nearest = stamps[0];
      let best = Infinity;
      for (const stamp of stamps) {
        const distance = Math.abs(stamp - target);
        if (distance < best) { best = distance; nearest = stamp; }
      }

      const x = scaleX(nearest);
      rule.setAttribute('x1', x);
      rule.setAttribute('x2', x);

      const rows = [];
      active.forEach((serie, index) => {
        const value = byStamp[index].get(nearest);
        const marker = markers[index];
        if (value === undefined || value === null) {
          marker.setAttribute('opacity', '0');
          return;
        }
        marker.setAttribute('opacity', '1');
        marker.setAttribute('cx', x);
        marker.setAttribute('cy', serie.kind === 'bar' ? scaleBar(value) : scaleLeft(value));
        rows.push(
          `<span class="chart-tip__row">
             <span class="chart-tip__swatch" style="background:${serie.color || seriesColor(index)}"></span>
             <span class="chart-tip__name">${fmt.esc(serie.title || serie.code)}</span>
             <span class="chart-tip__value">${fmt.esc(tipValue(value, serie))}</span>
           </span>`
        );
      });
      if (!rows.length) return hide();

      hover.setAttribute('opacity', '1');
      tip.innerHTML =
        `<div class="chart-tip__date">${fmt.date(new Date(nearest))}</div>${rows.join('')}`;
      tip.hidden = false;

      // Держим подсказку внутри карточки: у правого края разворачиваем влево
      const px = (x / VIEW_W) * rect.width;
      const width = tip.offsetWidth || 170;
      tip.style.left = `${Math.max(4, Math.min(px + 14, rect.width - width - 4))}px`;
      tip.style.top = '10px';
    }

    svg.addEventListener('mousemove', move);
    svg.addEventListener('mouseleave', hide);
    // На телефоне тем же жестом ведут палец по графику
    svg.addEventListener('touchmove', (event) => {
      if (event.touches && event.touches.length) move(event.touches[0]);
    }, { passive: true });
    svg.addEventListener('touchend', hide);

    if (active.length > 1) {
      const legend = document.createElement('div');
      legend.className = 'legend';
      active.forEach((serie, index) => {
        const item = document.createElement('span');
        item.className = 'legend__item';
        item.innerHTML =
          `<span class="legend__swatch" style="background:${serie.color || seriesColor(index)}"></span>` +
          fmt.esc(serie.title || serie.code) +
          (serie.kind === 'bar' ? ' <span class="legend__hint">(столбцы, правая ось)</span>' : '');
        legend.appendChild(item);
      });
      container.appendChild(legend);
    }
    return svg;
  }

  /**
   * Профиль выплат позиции: прибыль и убыток при разных ценах.
   *
   * curve: [{ price, pnl }]; marker — текущая цена, breakeven — точки нуля.
   * Ноль здесь не просто отметка на оси: он делит график на прибыль и
   * убыток, поэтому линия нуля рисуется всегда, а не только когда попала
   * в диапазон значений.
   */
  function payoffChart(container, curve, options = {}) {
    const opts = Object.assign(
      { height: 260, marker: null, breakeven: [], subject: 'актива' },
      options
    );
    const data = (curve || []).filter((p) => fmt.isNum(p.price) && fmt.isNum(p.pnl));
    if (!data.length) return empty(container, opts.emptyMessage);

    container.innerHTML = '';
    const frame = document.createElement('div');
    frame.className = 'chart-frame';
    container.appendChild(frame);

    const svg = el('svg', {
      class: 'chart',
      viewBox: `0 0 ${VIEW_W} ${opts.height}`,
      preserveAspectRatio: 'none',
      height: opts.height,
    });
    frame.appendChild(svg);

    const geom = { left: 62, top: 12, w: VIEW_W - 62 - 16, h: opts.height - 12 - 28 };
    const xMin = Math.min(...data.map((p) => p.price));
    const xMax = Math.max(...data.map((p) => p.price));
    // Ноль обязан попасть в шкалу: без него не видно, где кончается убыток
    const bounds = niceBounds(
      Math.min(...data.map((p) => p.pnl), 0),
      Math.max(...data.map((p) => p.pnl), 0)
    );

    const scaleX = (x) =>
      xMax === xMin ? geom.left + geom.w / 2 : geom.left + ((x - xMin) / (xMax - xMin)) * geom.w;
    const scaleY = (y) =>
      geom.top + geom.h - ((y - bounds.min) / (bounds.max - bounds.min)) * geom.h;

    drawGrid(svg, geom, bounds.min, bounds.max, (v) => fmt.money(v, 0));

    const zeroY = scaleY(0);
    const path = data
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${scaleX(p.price).toFixed(2)},${scaleY(p.pnl).toFixed(2)}`)
      .join(' ');

    // Заливка прибыли и убытка разными цветами: направление читается сразу
    const defs = el('defs');
    const clipUp = el('clipPath', { id: 'payoff-up' });
    clipUp.appendChild(el('rect', {
      x: geom.left, y: geom.top, width: geom.w, height: Math.max(0, zeroY - geom.top),
    }));
    const clipDown = el('clipPath', { id: 'payoff-down' });
    clipDown.appendChild(el('rect', {
      x: geom.left, y: zeroY, width: geom.w, height: Math.max(0, geom.top + geom.h - zeroY),
    }));
    defs.appendChild(clipUp);
    defs.appendChild(clipDown);
    svg.appendChild(defs);

    const area = `${path} L${scaleX(data[data.length - 1].price).toFixed(2)},${zeroY}` +
      ` L${scaleX(data[0].price).toFixed(2)},${zeroY} Z`;
    svg.appendChild(el('path', {
      d: area, 'clip-path': 'url(#payoff-up)',
      style: 'fill:var(--up);opacity:0.18',
    }));
    svg.appendChild(el('path', {
      d: area, 'clip-path': 'url(#payoff-down)',
      style: 'fill:var(--down);opacity:0.18',
    }));

    svg.appendChild(el('line', {
      class: 'chart__zero',
      x1: geom.left, x2: geom.left + geom.w, y1: zeroY, y2: zeroY,
    }));
    svg.appendChild(el('path', { class: 'chart__line', d: path }));

    // Текущая цена базового актива
    if (fmt.isNum(opts.marker) && opts.marker >= xMin && opts.marker <= xMax) {
      const x = scaleX(opts.marker);
      svg.appendChild(el('line', {
        class: 'chart__rule', x1: x, x2: x, y1: geom.top, y2: geom.top + geom.h,
      }));
      svg.appendChild(el('text', {
        class: 'chart__label', x: x + 4, y: geom.top + 10,
      }, `сейчас ${fmt.num(opts.marker, 2)}`));
    }

    (opts.breakeven || []).forEach((value) => {
      if (!fmt.isNum(value) || value < xMin || value > xMax) return;
      const x = scaleX(value);
      svg.appendChild(el('circle', {
        cx: x, cy: zeroY, r: 4, style: 'fill:var(--warn)',
      }));
    });

    const ticks = Math.min(7, Math.max(2, Math.round(geom.w / 110)));
    for (let i = 0; i <= ticks; i += 1) {
      const value = xMin + ((xMax - xMin) * i) / ticks;
      svg.appendChild(el('text', {
        class: 'chart__axis',
        x: geom.left + (geom.w * i) / ticks,
        y: opts.height - 8,
        'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle',
      }, fmt.num(value, value > 1000 ? 0 : 2)));
    }

    // Подсказка при наведении
    const hover = el('g', { style: 'pointer-events:none', opacity: '0' });
    const rule = el('line', {
      class: 'chart__rule', y1: geom.top, y2: geom.top + geom.h, x1: 0, x2: 0,
    });
    const dot = el('circle', { r: 4, class: 'chart__marker', style: 'fill:var(--accent)' });
    hover.appendChild(rule);
    hover.appendChild(dot);
    svg.appendChild(hover);

    const tip = document.createElement('div');
    tip.className = 'chart-tip';
    tip.hidden = true;
    frame.appendChild(tip);

    svg.addEventListener('mousemove', (event) => {
      const rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      const localX = ((event.clientX - rect.left) / rect.width) * VIEW_W;
      const ratio = (localX - geom.left) / geom.w;
      if (ratio < 0 || ratio > 1) {
        hover.setAttribute('opacity', '0');
        tip.hidden = true;
        return;
      }
      const target = xMin + ratio * (xMax - xMin);
      const nearest = data.reduce((best, p) =>
        Math.abs(p.price - target) < Math.abs(best.price - target) ? p : best, data[0]);

      const x = scaleX(nearest.price);
      rule.setAttribute('x1', x);
      rule.setAttribute('x2', x);
      dot.setAttribute('cx', x);
      dot.setAttribute('cy', scaleY(nearest.pnl));
      hover.setAttribute('opacity', '1');

      const sign = nearest.pnl > 0 ? 'up' : nearest.pnl < 0 ? 'down' : '';
      tip.innerHTML =
        `<div class="chart-tip__date">цена ${opts.subject}: ${fmt.num(nearest.price, 2)}</div>` +
        `<span class="chart-tip__row">
           <span class="chart-tip__name">результат</span>
           <span class="chart-tip__value ${sign}">${fmt.rub(nearest.pnl, 2)}</span>
         </span>`;
      tip.hidden = false;

      const px = (x / VIEW_W) * rect.width;
      const width = tip.offsetWidth || 170;
      tip.style.left = `${Math.max(4, Math.min(px + 14, rect.width - width - 4))}px`;
      tip.style.top = '10px';
    });
    svg.addEventListener('mouseleave', () => {
      hover.setAttribute('opacity', '0');
      tip.hidden = true;
    });

    return svg;
  }

  /** Столбчатый график: bars = [{ x, y, label, positive }] */
  function barChart(container, bars, options = {}) {
    const opts = Object.assign(
      { height: 180, yFormat: (v) => fmt.money(v), xFormat: (v) => String(v), colorBySign: false, xTicks: 6 },
      options
    );
    if (!bars || !bars.length) return empty(container, opts.emptyMessage);

    const svg = createSvg(container, opts.height);
    const geom = { left: 52, top: 12, w: VIEW_W - 52 - 14, h: opts.height - 12 - 26 };

    const values = bars.map((b) => b.y);
    const maxValue = Math.max(...values, 0);
    const minValue = Math.min(...values, 0);
    const span = maxValue - minValue || 1;

    const scaleY = (y) => geom.top + geom.h - ((y - minValue) / span) * geom.h;
    drawGrid(svg, geom, minValue, maxValue, opts.yFormat, 3);

    const slot = geom.w / bars.length;
    const width = Math.max(1, Math.min(slot * 0.72, 42));
    const zeroY = scaleY(0);

    bars.forEach((bar, index) => {
      const x = geom.left + slot * index + (slot - width) / 2;
      const y = scaleY(bar.y);
      let cls = 'chart__bar';
      if (opts.colorBySign) cls += bar.y >= 0 ? ' chart__bar--up' : ' chart__bar--down';

      const rect = el('rect', {
        class: cls,
        x,
        y: Math.min(y, zeroY),
        width,
        height: Math.max(1, Math.abs(zeroY - y)),
        rx: 2,
      });
      rect.appendChild(el('title', {}, bar.label || `${opts.xFormat(bar.x)}: ${opts.yFormat(bar.y)}`));
      svg.appendChild(rect);
    });

    const step = Math.max(1, Math.floor(bars.length / opts.xTicks));
    bars.forEach((bar, index) => {
      if (index % step !== 0 && index !== bars.length - 1) return;
      svg.appendChild(
        el('text', {
          class: 'chart__axis',
          x: geom.left + slot * index + slot / 2,
          y: opts.height - 8,
          'text-anchor': 'middle',
        }, opts.xFormat(bar.x))
      );
    });
    return svg;
  }

  /**
   * Цена и объём на одном полотне: линия сверху, столбцы снизу.
   * bars: [{ trade_date, close, volume }]
   */
  function priceVolumeChart(container, rows, options = {}) {
    const opts = Object.assign(
      {
        height: 260,
        priceFormat: (v) => fmt.num(v, 2),
        // Подпись оси: по дням это дата, внутри сессии — время
        xFormat: (v) => fmt.dateShort(v),
        xFormatFull: null,
      },
      options
    );
    const data = (rows || []).filter((r) => r.close !== null && r.close !== undefined);
    if (!data.length) return empty(container, opts.emptyMessage || 'Нет истории торгов');

    const svg = createSvg(container, opts.height);
    const priceH = opts.height * 0.62;
    const volumeTop = priceH + 14;
    const volumeH = opts.height - volumeTop - 26;
    const geom = { left: 52, top: 12, w: VIEW_W - 52 - 14, h: priceH - 12 };

    const prices = data.map((r) => r.close);
    const bounds = niceBounds(Math.min(...prices), Math.max(...prices));
    const maxVolume = Math.max(...data.map((r) => r.volume || 0), 1);

    const scaleX = (i) => geom.left + (data.length === 1 ? geom.w / 2 : (i / (data.length - 1)) * geom.w);
    const scaleY = (v) => geom.top + geom.h - ((v - bounds.min) / (bounds.max - bounds.min)) * geom.h;

    drawGrid(svg, geom, bounds.min, bounds.max, opts.priceFormat, 3);

    const defs = el('defs');
    const grad = el('linearGradient', { id: 'area-grad', x1: 0, y1: 0, x2: 0, y2: 1 });
    const accent = getComputedStyle(document.documentElement)
      .getPropertyValue('--accent').trim() || '#3f9d6d';
    grad.appendChild(el('stop', { offset: '0%', 'stop-color': accent, 'stop-opacity': 0.3 }));
    grad.appendChild(el('stop', { offset: '100%', 'stop-color': accent, 'stop-opacity': 0 }));
    defs.appendChild(grad);
    svg.appendChild(defs);

    const line = data.map((r, i) => `${i === 0 ? 'M' : 'L'}${scaleX(i).toFixed(2)},${scaleY(r.close).toFixed(2)}`).join(' ');
    svg.appendChild(
      el('path', {
        class: 'chart__area',
        d: `${line} L${scaleX(data.length - 1).toFixed(2)},${geom.top + geom.h} L${scaleX(0).toFixed(2)},${geom.top + geom.h} Z`,
      })
    );
    svg.appendChild(el('path', { class: 'chart__line', d: line }));

    // Объёмы окрашиваем по направлению дня — видно, на чём был оборот
    const slot = geom.w / data.length;
    const width = Math.max(1, Math.min(slot * 0.7, 22));
    data.forEach((row, i) => {
      const height = ((row.volume || 0) / maxVolume) * volumeH;
      const rising = i === 0 ? true : row.close >= data[i - 1].close;
      const rect = el('rect', {
        class: `chart__bar chart__bar--${rising ? 'up' : 'down'}`,
        x: scaleX(i) - width / 2,
        y: volumeTop + volumeH - height,
        width,
        height: Math.max(1, height),
        rx: 1,
      });
      rect.appendChild(
        el('title', {},
          `${(opts.xFormatFull || opts.xFormat)(row.trade_date)}\n` +
          `Цена: ${fmt.num(row.close, 2)}\nОбъём: ${fmt.int(row.volume)}` +
          (row.turnover ? `\nОборот: ${fmt.money(row.turnover)} ₽` : ''))
      );
      svg.appendChild(rect);
    });

    svg.appendChild(
      el('text', { class: 'chart__label', x: geom.left, y: volumeTop - 3 }, 'Объём')
    );

    const step = Math.max(1, Math.floor(data.length / 6));
    data.forEach((row, i) => {
      if (i % step !== 0 && i !== data.length - 1) return;
      svg.appendChild(
        el('text', {
          class: 'chart__axis',
          x: scaleX(i),
          y: opts.height - 8,
          'text-anchor': 'middle',
        }, opts.xFormat(row.trade_date))
      );
    });
    return svg;
  }

  /**
   * Точечная диаграмма — карта рынка.
   * points: [{ x, y, size, color, label, key }]
   */
  function scatterChart(container, points, options = {}) {
    const opts = Object.assign(
      {
        height: 420,
        xFormat: (v) => fmt.num(v, 1),
        yFormat: (v) => fmt.num(v, 1),
        xTitle: '',
        yTitle: '',
        onPick: null,
      },
      options
    );

    const data = (points || []).filter(
      (p) => fmt.isNum(p.x) && fmt.isNum(p.y)
    );
    if (!data.length) return empty(container, opts.emptyMessage);

    const svg = createSvg(container, opts.height);
    const geom = { left: 58, top: 14, w: VIEW_W - 58 - 16, h: opts.height - 14 - 34 };

    const xBounds = niceBounds(Math.min(...data.map((p) => p.x)), Math.max(...data.map((p) => p.x)));
    const yBounds = niceBounds(Math.min(...data.map((p) => p.y)), Math.max(...data.map((p) => p.y)));

    const scaleX = (x) =>
      geom.left + ((x - xBounds.min) / (xBounds.max - xBounds.min)) * geom.w;
    const scaleY = (y) =>
      geom.top + geom.h - ((y - yBounds.min) / (yBounds.max - yBounds.min)) * geom.h;

    drawGrid(svg, geom, yBounds.min, yBounds.max, opts.yFormat);

    // Вертикальная сетка по оси X
    for (let i = 0; i <= 5; i += 1) {
      const value = xBounds.min + ((xBounds.max - xBounds.min) * i) / 5;
      const x = scaleX(value);
      svg.appendChild(
        el('line', { class: 'chart__grid', x1: x, x2: x, y1: geom.top, y2: geom.top + geom.h })
      );
      svg.appendChild(
        el('text', {
          class: 'chart__axis', x, y: opts.height - 18, 'text-anchor': 'middle',
        }, opts.xFormat(value))
      );
    }

    // Размер точки — по обороту: масштабируем по корню, иначе крупные
    // выпуски закрывают собой всё поле
    const sizes = data.map((p) => p.size || 0).filter((value) => value > 0);
    const maxSize = sizes.length ? Math.max(...sizes) : 1;

    data.forEach((point) => {
      const radius = point.size
        ? 3 + Math.sqrt(point.size / maxSize) * 11
        : 4;
      const circle = el('circle', {
        cx: scaleX(point.x),
        cy: scaleY(point.y),
        r: radius.toFixed(1),
        fill: point.color || 'var(--accent)',
        'fill-opacity': 0.62,
        stroke: point.color || 'var(--accent)',
        'stroke-opacity': 0.9,
        'stroke-width': 1,
        style: opts.onPick ? 'cursor:pointer' : null,
      });
      circle.appendChild(el('title', {}, point.label || ''));
      if (opts.onPick && point.key) {
        circle.addEventListener('click', () => opts.onPick(point.key));
      }
      svg.appendChild(circle);
    });

    if (opts.xTitle) {
      svg.appendChild(
        el('text', {
          class: 'chart__label',
          x: geom.left + geom.w / 2,
          y: opts.height - 3,
          'text-anchor': 'middle',
        }, opts.xTitle)
      );
    }
    if (opts.yTitle) {
      svg.appendChild(
        el('text', {
          class: 'chart__label',
          x: 12,
          y: geom.top + geom.h / 2,
          'text-anchor': 'middle',
          transform: `rotate(-90 12 ${geom.top + geom.h / 2})`,
        }, opts.yTitle)
      );
    }
    return svg;
  }

  /** Горизонтальные полосы: структура портфеля, сценарии ставок. */
  function barsHorizontal(container, items, options = {}) {
    const opts = Object.assign(
      { valueFormat: (v) => fmt.money(v), colorBySign: false, showShare: true },
      options
    );
    if (!items || !items.length) return empty(container, opts.emptyMessage);

    const maxAbs = Math.max(...items.map((item) => Math.abs(item.value)), 1);
    const wrap = document.createElement('div');
    wrap.style.display = 'flex';
    wrap.style.flexDirection = 'column';
    wrap.style.gap = '9px';

    items.forEach((item) => {
      const share = (Math.abs(item.value) / maxAbs) * 100;
      const positive = item.value >= 0;
      const color = opts.colorBySign
        ? positive ? 'var(--up)' : 'var(--down)'
        : 'var(--accent)';

      const row = document.createElement('div');
      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;gap:12px;font-size:12px;margin-bottom:4px">
          <span style="color:var(--text-dim)">${fmt.esc(item.label)}</span>
          <span style="font-family:var(--mono);${opts.colorBySign ? `color:${positive ? 'var(--up)' : 'var(--down)'}` : ''}">
            ${opts.valueFormat(item.value)}${opts.showShare && item.share != null ? ` · ${fmt.pct(item.share, 1)}` : ''}
          </span>
        </div>
        <div style="height:7px;background:var(--bg-elev-2);border-radius:4px;overflow:hidden">
          <div style="height:100%;width:${share.toFixed(1)}%;background:${color};border-radius:4px"></div>
        </div>`;
      wrap.appendChild(row);
    });

    container.innerHTML = '';
    container.appendChild(wrap);
  }

  global.charts = {
    lineChart, barChart, timeSeriesChart, payoffChart, priceVolumeChart,
    scatterChart, barsHorizontal, empty, seriesColor,
  };
})(window);
