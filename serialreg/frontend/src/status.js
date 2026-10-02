// 馆藏/编号槽位状态的展示字典。
// not_published / ceased_gap 都是「缺号」——没有发行记录，不自动等同缺藏。
export const HOLDING_STATUS = {
  "issued+held": { label: "已入藏", cls: "ok", hint: "已发行且有在馆实物" },
  "issued+missing": {
    label: "缺藏",
    cls: "missing",
    hint: "已发行但没有可用实物",
  },
  not_published: {
    label: "缺号",
    cls: "gap",
    hint: "没有发行记录，不自动等同缺藏",
  },
  ceased_gap: {
    label: "停刊后缺号",
    cls: "ceased",
    hint: "停刊月份之后，再无发行",
  },
  unregistered: { label: "未登记", cls: "gap", hint: "编号槽位不存在" },
};

export const ITEM_STATUS = {
  available: "在馆",
  checked_out: "借出",
  lost: "丢失",
  bound: "已装订",
};

export const ISSUE_KIND = { regular: "普通期", combined: "两期合刊" };

// 盘点结果标记
export const STOCKTAKE_RESULT = {
  pending: { label: "未见", cls: "missing", hint: "尚未扫到" },
  seen: { label: "已见", cls: "ok", hint: "盘点已扫到，位置一致" },
  misplaced: { label: "错架", cls: "warn", hint: "扫到但不在快照位置" },
  review: { label: "待核查", cls: "conflict", hint: "盘点期间数据有变化" },
  lost: { label: "盘点遗失", cls: "missing", hint: "已确认遗失" },
};

export const CONFLICT_KIND = {
  item_changed: "实物在盘点期间被修改（位置/状态）",
  moved_in: "实物盘点期间新进入范围",
  moved_out: "实物盘点期间离开范围",
  unbound: "盘点期间被拆订",
  bound: "盘点期间被装订",
  rebound: "装订成员重组",
};
