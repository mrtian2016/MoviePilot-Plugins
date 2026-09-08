<template>
  <CheckinDashboard
    v-if="isCheckinDashboard"
    :api="api"
    :config="config"
    :allow-refresh="allowRefresh"
    :refresh-interval="refreshInterval" />
  <div v-else ref="widgetRef" class="cloud-dashboard">
    <v-card :flat="cardFlat" :loading="loading" class="dashboard-card">
      <v-card-item class="dashboard-header">
        <template #prepend>
          <v-avatar color="primary" variant="tonal" size="36">
            <v-icon icon="mdi-cloud-sync-outline" size="20" />
          </v-avatar>
        </template>
        <v-card-title class="dashboard-title">{{ cardTitle }}</v-card-title>
        <v-card-subtitle>{{ cardSubtitle }}</v-card-subtitle>
      </v-card-item>

      <v-card-text class="dashboard-body">
        <div v-if="loading && !loaded" class="dashboard-state">
          <v-progress-circular indeterminate color="primary" size="28" />
        </div>
        <v-alert v-else-if="error" type="error" variant="tonal" density="compact" class="text-caption">
          {{ error }}
        </v-alert>
        <template v-else-if="loaded">
          <div class="runtime-line">
            <span class="runtime-mark" :class="`runtime-mark--${statusColor}`" />
            <div class="runtime-copy">
              <strong>{{ statusText }}</strong>
              <span>{{ overview.runtime?.task || "当前没有订阅处理任务" }}</span>
            </div>
            <v-chip size="x-small" variant="tonal">{{ activeTaskCount }} 个任务</v-chip>
          </div>

          <div class="metric-grid">
            <div v-for="item in overview.stats || []" :key="item.title">
              <span>{{ item.title }}</span>
              <strong :class="`text-${item.color || 'primary'}`">
                {{ item.value ?? 0 }}
              </strong>
            </div>
          </div>

          <div v-if="recentHistory.length" class="recent-list">
            <div v-for="item in recentHistory" :key="historyKey(item)">
              <v-icon
                :icon="item.status === '成功' ? 'mdi-check-circle-outline' : 'mdi-alert-circle-outline'"
                :color="item.status === '成功' ? 'success' : 'error'"
                size="16" />
              <span>{{ item.title || item.file_name || "未知媒体" }}</span>
              <small>{{ item.time || "" }}</small>
            </div>
          </div>
          <div v-else class="dashboard-empty text-medium-emphasis">暂无转存记录</div>
        </template>
      </v-card-text>

      <v-divider v-if="allowRefresh" />
      <v-card-actions v-if="allowRefresh" class="dashboard-actions">
        <span class="text-caption text-disabled">
          {{ refreshedText || "等待更新" }}
        </span>
        <v-spacer />
        <v-btn icon="mdi-refresh" variant="text" size="small" :loading="loading" title="刷新" @click="loadOverview" />
      </v-card-actions>
    </v-card>
  </div>
</template>

<script setup>
import {computed, onMounted, onUnmounted, ref, watch} from "vue";
import {connectRuntimeStream} from "../utils/runtimeStream.js";
import CheckinDashboard from "./dashboard/CheckinDashboard.vue";

const props = defineProps({
  api: {type: Object, default: () => ({})},
  config: {type: Object, default: () => ({attrs: {}})},
  allowRefresh: {type: Boolean, default: true},
  refreshInterval: {type: Number, default: 0},
})

const loading = ref(false);
const loaded = ref(false);
const error = ref("");
const refreshedAt = ref(0);
const overview = ref({runtime: {}, stats: [], recent_history: []});
let refreshTimer = null;
let runtimeStream = null;
let runtimeStreamFailures = 0;
let runtimeStreamDisabled = false;

const attrs = computed(() => props.config?.attrs || {});
const dashboardType = computed(() => props.config?.key || attrs.value.dashboard || "overview");
const isCheckinDashboard = computed(() => dashboardType.value === "checkin");
const cardTitle = computed(() => attrs.value.title || "网盘订阅助手");
const cardSubtitle = computed(() => attrs.value.subtitle || "订阅任务与转存概览");
const cardFlat = computed(() => attrs.value.border === false);
const refreshSeconds = computed(() => {
  const value = Number(props.refreshInterval || attrs.value.refresh || 0);
  return Number.isFinite(value) ? value : 0;
})
const recentHistory = computed(() => (overview.value.recent_history || []).slice(0, 3));
const activeTaskCount = computed(
  () =>
    (overview.value.runtime?.tasks || []).filter((task) =>
      ["queued", "running", "stopping", "postprocessing"].includes(task.status),
    ).length,
)
const statusColor = computed(() =>
  ["starting", "running"].includes(overview.value.runtime?.status)
    ? "primary"
    : overview.value.runtime?.status === "stopping"
      ? "warning"
      : "success",
)
const statusText = computed(() =>
  ["starting", "running"].includes(overview.value.runtime?.status)
    ? "正在运行"
    : overview.value.runtime?.status === "stopping"
      ? "正在停止"
      : "当前空闲",
)
const refreshedText = computed(() =>
  refreshedAt.value
    ? `更新于 ${new Date(refreshedAt.value).toLocaleTimeString("zh-CN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
    })}`
    : "",
)

function historyKey(item) {
  return [item.time, item.file_name, item.share_url].join("|");
}

async function loadOverview() {
  if (isCheckinDashboard.value || !props.api?.get || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    const result = await props.api.get("plugin/PanSearch/overview?include_runtime=false");
    if (!result?.success) throw new Error(result?.message || "获取数据失败");
    overview.value = {
      ...overview.value,
      ...(result.data || {}),
    }
    loaded.value = true;
    refreshedAt.value = Date.now();
  } catch (e) {
    error.value = e?.message || "获取数据失败";
  } finally {
    loading.value = false;
  }
}

function runtimeIsActive(value) {
  return (
    ["starting", "running", "stopping"].includes(value?.status) ||
    (value?.tasks || []).some((task) => ["queued", "running", "stopping", "postprocessing"].includes(task.status))
  )
}

function isPageVisible() {
  return document.visibilityState !== "hidden";
}

function clearRefreshTimer() {
  if (refreshTimer !== null) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function closeRuntimeStream() {
  if (runtimeStream) {
    runtimeStream.close();
    runtimeStream = null;
  }
}

function stopAutoRefresh() {
  clearRefreshTimer();
  closeRuntimeStream();
}

function scheduleFallbackRefresh(delay = refreshSeconds.value * 1000) {
  clearRefreshTimer();
  if (refreshSeconds.value <= 0 || !isPageVisible()) return;
  refreshTimer = window.setTimeout(
    async () => {
      refreshTimer = null;
      await loadOverview();
      scheduleFallbackRefresh();
    },
    Math.max(1000, Number(delay) || 0),
  )
}

function openRuntimeStream() {
  if (runtimeStreamDisabled || runtimeStream || refreshSeconds.value <= 0) return false;
  const source = connectRuntimeStream("PanSearch", {
    onOpen() {
      runtimeStreamFailures = 0;
      clearRefreshTimer();
    },
    onRuntime(nextRuntime) {
      const wasActive = runtimeIsActive(overview.value.runtime);
      overview.value = {...overview.value, runtime: nextRuntime};
      loaded.value = true;
      refreshedAt.value = Date.now();
      if (wasActive && !runtimeIsActive(nextRuntime)) void loadOverview();
    },
    onError() {
      runtimeStreamFailures += 1;
      if (runtimeStreamFailures < 2) return;
      closeRuntimeStream();
      runtimeStreamDisabled = true;
      scheduleFallbackRefresh(1000);
    },
  })
  if (!source) return false;
  runtimeStream = source;
  return true;
}

function startAutoRefresh() {
  stopAutoRefresh();
  if (isCheckinDashboard.value || refreshSeconds.value <= 0 || !isPageVisible()) return;
  if (!openRuntimeStream()) scheduleFallbackRefresh();
}

function handleVisibilityChange() {
  if (!isPageVisible()) {
    stopAutoRefresh();
    return;
  }
  runtimeStreamDisabled = false;
  runtimeStreamFailures = 0;
  void loadOverview().finally(startAutoRefresh);
}

watch(refreshSeconds, () => {
  if (isCheckinDashboard.value) return;
  runtimeStreamDisabled = false;
  runtimeStreamFailures = 0;
  startAutoRefresh();
})

onMounted(() => {
  if (isCheckinDashboard.value) return;
  document.addEventListener("visibilitychange", handleVisibilityChange);
  void loadOverview().finally(startAutoRefresh);
})

onUnmounted(() => {
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  stopAutoRefresh();
})
</script>

<style scoped>
.cloud-dashboard,
.dashboard-card {
  width: 100%;
  height: 100%;
  min-height: 0;
}

.dashboard-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dashboard-header {
  flex: 0 0 auto;
  padding-bottom: 8px;
}

.dashboard-title {
  overflow: hidden;
  font-size: 1rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dashboard-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding-top: 8px;
}

.dashboard-state {
  display: grid;
  min-height: 140px;
  place-items: center;
}

.runtime-line {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  padding: 8px 0 12px;
}

.runtime-mark {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: rgb(var(--v-theme-success));
}

.runtime-mark--primary {
  background: rgb(var(--v-theme-primary));
  box-shadow: 0 0 0 5px rgba(var(--v-theme-primary), 0.1);
}

.runtime-mark--warning {
  background: rgb(var(--v-theme-warning));
}

.runtime-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.runtime-copy strong,
.runtime-copy span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.runtime-copy strong {
  font-size: 0.875rem;
}

.runtime-copy span {
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.75rem;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-block: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.metric-grid > div {
  min-width: 0;
  padding: 12px 8px;
  text-align: center;
}

.metric-grid > div + div {
  border-left: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.metric-grid span,
.metric-grid strong {
  display: block;
}

.metric-grid span {
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.72rem;
}

.metric-grid strong {
  margin-top: 3px;
  font-size: 1.1rem;
}

.recent-list {
  padding-top: 8px;
}

.recent-list > div {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 7px;
  min-height: 30px;
}

.recent-list span,
.recent-list small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-list span {
  font-size: 0.8rem;
}

.recent-list small {
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-size: 0.7rem;
}

.dashboard-empty {
  padding: 20px 0 8px;
  text-align: center;
}

.dashboard-actions {
  flex: 0 0 auto;
  min-height: 40px;
  padding: 4px 12px;
}

@media (max-width: 360px) {
  .recent-list small {
    display: none;
  }
}
</style>
