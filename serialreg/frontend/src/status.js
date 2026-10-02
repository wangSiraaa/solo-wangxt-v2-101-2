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

// 盘点快照结论
export const STOCKTAKE_RESULT = {
  unseen: { label: "未见", cls: "missing", hint: "未扫到，关闭时进入遗失候选" },
  seen: { label: "已见", cls: "ok", hint: "已扫描确认" },
  misplaced: { label: "错架", cls: "warning", hint: "扫到但实际位置不符" },
  review: { label: "待核查", cls: "review", hint: "盘点期间被移动/拆订/修改，有冲突" },
};

export const STOCKTAKE_OUTCOME = {
  pending: "待处理",
  lost_candidate: "遗失候选",
  lost_confirmed: "已转遗失",
  kept: "保留",
};

export const STOCKTAKE_STATUS = {
  open: "进行中",
  closed: "已关闭",
  reopened: "已重新打开",
};

export const SCAN_EVENT = {
  recorded: "已记录",
  duplicate: "重复扫描",
  out_of_scope: "范围外",
  unknown: "未知条码",
};
