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

  global.api = {
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
    portfolio: (name) => request('/api/portfolio', { params: { name } }),
    portfolioNames: () => request('/api/portfolio/names'),
    sensitivity: (name) => request('/api/portfolio/sensitivity', { params: { name } }),
    deals: (params) => request('/api/portfolio/deals', { params }),
    createDeal: (deal) => request('/api/portfolio/deals', { method: 'POST', body: deal }),
    deleteDeal: (id) => request(`/api/portfolio/deals/${id}`, { method: 'DELETE' }),
  };
})(window);
