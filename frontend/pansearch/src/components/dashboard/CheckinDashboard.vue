<template>
  <div class="checkin-dashboard">
    <v-card :flat="cardFlat" :loading="loading" class="checkin-dashboard-card">
      <v-card-item class="checkin-dashboard-header">
        <template #prepend>
          <v-avatar color="primary" variant="tonal" size="36">
            <v-icon icon="mdi-calendar-check-outline" size="20" />
          </v-avatar>
        </template>
        <v-card-title class="checkin-dashboard-title">{{ cardTitle }}</v-card-title>
        <v-card-subtitle>{{ cardSubtitle }}</v-card-subtitle>
        <template #append>
          <v-chip size="x-small" variant="tonal" :color="overallStatus.color">
            {{ overallStatus.label }}
          </v-chip>
        </template>
      </v-card-item>

      <v-card-text class="checkin-dashboard-body">
        <div v-if="loading && !loaded" class="checkin-dashboard-state">
          <v-progress-circular indeterminate color="primary" size="28" />
        </div>
        <v-alert v-else-if="error" type="error" variant="tonal" density="compact" class="text-caption">
          {{ error }}
        </v-alert>
        <template v-else-if="loaded">
          <div class="checkin-summary-line">
            <div>
              <strong>{{ summary.today_success || 0 }}/{{ summary.ready || 0 }}</strong>
              <span>今日签到</span>
            </div>
            <small>{{ scheduleText }}</small>
          </div>

          <div class="checkin-metric-grid">
            <div>
              <span>可用渠道</span>
              <strong>{{ summary.ready || 0 }}</strong>
            </div>
            <div>
              <span>今日成功</span>
              <strong class="text-success">{{ summary.today_success || 0 }}</strong>
            </div>
            <div>
              <span>今日积分</span>
              <strong :class="numberTone(summary.today_points)">
                {{ signedNumber(summary.today_points) }}
              </strong>
            </div>
            <div>
              <span>转盘净积分</span>
              <strong :class="numberTone(summary.lottery_net_points)">
                {{ signedNumber(summary.lottery_net_points) }}
              </strong>
            </div>
          </div>

          <div v-if="channels.length" class="checkin-channel-list">
            <div v-for="channel in channels" :key="channel.provider" class="checkin-channel">
              <div class="checkin-channel-head">
                <v-avatar size="28" variant="tonal" color="primary">
                  <v-icon :icon="providerIcon(channel.provider)" size="16" />
                </v-avatar>
                <div class="checkin-channel-copy">
                  <strong>{{ channel.provider_name }}</strong>
                  <span>{{ channelMeta(channel) }}</span>
                </div>
                <v-chip size="x-small" variant="tonal" :color="statusColor(channel.status?.tone)">
                  {{ channel.status?.label || "未知" }}
                </v-chip>
              </div>

              <div class="checkin-channel-detail">
                <span>{{ balanceText(channel) }}</span>
                <span>{{ pointsText(channel) }}</span>
                <span v-if="lotteryText(channel)">{{ lotteryText(channel) }}</span>
              </div>

              <div class="checkin-mini-timeline">
                <v-tooltip v-for="day in displayTimeline(channel)" :key="day.date" location="top">
                  <template #activator="{ props: tooltipProps }">
                    <span
                      v-bind="tooltipProps"
                      class="checkin-day-dot"
                      :class="`checkin-day-dot--${day.status || 'pending'}`" />
                  </template>
                  {{ day.date }} · {{ day.label }}
                </v-tooltip>
              </div>
            </div>
          </div>
          <div v-else class="checkin-dashboard-empty text-medium-emphasis">暂无签到渠道</div>
        </template>
      </v-card-text>

      <v-divider v-if="allowRefresh" />
      <v-card-actions v-if="allowRefresh" class="checkin-dashboard-actions">
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
const overview = ref({summary: {}, schedule: {}, channels: []});
let refreshTimer = null;

const attrs = computed(() => props.config?.attrs || {});
const cardTitle = computed(() => attrs.value.title || "签到概览");
const cardSubtitle = computed(() => attrs.value.subtitle || "多渠道每日签到与积分");
const cardFlat = computed(() => attrs.value.border === false);
const refreshSeconds = computed(() => {
  const value = Number(props.refreshInterval || attrs.value.refresh || 0);
  return Number.isFinite(value) ? value : 0;
})
const summary = computed(() => overview.value.summary || {});
const channels = computed(() => overview.value.channels || []);
const overallStatus = computed(() => {
  if (overview.value.running) return {label: "签到中", color: "primary"};
  if (!summary.value.ready) return {label: "未配置", color: "warning"};
  if (summary.value.today_failed) return {label: "有失败", color: "error"};
  if (summary.value.today_success >= summary.value.ready) {
    return {label: "今日完成", color: "success"};
  }
  return {label: "待签到", color: "warning"};
})
const scheduleText = computed(() => {
  const schedule = overview.value.schedule || {};
  if (!schedule.auto_retry) return "失败后不自动重试";
  return `失败自动重试 ${schedule.retry_count || 0} 次`;
})
const refreshedText = computed(() =>
  refreshedAt.value
    ? `更新于 ${new Date(refreshedAt.value).toLocaleTimeString("zh-CN", {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
    })}`
    : "",
)

function signedNumber(value) {
  const number = Number(value || 0);
  return `${number > 0 ? "+" : ""}${number}`;
}

function numberTone(value) {
  const number = Number(value || 0);
  if (number > 0) return "text-success";
  if (number < 0) return "text-error";
  return "text-medium-emphasis";
}

function statusColor(tone) {
  return (
    {
      success: "success",
      error: "error",
      warning: "warning",
      pending: "secondary",
      disabled: "secondary",
    }[tone] || "secondary"
  )
}

function providerIcon(provider) {
  return (
    {
      hdhive: "mdi-hexagon-multiple-outline",
      dian115: "mdi-cloud-search",
      juying: "mdi-movie-check-outline",
    }[provider] || "mdi-calendar-check-outline"
  )
}

function modeLabel(mode) {
  return {gambler: "赌狗签到", lucky: "运气签到"}[mode] || "普通签到";
}

function channelMeta(channel) {
  const executedAt = channel.today?.executed_at || channel.latest?.executed_at;
  if (!executedAt) return modeLabel(channel.mode);
  const date = new Date(executedAt);
  const time = Number.isNaN(date.getTime())
    ? String(executedAt)
    : date.toLocaleTimeString("zh-CN", {
        hour12: false,
        hour: "2-digit",
        minute: "2-digit",
    })
  return `${modeLabel(channel.mode)} · ${time}`;
}

function balanceText(channel) {
  const value = channel.today?.points_after ?? channel.latest?.points_after;
  return value === null || value === undefined ? "余额 —" : `余额 ${value}`;
}

function pointsText(channel) {
  const value = channel.today?.points_change;
  return value === null || value === undefined ? "今日积分 —" : `今日积分 ${signedNumber(value)}`;
}

function lotteryText(channel) {
  const record = channel.today;
  if (!record?.lottery_target_count) return "";
  return `转盘 ${record.lottery_executed || 0}/${record.lottery_target_count}`;
}

function displayTimeline(channel) {
  return [...(channel.timeline || [])].reverse();
}

async function loadOverview() {
  if (!props.api?.get || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    const result = await props.api.get("plugin/PanSearch/checkin/overview?days=7");
    if (!result?.success) throw new Error(result?.message || "获取签到数据失败");
    overview.value = result.data || {summary: {}, schedule: {}, channels: []};
    loaded.value = true;
    refreshedAt.value = Date.now();
  } catch (e) {
    error.value = e?.message || "获取签到数据失败";
  } finally {
    loading.value = false;
  }
}

function clearRefreshTimer() {
  if (refreshTimer !== null) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function scheduleRefresh() {
  clearRefreshTimer();
  if (refreshSeconds.value <= 0 || document.visibilityState === "hidden") return;
  refreshTimer = window.setTimeout(
    async () => {
      refreshTimer = null;
      await loadOverview();
      scheduleRefresh();
    },
    Math.max(1000, refreshSeconds.value * 1000),
  )
}

function handleVisibilityChange() {
  if (document.visibilityState === "hidden") {
    clearRefreshTimer();
    return;
  }
  void loadOverview().finally(scheduleRefresh);
}

watch(refreshSeconds, scheduleRefresh);

onMounted(() => {
  document.addEventListener("visibilitychange", handleVisibilityChange);
  void loadOverview().finally(scheduleRefresh);
})

onUnmounted(() => {
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  clearRefreshTimer();
})
</script>

<style scoped>
.checkin-dashboard,
.checkin-dashboard-card {
  width: 100%;
  height: 100%;
  min-height: 0;
}

.checkin-dashboard-card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.checkin-dashboard-header {
  flex: 0 0 auto;
  padding-bottom: 8px;
}

.checkin-dashboard-title {
  overflow: hidden;
  font-size: 1rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.checkin-dashboard-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding-top: 8px;
}

.checkin-dashboard-state {
  display: grid;
  min-height: 140px;
  place-items: center;
}

.checkin-summary-line {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0 12px;
}

.checkin-summary-line > div {
  display: flex;
  align-items: baseline;
  gap: 7px;
}

.checkin-summary-line strong {
  font-size: 1.45rem;
  line-height: 1;
}

.checkin-summary-line span,
.checkin-summary-line small {
  color: rgba(var(--v-theme-on-surface), 0.6);
  font-size: 0.72rem;
}

.checkin-metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-block: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.checkin-metric-grid > div {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  padding: 10px 5px;
  text-align: center;
}

.checkin-metric-grid > div + div {
  border-left: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.checkin-metric-grid span {
  overflow: hidden;
  color: rgba(var(--v-theme-on-surface), 0.58);
  font-size: 0.68rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.checkin-metric-grid strong {
  font-size: 1rem;
}

.checkin-channel-list {
  display: flex;
  flex-direction: column;
  padding-top: 6px;
}

.checkin-channel {
  padding: 10px 0;
}

.checkin-channel + .checkin-channel {
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.checkin-channel-head {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
}

.checkin-channel-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.checkin-channel-copy strong,
.checkin-channel-copy span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.checkin-channel-copy strong {
  font-size: 0.82rem;
}

.checkin-channel-copy span,
.checkin-channel-detail {
  color: rgba(var(--v-theme-on-surface), 0.58);
  font-size: 0.68rem;
}

.checkin-channel-detail {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  padding: 7px 0 6px 36px;
}

.checkin-mini-timeline {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 7px;
  padding-left: 36px;
}

.checkin-day-dot {
  width: 9px;
  height: 9px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.16);
  border-radius: 50%;
  background: rgba(var(--v-theme-on-surface), 0.08);
}

.checkin-day-dot--success,
.checkin-day-dot--already {
  border-color: rgba(var(--v-theme-success), 0.48);
  background: rgb(var(--v-theme-success));
}

.checkin-day-dot--failed {
  border-color: rgba(var(--v-theme-error), 0.48);
  background: rgb(var(--v-theme-error));
}

.checkin-day-dot--retry {
  border-color: rgba(var(--v-theme-warning), 0.52);
  background: rgb(var(--v-theme-warning));
}

.checkin-day-dot--pending {
  border-color: rgba(var(--v-theme-primary), 0.36);
  background: rgba(var(--v-theme-primary), 0.18);
}

.checkin-day-dot--disabled,
.checkin-day-dot--unconfigured {
  opacity: 0.36;
}

.checkin-dashboard-empty {
  display: grid;
  min-height: 120px;
  place-items: center;
  font-size: 0.78rem;
}

.checkin-dashboard-actions {
  flex: 0 0 auto;
  min-height: 40px;
  padding-block: 2px;
}

@media (max-width: 520px) {
  .checkin-metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .checkin-metric-grid > div:nth-child(3) {
    border-left: 0;
    border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  }

  .checkin-metric-grid > div:nth-child(4) {
    border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  }
}
</style>
