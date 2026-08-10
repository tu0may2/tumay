/* Тонкий клиент REST API терминала. */
(function (global) {
  'use strict';

  const BASE = '';

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

  async function request(path, { method = 'GET', params, body } = {}) {
    const options = { method, headers: {} };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }

    const response = await fetch(BASE + path + buildQuery(params), options);
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
    const options = { method, headers: {} };
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
    boards: () => request('/api/boards'),
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
    deleteDeal: (id) => request(`/api/portfolio/deals/${id}`, { method: 'DELETE' }),
  };
})(window);
