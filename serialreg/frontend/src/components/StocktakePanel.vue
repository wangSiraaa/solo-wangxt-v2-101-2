<template>
  <div class="panel stocktake-panel">
    <h2>年度盘点</h2>

    <!-- 无进行中批次：启动盘点 -->
    <template v-if="!batch">
      <p class="muted">
        启动后冻结该范围内实体、装订关系、位置与版本快照；
        <b>缺号（未发行 / 停刊后缺号）永远不会因没扫到而判遗失</b>。
      </p>
      <div class="row">
        <label class="field"><b>批次名称</b>
          <input v-model="form.name" placeholder="如 2024 年度盘点" style="width:160px" />
        </label>
        <label class="field"><b>库位范围（可空）</b>
          <input v-model="form.scope_location" placeholder="空=全部库位" style="width:160px" />
        </label>
        <button @click="start">启动盘点（冻结快照）</button>
      </div>
      <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>

      <template v-if="history.length">
        <h3>历史批次</h3>
        <div v-for="b in history" :key="b.id" class="binding-card">
          <div class="row">
            <strong>#{{ b.id }} {{ b.name || "盘点批次" }}</strong>
            <span class="badge" :class="b.status === 'closed' ? 'gap' : 'ok'">
              {{ STOCKTAKE_STATUS[b.status] || b.status }}
            </span>
            <span class="loc">{{ b.scope_location || "全部库位" }}</span>
            <span class="muted">
              已见 {{ b.stats.seen }} · 错架 {{ b.stats.misplaced }} ·
              未见 {{ b.stats.unseen }} · 待核查 {{ b.stats.review }}
            </span>
            <button v-if="b.status === 'closed'" class="tiny ghost"
                    @click="reopen(b.id)">恢复盘点</button>
            <button v-else class="tiny ghost" @click="open(b.id)">继续盘点</button>
          </div>
        </div>
      </template>
    </template>

    <!-- 进行中批次 -->
    <template v-else>
      <div class="st-head">
        <div>
          <strong>#{{ batch.id }} {{ batch.name || "盘点批次" }}</strong>
          <span class="badge" :class="batch.status === 'closed' ? 'gap' : 'ok'">
            {{ STOCKTAKE_STATUS[batch.status] }}
          </span>
          <span class="loc">📍 {{ batch.scope_location || "全部库位" }}</span>
        </div>
        <div>
          <button class="tiny ghost" @click="doRefresh">🔄 刷新比较版本</button>
          <button class="tiny ghost" @click="reload">重新载入</button>
        </div>
      </div>

      <!-- 扫描栏 -->
      <div v-if="isOpen" class="row scan-row">
        <label class="field"><b>扫描条码</b>
          <input v-model="scanCode" placeholder="实体条码 或 装订册索书号"
                 style="width:220px"
                 @keyup.enter="doScan" />
        </label>
        <label class="field"><b>扫到位置（可空）</b>
          <input v-model="scanLocation" placeholder="用于判定错架" style="width:150px"
                 @keyup.enter="doScan" />
        </label>
        <button @click="doScan">扫描</button>
      </div>
      <p v-if="scanMsg" class="msg" :class="scanMsg.err ? 'err' : 'ok'">
        {{ scanMsg.text }}
      </p>

      <!-- 统计 -->
      <div class="st-stats">
        <span class="st-chip ok">已见 {{ stats.seen }}</span>
        <span class="st-chip warning">错架 {{ stats.misplaced }}</span>
        <span class="st-chip missing">未见 {{ stats.unseen }}</span>
        <span class="st-chip review">待核查 {{ stats.review }}</span>
        <span class="st-chip gap">缺号 {{ gapNumbers.length }}（永不判遗失）</span>
        <span v-if="!isOpen && lostRows.length" class="st-chip missing">
          遗失候选 {{ lostRows.length }}
        </span>
      </div>
      <ul v-if="blockers.length" class="st-blockers">
        <li v-for="(b, i) in blockers" :key="i">⛔ {{ b }}</li>
      </ul>

      <!-- 缺号编号（not_published / ceased_gap）：只读提示 -->
      <details v-if="gapNumbers.length" class="st-gaps">
        <summary>缺号编号（{{ gapNumbers.length }}）——盘点不会改变它们</summary>
        <span v-for="g in gapNumbers" :key="g.number_id" class="muted">
          v.{{ g.volume || "—" }} no.{{ g.number }}
          （{{ g.frozen_holding_status === "ceased_gap" ? "停刊后缺号" : "未发行" }}）；
        </span>
      </details>

      <!-- 快照行 -->
      <table class="st-table">
        <thead>
          <tr>
            <th>条码</th><th>冻结位置</th><th>当前位置</th><th>盘点结论</th>
            <th>冲突处理</th><th>关闭去向</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in batch.snapshot_items" :key="row.id"
              :class="rowCls(row)">
            <td><code>{{ row.barcode }}</code>
              <span v-if="row.frozen_bound" class="muted">
                （册 {{ row.frozen_binding_call_number }}）
              </span>
            </td>
            <td>{{ row.frozen_actual_location || "（未排架）" }}</td>
            <td>
              {{ row.current_location || "（未排架）" }}
              <span v-if="row.current_binding" class="muted">
                （册 {{ row.current_binding }}）
              </span>
            </td>
            <td>
              <span class="badge" :class="resultMeta(row.result).cls">
                {{ resultMeta(row.result).label }}
              </span>
              <div v-if="row.observed_location" class="muted tiny-text">
                扫于 {{ row.observed_location }}
              </div>
            </td>
            <td>
              <template v-for="c in openConflictsOf(row)" :key="c.id">
                <div class="st-conflict">
                  ⚠ {{ c.kind_display }}：{{ c.detail }}
                  <div class="muted tiny-text">
                    快照：{{ c.snapshot_value || "—" }} → 现状：{{ c.current_value || "—" }}
                  </div>
                  <button class="tiny ok-btn"
                          @click="resolve(c, 'seen')">按现状确认已见</button>
                  <button class="tiny ghost"
                          @click="resolve(c, 'unseen')">按未见重新核查</button>
                </div>
              </template>
            </td>
            <td>
              <span v-if="row.outcome === 'lost_candidate'" class="badge missing">
                遗失候选
              </span>
              <span v-else-if="row.outcome === 'lost_confirmed'" class="badge missing">
                已转遗失
              </span>
              <span v-else-if="row.outcome === 'kept'" class="muted">保留</span>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 范围级冲突 -->
      <div v-for="c in scopeConflicts" :key="c.id" class="st-conflict scope">
        ⚠ {{ c.detail }}
        <button class="tiny ok-btn" @click="resolve(c, 'scope')">
          确认范围变化（纳入新对象）
        </button>
      </div>

      <!-- 关闭区 -->
      <div class="st-close">
        <template v-if="isOpen">
          <label class="field">
            <input type="checkbox"
                   :checked="batch.completeness_confirmed"
                   :disabled="batch.completeness_confirmed"
                   @change="toggleComplete" />
            <b>我确认盘点范围完整、应盘对象已全部核对</b>
          </label>
          <button class="danger" :disabled="!batch.completeness_confirmed"
                  @click="close">关闭批次</button>
          <span class="muted">
            关闭后只有“已发行未见”进入遗失候选，仍需逐条确认才会转丢失。
          </span>
        </template>
        <template v-else>
          <button @click="confirmAllLosses" :disabled="!lostRows.length">
            确认全部遗失候选（{{ lostRows.length }}）
          </button>
          <button class="ghost" @click="reopen(batch.id)">恢复盘点</button>
        </template>
      </div>

      <!-- 审计 -->
      <details class="st-audit">
        <summary>审计流水（{{ batch.audit_logs.length }}）</summary>
        <ul>
          <li v-for="a in [...batch.audit_logs].reverse()" :key="a.id">
            <span class="muted">{{ a.created_at?.slice(0, 19).replace("T", " ") }}</span>
            [{{ a.action_display }}] {{ a.detail }}
          </li>
        </ul>
      </details>
    </template>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";
import {
  STOCKTAKE_RESULT, STOCKTAKE_STATUS,
} from "../status.js";

const props = defineProps({
  titleId: [Number, String],
  timeline: Object,
});
const emit = defineEmits(["changed"]);

const batch = ref(null);
const history = ref([]);
const msg = ref(null);
const scanMsg = ref(null);
const form = reactive({ name: "", scope_location: "" });
const scanCode = ref("");
const scanLocation = ref("");

const isOpen = computed(
  () => batch.value && ["open", "reopened"].includes(batch.value.status),
);
const stats = computed(() => batch.value?.stats || {});
const blockers = computed(() => batch.value?.blockers || []);
const gapNumbers = computed(() => stats.value.gap_numbers || []);
const scopeConflicts = computed(() =>
  (batch.value?.conflicts || []).filter(
    (c) => c.status === "open" && c.kind === "scope_changed",
  ),
);
const lostRows = computed(() =>
  (batch.value?.snapshot_items || []).filter(
    (r) => r.outcome === "lost_candidate",
  ),
);

function resultMeta(r) {
  return STOCKTAKE_RESULT[r] || { label: r, cls: "gap" };
}
function rowCls(row) {
  return {
    "row-review": row.result === "review",
    "row-misplaced": row.result === "misplaced",
    "row-unseen": row.result === "unseen" && isOpen.value,
  };
}
function openConflictsOf(row) {
  return (row.conflicts || []).filter((c) => c.status === "open");
}
function notify(text, err = false, box = msg) {
  box.value = { text, err };
  setTimeout(() => (box.value = null), 5000);
}

async function loadHistory() {
  if (!props.titleId) return;
  try {
    history.value = await api.listStocktakes(props.titleId);
    const active = history.value.find((b) =>
      ["open", "reopened"].includes(b.status));
    if (active) await open(active.id);
  } catch (e) { /* 列表非关键路径 */ }
}
watch(() => props.titleId, loadHistory, { immediate: true });

async function reload() {
  if (!batch.value) return;
  batch.value = await api.stocktake(batch.value.id);
}
async function open(id) {
  batch.value = await api.stocktake(id);
}

async function start() {
  try {
    const created = await api.createStocktake({
      title: props.titleId,
      name: form.name,
      scope_location: form.scope_location,
    });
    form.name = ""; form.scope_location = "";
    batch.value = created;
    notify("盘点已启动，快照已冻结。可以开始扫描。");
    emit("changed");
  } catch (e) { notify(e.message, true); }
}

async function doScan() {
  const code = scanCode.value.trim();
  if (!code) return;
  try {
    const r = await api.scanStocktake(batch.value.id, {
      barcode: code, observed_location: scanLocation.value.trim(),
    });
    await reload();
    notify(`${r.detail}${r.duplicate ? "（重复，只计一次）" : ""}`,
           r.conflict_ids?.length, scanMsg);
    scanCode.value = "";
  } catch (e) { notify(e.message, true, scanMsg); }
}

async function doRefresh() {
  try {
    const r = await api.refreshStocktake(batch.value.id);
    await reload();
    notify(r.detail, r.conflict_ids.length > 0, scanMsg);
  } catch (e) { notify(e.message, true, scanMsg); }
}

async function resolve(conflict, resolution) {
  try {
    const r = await api.resolveConflict(batch.value.id, {
      conflict_id: conflict.id, resolution,
    });
    await reload();
    notify(r.detail, false, scanMsg);
  } catch (e) { notify(e.message, true, scanMsg); }
}

async function toggleComplete(e) {
  try {
    if (e.target.checked) {
      await api.confirmComplete(batch.value.id);
    }
    await reload();
  } catch (err) { notify(err.message, true, scanMsg); }
}

async function close() {
  try {
    const r = await api.closeStocktake(batch.value.id);
    await reload();
    notify(r.detail);
    emit("changed");
  } catch (e) {
    await reload(); // 关闭被阻止时也要把新发现的冲突展示出来
    notify(e.message, true, scanMsg);
  }
}

async function confirmAllLosses() {
  const ids = lostRows.value.map((r) => r.item_id);
  if (!ids.length) return;
  try {
    const r = await api.confirmLosses(batch.value.id, ids);
    await reload();
    notify(r.detail);
    emit("changed");
  } catch (e) { notify(e.message, true, scanMsg); }
}

async function reopen(id) {
  try {
    const r = await api.reopenStocktake(id || batch.value.id);
    await loadHistory();
    notify(r.detail);
    emit("changed");
  } catch (e) { notify(e.message, true); }
}
</script>
