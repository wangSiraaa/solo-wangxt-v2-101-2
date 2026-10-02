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

  timeline: (titleId, stocktakeId) => {
    const qs = new URLSearchParams({ title: titleId });
    if (stocktakeId) qs.set("stocktake", stocktakeId);
    return request(`/timeline/?${qs}`);
  },
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

  // ---------- 盘点批次 ----------
  listStocktakes: (titleId, state) => {
    const qs = new URLSearchParams();
    if (titleId) qs.set("title", titleId);
    if (state) qs.set("state", state);
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/stocktakes/${tail}`);
  },
  createStocktake: (payload) =>
    request("/stocktakes/", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  stocktake: (id) => request(`/stocktakes/${id}/`),
  scanStocktake: (id, payload) =>
    request(`/stocktakes/${id}/scan/`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  reviewStocktake: (id, payload) =>
    request(`/stocktakes/${id}/review/`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  resolveConflict: (id, conflictId) =>
    request(`/stocktakes/${id}/conflicts/${conflictId}/resolve/`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  closeStocktake: (id, confirm = false) =>
    request(`/stocktakes/${id}/close/`, {
      method: "POST",
      body: JSON.stringify({ confirm }),
    }),
  reopenStocktake: (id) =>
    request(`/stocktakes/${id}/reopen/`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
};
