const BASE = "/api";

async function request(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const detail =
      typeof data === "object" && data
        ? Object.entries(data)
            .map(([k, v]) => `${k}: ${[].concat(v).join("；")}`)
            .join("｜")
        : String(data);
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return data;
}

export const api = {
  listTitles: () => request("/titles/"),
  createTitle: (payload) =>
    request("/titles/", { method: "POST", body: JSON.stringify(payload) }),
  updateTitle: (id, payload) =>
    request(`/titles/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  timeline: (titleId) => request(`/timeline/?title=${titleId}`),
  listNumbers: (titleId) => request(`/numbers/?title=${titleId}`),

  createNumber: (payload) =>
    request("/numbers/", { method: "POST", body: JSON.stringify(payload) }),
  createIssue: (payload) =>
    request("/issues/", { method: "POST", body: JSON.stringify(payload) }),
  createItem: (payload) =>
    request("/items/", { method: "POST", body: JSON.stringify(payload) }),
  setItemStatus: (id, status) =>
    request(`/items/${id}/`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  locate: (params) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null),
    ).toString();
    return request(`/items/locate/?${qs}`);
  },

  listBindings: (titleId) =>
    request(titleId ? `/bindings/?title=${titleId}` : "/bindings/"),
  bind: (payload) =>
    request("/bindings/", { method: "POST", body: JSON.stringify(payload) }),
  unbind: (bindingId) =>
    request("/bindings/unbind/", {
      method: "POST",
      body: JSON.stringify({ binding_id: bindingId }),
    }),

  // ---------- 盘点 ----------
  listStocktakes: (titleId) =>
    request(titleId ? `/stocktakes/?title=${titleId}` : "/stocktakes/"),
  stocktake: (id) => request(`/stocktakes/${id}/`),
  createStocktake: (payload) =>
    request("/stocktakes/", { method: "POST", body: JSON.stringify(payload) }),
  scanStocktake: (id, payload) =>
    request(`/stocktakes/${id}/scan/`, {
      method: "POST", body: JSON.stringify(payload),
    }),
  refreshStocktake: (id, payload = {}) =>
    request(`/stocktakes/${id}/refresh/`, {
      method: "POST", body: JSON.stringify(payload),
    }),
  resolveConflict: (id, payload) =>
    request(`/stocktakes/${id}/resolve_conflict/`, {
      method: "POST", body: JSON.stringify(payload),
    }),
  confirmComplete: (id, payload = {}) =>
    request(`/stocktakes/${id}/confirm_complete/`, {
      method: "POST", body: JSON.stringify(payload),
    }),
  closeStocktake: (id, payload = {}) =>
    request(`/stocktakes/${id}/close/`, {
      method: "POST", body: JSON.stringify(payload),
    }),
  confirmLosses: (id, itemIds) =>
    request(`/stocktakes/${id}/confirm_losses/`, {
      method: "POST", body: JSON.stringify({ item_ids: itemIds }),
    }),
  reopenStocktake: (id, payload = {}) =>
    request(`/stocktakes/${id}/reopen/`, {
      method: "POST", body: JSON.stringify(payload),
    }),
};
