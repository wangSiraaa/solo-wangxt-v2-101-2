<template>
  <div class="panel">
    <h2>盘点批次</h2>

    <!-- 启动盘点 -->
    <template v-if="!batch">
      <div class="row">
        <label class="field"><b>盘点卷（可空=全刊）</b>
          <input v-model="form.scope_volume" placeholder="如 8" style="width:110px" />
        </label>
        <label class="field"><b>批次名称（可空）</b>
          <input v-model="form.name" placeholder="2026 年度 v.8 盘点" style="width:180px" />
        </label>
        <button @click="start">启动盘点并冻结快照</button>
      </div>
      <p class="muted" style="margin-top:6px">
        启动时冻结范围内实物、装订关系、位置与版本；缺号（未发行 / 停刊后缺号）
        没有实物，永远不会因没扫到而判遗失。
      </p>

      <div v-if="history.length" class="st-history">
        <h3>历史批次（点击恢复盘点）</h3>
        <div v-for="b in history" :key="b.id" class="st-item-row">
          <code>#{{ b.id }} {{ b.name || b.scope_label }}</code>
          <span class="badge" :class="b.state === 'open' ? 'ok' : 'ceased'">
            {{ b.state === "open" ? "盘点中" : "已关闭" }}
          </span>
          <span class="muted">{{ b.created_at?.slice(0, 16).replace("T", " ") }}</span>
          <button class="tiny ghost" @click="load(b.id)">
            {{ b.state === "open" ? "继续盘点" : "查看 / 重新打开" }}
          </button>
        </div>
      </div>
    </template>

    <!-- 盘点中 / 已关闭 -->
    <template v-else>
      <div
        class="st-banner"
        :class="{
          closed: batch.state === 'closed',
          conflict: openConflicts.length > 0,
        }"
      >
        <b>#{{ batch.id }} {{ batch.name || batch.scope_label }}</b>
        <span class="badge" :class="batch.state === 'open' ? 'ok' : 'ceased'">
          {{ batch.state === "open" ? "盘点中" : "已关闭" }}
        </span>
        <span v-if="batch.closed_at" class="muted">
          关闭于 {{ batch.closed_at.slice(0, 16).replace("T", " ") }}
        </span>
        <button class="tiny ghost" style="float:right" @click="exit">退出视图</button>
      </div>

      <!-- 统计 -->
      <div class="st-stats">
        <span class="st-pill">范围 <b>{{ c.total }}</b></span>
        <span class="st-pill">已见 <b>{{ c.seen }}</b></span>
        <span class="st-pill">错架 <b>{{ c.misplaced }}</b></span>
        <span class="st-pill">待核查 <b>{{ c.review }}</b></span>
        <span class="st-pill">未见 <b>{{ c.pending }}</b></span>
        <span class="st-pill" v-if="c.acknowledged_lost">
          确认遗失 <b>{{ c.acknowledged_lost }}</b>
        </span>
        <span class="st-pill" :class="{ 'msg err': c.open_conflicts }">
          未处理冲突 <b>{{ c.open_conflicts }}</b>
        </span>
      </div>

      <!-- 扫描 -->
      <div v-if="batch.state === 'open'" class="st-scanbox">
        <label class="field"><b>条码 / 装订册索书号</b>
          <input
            v-model="barcode"
            type="text"
            placeholder="扫实体条码或装订册"
            @keyup.enter="doScan"
          />
        </label>
        <label class="field"><b>当前架位（可空）</b>
          <input v-model="observed" type="text" placeholder="如 现刊区 B-02" />
        </label>
        <button @click="doScan">扫描</button>
        <button class="ghost" @click="reload">刷新冲突</button>
      </div>
      <p v-if="scanMsg" class="msg" :class="scanMsg.err ? 'err' : 'ok'">
        {{ scanMsg.text }}
      </p>

      <!-- 冲突 -->
      <h3 v-if="conflicts.length">冲突（必须先处理，不能用旧扫描覆盖新位置）</h3>
      <div v-for="cf in conflicts" :key="cf.id" class="st-conflict">
        <div>
          <b>{{ conflictLabel(cf.kind) }}</b>
          <code v-if="cf.item_barcode">{{ cf.item_barcode }}</code>
          <span class="badge" :class="cf.status === 'open' ? 'conflict' : 'ok'">
            {{ cf.status === "open" ? "待处理" : "已解决" }}
          </span>
        </div>
        <div class="muted">{{ cf.detail }}</div>
        <div v-if="cf.status === 'open'" style="margin-top:4px">
          <button
            class="tiny"
            :disabled="batch.state !== 'open'"
            @click="resolve(cf.id)"
          >按当前数据为准，解决冲突</button>
        </div>
      </div>

      <!-- 实物清单 -->
      <h3>快照实物（{{ items.length }}）</h3>
      <div v-for="it in items" :key="it.item_id" class="st-item-row">
        <code>{{ it.barcode }}</code>
        <span class="badge" :class="resultMeta(it.result).cls">
          {{ resultMeta(it.result).label }}
        </span>
        <span class="muted">
          📍 快照 {{ it.snapshot_location || "（未排架）" }}
          <template v-if="it.snapshot_binding">（装订 {{ it.snapshot_binding }}）</template>
          → 现在 {{ it.current_location || "（未排架）" }}
        </span>
        <template v-if="batch.state === 'open'">
          <!-- 未见：可以确认遗失（已发行实体的显式动作） -->
          <button
            v-if="it.result === 'pending'"
            class="tiny danger"
            title="只有已发行实体才能确认遗失"
            @click="ackLost(it.item_id)"
          >确认找不到→遗失</button>
          <!-- 待核查 / 错架：复核操作 -->
          <button
            v-if="it.result === 'review' || it.result === 'misplaced'"
            class="tiny ghost"
            @click="markReview(it.item_id, 'seen')"
          >复核为已见</button>
          <button
            v-if="it.result === 'review' || it.result === 'misplaced'"
            class="tiny ghost"
            @click="markReview(it.item_id, 'misplaced')"
          >保持错架</button>
        </template>
      </div>

      <!-- 关闭 / 重新打开 -->
      <div class="row" style="margin-top:12px">
        <template v-if="batch.state === 'open'">
          <button class="ghost" @click="tryClose">检查范围完整性</button>
          <button @click="confirmClose" :disabled="!canClose">
            确认范围完整并关闭批次
          </button>
          <span v-if="!canClose" class="muted">
            还有未见 / 待核查 / 未处理冲突，无法关闭
          </span>
        </template>
        <template v-else>
          <button @click="reopen">恢复盘点（撤销关闭时判失）</button>
          <span class="muted">扫描事件、冲突与快照均保留可追溯。</span>
        </template>
      </div>
      <p v-if="closeMsg" class="msg" :class="closeMsg.err ? 'err' : 'ok'">
        {{ closeMsg.text }}
      </p>

      <!-- 扫描审计 -->
      <div v-if="scans.length" class="st-history">
        <h3>扫描事件（{{ scans.length }}，重复扫描不新增、不覆盖首次）</h3>
        <div v-for="s in scans" :key="s.id" class="st-event">
          {{ s.created_at?.slice(0, 19).replace("T", " ") }}
          扫 <code>{{ s.barcode }}</code>
          （{{ s.target_type === "binding" ? "装订册" : "实体" }}）
          → {{ s.resolved_item_ids.length }} 件
          <span v-if="s.observed_location">@ {{ s.observed_location }}</span>
        </div>
      </div>
    </template>

    <p v-if="msg" class="msg" :class="msg.err ? 'err' : 'ok'">{{ msg.text }}</p>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";
import { STOCKTAKE_RESULT, CONFLICT_KIND } from "../status.js";

const props = defineProps({
  titleId: [Number, String],
  // 时间轴中正在查看的批次 id（可选，用于联动标记）
  activeId: [Number, String, null],
});
const emit = defineEmits(["changed", "active-change"]);

const form = reactive({ scope_volume: "", name: "" });
const batch = ref(null);
const history = ref([]);
const barcode = ref("");
const observed = ref("");
const scanMsg = ref(null);
const closeMsg = ref(null);
const msg = ref(null);
// 与父组件联动时防止重复回写
let syncing = false;

function notify(target, text, err = false, ms = 5000) {
  target.value = { text, err };
  if (ms) setTimeout(() => (target.value = null), ms);
}
const resultMeta = (r) =>
  STOCKTAKE_RESULT[r] || { label: r, cls: "gap" };
const conflictLabel = (k) => CONFLICT_KIND[k] || k;

const items = computed(() => batch.value?.items || []);
const scans = computed(() => batch.value?.scans || []);
const conflicts = computed(() => batch.value?.conflicts || []);
const openConflicts = computed(() =>
  conflicts.value.filter((c) => c.status === "open"));
const c = computed(() => batch.value?.completeness || {});
const canClose = computed(() => c.value.complete === true);

async function loadHistory() {
  try {
    history.value = await api.listStocktakes(props.titleId);
  } catch (e) { /* 非关键 */ }
}
watch(() => props.titleId, () => {
  batch.value = null;
  loadHistory();
}, { immediate: true });
watch(() => props.activeId, async (id) => {
  if (syncing) { syncing = false; return; }
  if (id && (!batch.value || batch.value.id !== id)) await load(id);
});

async function start() {
  msg.value = null;
  try {
    batch.value = await api.createStocktake({
      title: props.titleId,
      scope_volume: form.scope_volume.trim(),
      name: form.name.trim(),
    });
    form.scope_volume = ""; form.name = "";
    syncing = true;
    emit("active-change", batch.value.id);
    emit("changed");
    loadHistory();
  } catch (e) { notify(msg, e.message, true); }
}

async function load(id) {
  batch.value = await api.stocktake(id);
  syncing = true;
  emit("active-change", id);
  emit("changed");
}

function exit() {
  batch.value = null;
  syncing = true;
  emit("active-change", null);
  emit("changed");
}

async function reload() {
  if (batch.value) {
    batch.value = await api.stocktake(batch.value.id);
    emit("changed");
  }
}

async function doScan() {
  scanMsg.value = null;
  if (!barcode.value.trim()) return;
  try {
    const r = await api.scanStocktake(batch.value.id, {
      barcode: barcode.value.trim(),
      observed_location: observed.value.trim(),
    });
    if (r.duplicate) {
      notify(scanMsg,
        `重复扫描：${r.resolved_item_ids.length} 件沿用首次扫描，未重复计数。`,
        false);
    } else {
      const extra = r.out_of_scope?.length
        ? `；${r.out_of_scope.length} 件不在本范围` : "";
      notify(scanMsg,
        `已扫描，映射 ${r.resolved_item_ids.length} 件实体${extra}。`);
    }
    if (r.open_conflicts > 0)
      notify(scanMsg, `检测到 ${r.open_conflicts} 个待处理冲突！`, true, 8000);
    barcode.value = "";
    await reload();
  } catch (e) {
    notify(scanMsg, e.message, true);
  }
}

async function markReview(itemId, result) {
  try {
    batch.value = await api.reviewStocktake(batch.value.id, {
      item_id: itemId, result,
    });
    emit("changed");
  } catch (e) { notify(msg, e.message, true); }
}

async function ackLost(itemId) {
  if (!confirm("确认这件已发行实体在盘点范围内找不到，标记为遗失？")) return;
  try {
    batch.value = await api.reviewStocktake(batch.value.id, {
      item_id: itemId, result: "lost",
    });
    emit("changed");
  } catch (e) { notify(msg, e.message, true); }
}

async function resolve(conflictId) {
  try {
    batch.value = await api.resolveConflict(batch.value.id, conflictId);
    emit("changed");
  } catch (e) { notify(msg, e.message, true); }
}

async function tryClose() {
  closeMsg.value = null;
  try {
    const r = await api.closeStocktake(batch.value.id, false);
    if (r.blocked) {
      notify(closeMsg,
        `范围未完成：未见 ${r.pending}、待核查 ${r.review}、` +
        `冲突 ${r.open_conflicts}；请处理后再关闭。`, true, 8000);
    } else if (r.needs_confirmation) {
      notify(closeMsg,
        `范围完整：已见 ${r.seen}，确认遗失 ${r.acknowledged_lost} 件。` +
        "请点「确认范围完整并关闭批次」。");
    }
  } catch (e) { notify(closeMsg, e.message, true); }
}

async function confirmClose() {
  try {
    const r = await api.closeStocktake(batch.value.id, false);
    const lostN = r.acknowledged_lost || 0;
    if (!confirm(
      `确认关闭盘点？${lostN} 件已确认遗失的实体将正式标记为丢失；` +
      "缺号（未发行）不受影响。",
    )) return;
    batch.value = await api.closeStocktake(batch.value.id, true);
    emit("changed");
    loadHistory();
    notify(msg, "盘点批次已关闭。");
  } catch (e) { notify(closeMsg, e.message || "范围未完成，无法关闭。", true); }
}

async function reopen() {
  batch.value = await api.reopenStocktake(batch.value.id);
  syncing = true;
  emit("active-change", batch.value.id);
  emit("changed");
  loadHistory();
}
</script>
