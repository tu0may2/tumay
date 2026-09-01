/* Тонкий клиент REST API терминала. */
(function (global) {
  'use strict';

  const BASE = '';
  const TOKEN_KEY = 'treasury-token';

  /** Токен входа. Живёт между перезагрузками страницы, пока не истечёт. */
  function token(value) {
    try {
      if (value === undefined) return localStorage.getItem(TOKEN_KEY);
      if (value === null) localStorage.removeItem(TOKEN_KEY);
      else localStorage.setItem(TOKEN_KEY, value);
    } catch (error) { /* приватный режим — работаем без запоминания */ }
    return value || null;
  }

  function authHeaders() {
    const saved = token();
    return saved ? { 'X-Auth-Token': saved } : {};
  }

  function buildQuery(params) {
    const search = new URLSearchParams();
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '') return;
      // Повторяющиеся параметры (kind, secid) передаются массивом
      if (Array.isArray(value)) {
        value.forEach((item) => search.append(key, item));
      } else {
        search.append(key, value);
      }
    });
    const query = search.toString();
    return query ? `?${query}` : '';
  }

  async function request(path, { method = 'GET', params, body, form } = {}) {
    const options = { method, headers: authHeaders() };
    if (form !== undefined) {
      // FormData: заголовок с границей проставляет сам браузер
      options.body = form;
    } else if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }

    const response = await fetch(BASE + path + buildQuery(params), options);
    if (response.status === 401 && typeof global.onAuthRequired === 'function') {
      // Сессия истекла — интерфейс должен снова показать форму входа
      token(null);
      global.onAuthRequired();
    }
    if (response.status === 204) return null;

    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }

    if (!response.ok) {
      // FastAPI отдаёт detail строкой или списком ошибок валидации
      const detail = payload && payload.detail;
      let message = `Ошибка ${response.status}`;
      if (typeof detail === 'string') {
        message = detail;
      } else if (Array.isArray(detail) && detail.length) {
        message = detail.map((item) => item.msg || JSON.stringify(item)).join('; ');
      }
      throw new Error(message);
    }
    return payload;
  }

  /**
   * Скачать файл, отданный сервером как вложение.
   * Имя берём из Content-Disposition, чтобы совпадало с тем, что дал бэкенд.
   */
  async function download(path, { method = 'GET', params, body } = {}) {
    const options = { method, headers: authHeaders() };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(BASE + path + buildQuery(params), options);
    if (!response.ok) {
      let message = `Ошибка ${response.status}`;
      try {
        const payload = await response.json();
        if (typeof payload.detail === 'string') message = payload.detail;
      } catch (error) { /* тело не JSON — оставляем код ошибки */ }
      throw new Error(message);
    }

    const disposition = response.headers.get('content-disposition') || '';
    const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    const filename = match ? decodeURIComponent(match[1]) : 'выгрузка';

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Отзываем ссылку не сразу: Safari успевает начать скачивание
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    return filename;
  }

  global.api = {
    download,
    token,
    overview: () => request('/api/overview'),
    instruments: (params) => request('/api/instruments', { params }),
    instrument: (secid, params) => request(`/api/instruments/${encodeURIComponent(secid)}`, { params }),
    curve: () => request('/api/curve'),
    movers: (params) => request('/api/movers', { params }),
    anomalies: (params) => request('/api/anomalies', { params }),
    alerts: (params) => request('/api/alerts', { params }),
    calendar: (params) => request('/api/calendar', { params }),
    fx: (params) => request('/api/fx', { params }),
    rates: (params) => request('/api/rates', { params }),
    rateCalendar: (params) => request('/api/rates/calendar', { params }),
    seriesCatalog: () => request('/api/series/catalog'),
    series: (chart, params) => request(`/api/series/${encodeURIComponent(chart)}`, { params }),
    seriesDownload: (chart, params) =>
      download(`/api/series/${encodeURIComponent(chart)}/download`, { params }),
    boards: () => request('/api/boards'),
    securityTypes: (params) => request('/api/instruments/security-types', { params }),
    health: () => request('/api/health'),
    sources: () => request('/api/sources'),
    runs: (params) => request('/api/collect/runs', { params }),
    collect: (withHistory = true) =>
      request('/api/collect', { method: 'POST', body: { with_history: withHistory } }),
    bondAnalysis: (params) => request('/api/bonds/analysis', { params }),
    bondFilters: () => request('/api/bonds/filters'),
    exportParameters: () => request('/api/export/parameters'),
    exportPreview: (body) => request('/api/export/preview', { method: 'POST', body }),
    portfolio: (name, method) => request('/api/portfolio', { params: { name, method } }),
    cashflow: (name, horizonDays) =>
      request('/api/portfolio/cashflow', { params: { name, horizon_days: horizonDays } }),
    benchmark: (name, days) => request('/api/benchmark', { params: { name, days } }),
    spreadHistory: (secid, days) =>
      request(`/api/instruments/${encodeURIComponent(secid)}/spread-history`, { params: { days } }),
    intraday: (secid, params) =>
      request(`/api/instruments/${encodeURIComponent(secid)}/intraday`, { params }),
    accrued: (secid, onDate) =>
      request(`/api/instruments/${encodeURIComponent(secid)}/accrued`, {
        params: { on_date: onDate },
      }),
    limitKinds: () => request('/api/limits/kinds'),
    limits: (portfolio) => request('/api/limits', { params: { portfolio } }),
    createLimit: (body) => request('/api/limits', { method: 'POST', body }),
    deleteLimit: (id) => request(`/api/limits/${id}`, { method: 'DELETE' }),
    checkLimits: (portfolio) => request('/api/limits/check', { params: { portfolio } }),
    previewTrade: (body) => request('/api/limits/preview', { method: 'POST', body }),
    watchlist: (name) => request('/api/watchlist', { params: { name } }),
    addWatch: (body) => request('/api/watchlist', { method: 'POST', body }),
    removeWatch: (id) => request(`/api/watchlist/${id}`, { method: 'DELETE' }),
    screens: (view) => request('/api/screens', { params: { view } }),
    saveScreen: (body) => request('/api/screens', { method: 'POST', body }),
    deleteScreen: (id) => request(`/api/screens/${id}`, { method: 'DELETE' }),
    portfolioNames: () => request('/api/portfolio/names'),
    sensitivity: (name) => request('/api/portfolio/sensitivity', { params: { name } }),
    deals: (params) => request('/api/portfolio/deals', { params }),
    createDeal: (deal) => request('/api/portfolio/deals', { method: 'POST', body: deal }),
    createDealsBulk: (body) => request('/api/portfolio/deals/bulk', { method: 'POST', body }),
    deleteDeal: (id) => request(`/api/portfolio/deals/${id}`, { method: 'DELETE' }),

    // Деньги
    cashPosition: (portfolio) => request('/api/cash/position', { params: { portfolio } }),
    cashCalendar: (portfolio, horizonDays) =>
      request('/api/cash/calendar', { params: { portfolio, horizon_days: horizonDays } }),
    downloadCalendar: (portfolio, horizonDays, fmt = 'xlsx') =>
      download('/api/cash/calendar/download', {
        params: { portfolio, horizon_days: horizonDays, fmt },
      }),
    cashHistory: (portfolio, days) =>
      request('/api/cash/history', { params: { portfolio, days } }),

    // Календарь по статьям и выгрузка по лицевым счетам
    calendarMatrix: (params) => request('/api/cash/matrix', { params }),
    downloadMatrix: (params) => download('/api/cash/matrix/download', { params }),
    ledgerSheet: (onDate) => request('/api/cash/ledger', { params: { on_date: onDate } }),
    ledgerRules: () => request('/api/cash/ledger/rules'),
    ledgerPreview: (file, onDate) => {
      const form = new FormData();
      form.append('file', file);
      if (onDate) form.append('on_date', onDate);
      return request('/api/cash/ledger/preview', { method: 'POST', form });
    },
    ledgerApply: (payload) =>
      request('/api/cash/ledger/apply', { method: 'POST', body: payload }),
    deleteLedger: (onDate) =>
      request(`/api/cash/ledger/${onDate}`, { method: 'DELETE' }),

    cashAccounts: (portfolio) => request('/api/cash/accounts', { params: { portfolio } }),
    createAccount: (body) => request('/api/cash/accounts', { method: 'POST', body }),
    deleteAccount: (id) => request(`/api/cash/accounts/${id}`, { method: 'DELETE' }),
    cashFlows: (params) => request('/api/cash/flows', { params }),
    createFlow: (body) => request('/api/cash/flows', { method: 'POST', body }),
    deleteFlow: (id) => request(`/api/cash/flows/${id}`, { method: 'DELETE' }),
    placements: (portfolio) => request('/api/cash/placements', { params: { portfolio } }),
    createPlacement: (body) => request('/api/cash/placements', { method: 'POST', body }),
    deletePlacement: (id) => request(`/api/cash/placements/${id}`, { method: 'DELETE' }),

    // Импорт и сверка
    importColumns: () => request('/api/import/columns'),
    importPreview: (file, portfolio) => {
      const form = new FormData();
      form.append('file', file);
      if (portfolio) form.append('portfolio', portfolio);
      return request('/api/import/preview', { method: 'POST', form });
    },
    importApply: (deals) => request('/api/import/apply', { method: 'POST', body: { deals } }),
    reconcile: (file, portfolio) => {
      const form = new FormData();
      form.append('file', file);
      if (portfolio) form.append('portfolio', portfolio);
      return request('/api/import/reconcile', { method: 'POST', form });
    },

    // Импорт портфеля книгой: портфели, остатки и сделки сразу.
    // Скачивание идёт через download(), а не ссылкой: вход в терминал
    // работает по токену в заголовке, которого у обычной ссылки нет
    downloadPortfolioTemplate: () => download('/api/import/portfolio/template'),
    portfolioImportPreview: (file) => {
      const form = new FormData();
      form.append('file', file);
      return request('/api/import/portfolio/preview', { method: 'POST', form });
    },
    portfolioImportApply: (payload) =>
      request('/api/import/portfolio/apply', { method: 'POST', body: payload }),

    // Нормативы, обеспечение ЦБ и график выплат
    ratios: (portfolio, onDate) =>
      request('/api/ratios', { params: { portfolio, on_date: onDate } }),
    saveRatioInputs: (body) =>
      request('/api/ratios/inputs', { method: 'PUT', body }),
    simulateRatios: (amountRub, eligible, portfolio) =>
      request('/api/ratios/simulate', {
        params: { amount_rub: amountRub, eligible, portfolio },
      }),
    portfolioCollateral: (portfolio) =>
      request('/api/ratios/collateral', { params: { portfolio } }),
    collateralList: (params) => request('/api/collateral', { params }),
    payments: (secid, quantity) =>
      request(`/api/instruments/${encodeURIComponent(secid)}/payments`, {
        params: { quantity },
      }),

    // Переоценка и виды учёта
    revaluation: (name) => request('/api/portfolio/revaluation', { params: { name } }),
    downloadRevaluation: (name, fmt = 'xlsx') =>
      download('/api/portfolio/revaluation/download', { params: { name, fmt } }),
    accounting: () => request('/api/portfolio/accounting'),
    setAccounting: (name, accountingType) =>
      request('/api/portfolio/accounting', {
        method: 'PUT',
        body: { name, accounting_type: accountingType },
      }),

    // История, отчёт, налоги, оферты
    portfolioHistory: (name, days) =>
      request('/api/history/portfolio', { params: { name, days } }),
    takeSnapshot: (name) => request('/api/history/snapshot', { method: 'POST', params: { name } }),
    offers: (name, horizonDays) =>
      request('/api/offers', { params: { name, horizon_days: horizonDays } }),
    taxes: () => request('/api/taxes'),
    events: (name) => request('/api/events', { params: { name } }),

    // Доступ и администрирование
    authMode: () => request('/api/auth/mode'),
    login: (login, password) =>
      request('/api/auth/login', { method: 'POST', body: { login, password } }),
    logout: () => request('/api/auth/logout', { method: 'POST' }),
    me: () => request('/api/auth/me'),
    users: () => request('/api/users'),
    createUser: (body) => request('/api/users', { method: 'POST', body }),
    changePassword: (body) => request('/api/auth/password', { method: 'POST', body }),
    disableUser: (id) => request(`/api/users/${id}`, { method: 'DELETE' }),
    audit: (params) => request('/api/audit', { params }),
    notificationRules: () => request('/api/notifications'),
    createRule: (body) => request('/api/notifications', { method: 'POST', body }),
    deleteRule: (id) => request(`/api/notifications/${id}`, { method: 'DELETE' }),
    sendNotifications: (name, dryRun) =>
      request('/api/notifications/send', { method: 'POST', params: { name, dry_run: dryRun } }),
  };
})(window);
