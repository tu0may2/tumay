/* Форматирование чисел и дат под русскую финансовую нотацию. */
(function (global) {
  'use strict';

  const NBSP = ' ';

  function isNum(value) {
    return typeof value === 'number' && isFinite(value);
  }

  /** Число с разделителем разрядов. */
  function num(value, digits = 2) {
    if (!isNum(value)) return '—';
    return value.toLocaleString('ru-RU', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  /** Компактная запись крупных сумм: 1.2 млрд, 340 млн. */
  function money(value, digits = 2) {
    if (!isNum(value)) return '—';
    const abs = Math.abs(value);
    if (abs >= 1e12) return num(value / 1e12, 2) + NBSP + 'трлн';
    if (abs >= 1e9) return num(value / 1e9, 2) + NBSP + 'млрд';
    if (abs >= 1e6) return num(value / 1e6, 1) + NBSP + 'млн';
    if (abs >= 1e3) return num(value / 1e3, 1) + NBSP + 'тыс';
    return num(value, digits);
  }

  /** Полная сумма в рублях — для итогов, где важна точность. */
  function rub(value, digits = 0) {
    if (!isNum(value)) return '—';
    return num(value, digits) + NBSP + '₽';
  }

  /**
   * Цена с точностью по величине: копеечные бумаги (ТГК-1, ВТБ) не должны
   * схлопываться в «0,00».
   */
  function price(value) {
    if (!isNum(value)) return '—';
    const abs = Math.abs(value);
    if (abs === 0) return '0';
    if (abs >= 1) return num(value, 2);
    if (abs >= 0.01) return num(value, 4);
    return num(value, 6);
  }

  function pct(value, digits = 2) {
    if (!isNum(value)) return '—';
    return num(value, digits) + '%';
  }

  /** Процент со знаком — для изменений цены. */
  function signedPct(value, digits = 2) {
    if (!isNum(value)) return '—';
    return (value > 0 ? '+' : '') + num(value, digits) + '%';
  }

  function bp(value) {
    if (!isNum(value)) return '—';
    return (value > 0 ? '+' : '') + num(value, 0) + NBSP + 'бп';
  }

  function int(value) {
    if (!isNum(value)) return '—';
    return Math.round(value).toLocaleString('ru-RU');
  }

  /** Класс окраски по знаку. */
  function trendClass(value) {
    if (!isNum(value) || value === 0) return 'dim';
    return value > 0 ? 'up' : 'down';
  }

  function date(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (isNaN(parsed)) return String(value);
    return parsed.toLocaleDateString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
    });
  }

  function dateShort(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (isNaN(parsed)) return String(value);
    return parsed.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
  }

  function time(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (isNaN(parsed)) return String(value);
    return parsed.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  function dateTime(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (isNaN(parsed)) return String(value);
    return parsed.toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  /** Экранирование текста из источников перед вставкой в разметку. */
  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** Склонение существительного: 1 день, 2 дня, 5 дней. */
  function plural(count, one, few, many) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
    return many;
  }

  global.fmt = {
    num, money, rub, price, pct, signedPct, bp, int,
    trendClass, date, dateShort, time, dateTime, esc, plural, isNum,
  };
})(window);
