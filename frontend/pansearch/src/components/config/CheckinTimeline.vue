<template>
  <div class="checkin-timeline">
    <div class="checkin-matrix-scroll">
      <div class="checkin-matrix">
        <div class="checkin-matrix-head checkin-provider-cell">渠道</div>
        <div class="checkin-matrix-head checkin-summary-cell">签到状态</div>
        <div class="checkin-matrix-head checkin-summary-cell">签到天数</div>
        <div class="checkin-matrix-head checkin-summary-cell">当前积分</div>
        <div v-for="day in dateColumns" :key="'head-' + day.key" class="checkin-matrix-head">
          {{ day.label }}
        </div>

        <template v-for="provider in enabledProviders" :key="provider.key">
          <div class="checkin-provider-cell checkin-matrix-row">
            <v-icon :icon="provider.icon" size="18" color="primary" />
            <div class="checkin-provider-name">{{ provider.name }}</div>
          </div>

          <div class="checkin-summary-cell checkin-matrix-row">
            <span
              v-if="providerStatus(provider)"
              class="checkin-status-chip"
              :class="'checkin-status-chip--' + providerStatus(provider).tone">
              <v-icon :icon="providerStatus(provider).icon" size="13" />
              {{ providerStatus(provider).label }}
            </span>
          </div>
          <div class="checkin-summary-cell checkin-matrix-row">
            {{ latestMetric(provider, "signin_days", "天") }}
          </div>
          <div class="checkin-summary-cell checkin-matrix-row">
            {{ latestMetric(provider, "points_after") }}
          </div>

          <div
            v-for="day in timelineDays(provider)"
            :key="provider.key + '-' + day.key"
            class="checkin-day-cell checkin-matrix-row">
            <v-tooltip
              v-if="day.latest"
              location="top"
              :open-delay="180"
              max-width="260"
              content-class="checkin-status-tooltip">
              <template #activator="{ props: tooltipProps }">
                <span
                  v-bind="tooltipProps"
                  class="checkin-status-dot"
                  :class="[
                    'checkin-status-dot--' + dayNodeStatus(provider, day),
                    {
                      'checkin-status-dot--clickable': canCheckin(provider, day),
                    },
                  ]"
                  :aria-label="dayNodeLabel(provider, day)"
                  tabindex="0"
                  @click.stop="checkinFromDay(provider, day)"
                  @keydown.enter.prevent="checkinFromDay(provider, day)"
                  @keydown.space.prevent="checkinFromDay(provider, day)">
                  <v-icon :icon="dayNodeIcon(provider, day)" size="14" />
                </span>
              </template>

              <div class="checkin-tooltip">
                <div class="checkin-tooltip-head">
                  <span>{{ day.fullLabel }}</span>
                  <span :class="'checkin-tooltip-status--' + day.status">
                    {{ day.statusLabel }}
                  </span>
                </div>
                <div class="checkin-tooltip-meta">
                  {{ day.executionLabel }}
                  <span v-if="day.pointsDetail">· {{ day.pointsDetail }}</span>
                  <span v-if="day.lotteryDetail">· {{ day.lotteryDetail }}</span>
                </div>
                <div v-if="day.message" class="checkin-tooltip-message">
                  {{ day.message }}
                </div>
              </div>
            </v-tooltip>
            <span
              v-else
              class="checkin-status-dot"
              :class="[
                'checkin-status-dot--' + dayNodeStatus(provider, day),
                {
                  'checkin-status-dot--clickable': canCheckin(provider, day),
                },
              ]"
              :aria-label="dayNodeLabel(provider, day)"
              :role="canCheckin(provider, day) ? 'button' : undefined"
              :tabindex="canCheckin(provider, day) ? 0 : undefined"
              @click="checkinFromDay(provider, day)"
              @keydown.enter.prevent="checkinFromDay(provider, day)"
              @keydown.space.prevent="checkinFromDay(provider, day)">
              <v-icon :icon="dayNodeIcon(provider, day)" size="12" />
            </span>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import {computed, onMounted, reactive, ref, watch} from "vue";

const props = defineProps({
  api: { type: [Object, Function], required: true },
  providers: { type: Array, default: () => [] },
  config: { type: Object, required: true },
})
const emit = defineEmits(["result"])
const histories = reactive({})
const runningProvider = ref("")

const dateColumns = computed(() => buildDateColumns())
const enabledProviders = computed(() => props.providers.filter(providerEnabled));

function unwrapResponse(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) return raw.data
  return raw || {}
}

function localDateKey(value) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, "0")
  const day = String(value.getDate()).padStart(2, "0")
  return [year, month, day].join("-")
}

function buildDateColumns() {
  const today = new Date()
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(today)
    date.setHours(12, 0, 0, 0)
    date.setDate(today.getDate() - index)
    return {
      key: localDateKey(date),
      label: String(date.getMonth() + 1) + "/" + String(date.getDate()),
      fullLabel:
        date.toLocaleDateString("zh-CN", {
          month: "long",
          day: "numeric",
          weekday: "short",
        }) + (index === 0 ? " · 今天" : ""),
      isToday: index === 0,
    }
  })
}

function historyState(provider) {
  return histories[provider.key] || { total: 0, items: [], error: "" }
}

function providerEnabled(provider) {
  return Boolean(props.config[provider.enabledKey])
}

function providerConfigured(provider) {
  if (provider.key === "p115") {
    return Boolean(String(props.config.cookies || "").trim());
  }
  if (provider.key === "hdhive") {
    if (String(props.config.hdhive_query_mode || "web") === "api") {
      return Boolean(
        String(props.config.hdhive_api_key || "").trim() &&
        (String(props.config.hdhive_access_token || "").trim() ||
          String(props.config.hdhive_refresh_token || "").trim()),
      )
    }
  }
  return provider.credentialKeys.every((key) => Boolean(String(props.config[key] || "").trim()))
}

function providerMode(provider) {
  return String(props.config[provider.modeKey] || "normal")
}

function latestRecord(provider) {
  return historyState(provider).items[0] || null
}

function providerStatus(provider) {
  if (runningProvider.value === provider.key) {
    return { label: "签到中", tone: "running", icon: "mdi-loading" }
  }
  const today = dateColumns.value[0]?.key
  const record = historyState(provider).items.find((item) => {
    const parsed = new Date(item.executed_at)
    return !Number.isNaN(parsed.getTime()) && localDateKey(parsed) === today
  })
  if (!record) return null
  const already =
    record.success && [record.status, record.message].some((value) => String(value || "").includes("已签到"))
  if (already) {
    return { label: "已签到", tone: "already", icon: "mdi-check-circle" }
  }
  if (record.success) {
    return { label: "签到成功", tone: "success", icon: "mdi-check-circle" }
  }
  if (record.trigger === "scheduled" && props.config.checkin_auto_retry !== false) {
    return { label: "等待重试", tone: "retry", icon: "mdi-refresh" }
  }
  if (record.trigger === "retry") {
    return { label: "重试失败", tone: "error", icon: "mdi-alert-circle" }
  }
  return { label: "签到失败", tone: "error", icon: "mdi-alert-circle" }
}

function latestMetric(provider, key, suffix = "") {
  const record = latestRecord(provider)
  if (!record) return "—"
  const value = formatOptionalNumber(record[key], suffix)
  return value === "未记录" ? "—" : value
}

function canCheckin(provider, day) {
  return (
    day.isToday &&
    !["success", "already"].includes(day.status) &&
    !runningProvider.value &&
    providerEnabled(provider) &&
    providerConfigured(provider)
  )
}

function dayNodeStatus(provider, day) {
  if (day.isToday && runningProvider.value === provider.key) return "running"
  return day.status === "none" ? "empty" : day.status
}

function dayNodeIcon(provider, day) {
  if (day.isToday && runningProvider.value === provider.key) return "mdi-loading"
  if (day.isToday && day.status === "none" && canCheckin(provider, day)) return "mdi-calendar-plus"
  return day.icon
}

function dayNodeLabel(provider, day) {
  if (day.isToday && runningProvider.value === provider.key) return `${provider.name} 正在签到`
  return canCheckin(provider, day) ? `${day.ariaLabel}，点击立即签到` : day.ariaLabel
}

function checkinFromDay(provider, day) {
  if (canCheckin(provider, day)) requestCheckin(provider)
}

function timelineDays(provider) {
  const recordsByDay = new Map()
  for (const item of historyState(provider).items) {
    const parsed = new Date(item.executed_at)
    if (Number.isNaN(parsed.getTime())) continue
    const key = localDateKey(parsed)
    if (!recordsByDay.has(key)) recordsByDay.set(key, [])
    recordsByDay.get(key).push(item)
  }
  return dateColumns.value.map((date) => {
    const records = recordsByDay.get(date.key) || []
    const latest = records[0]
    const already = records.some(
      (item) => item.success && [item.status, item.message].some((value) => String(value || "").includes("已签到")),
    )
    const success = records.some((item) => item.success)
    const waitingRetry = latest?.trigger === "scheduled" && !latest.success && props.config.checkin_auto_retry !== false
    const status = already
      ? "already"
      : success
        ? "success"
        : waitingRetry
          ? "retry"
          : records.length
            ? "error"
            : "none"
    let statusLabel = {
      already: "已签到",
      success: "签到成功",
      retry: "等待重试",
      error: "签到失败",
      none: "未执行",
    }[status]
    if (latest?.trigger === "retry" && status !== "already") {
      statusLabel = status === "success" ? "重试成功" : "重试失败"
    }
    const pointChanges = records
      .filter((item) => item.points_change !== null && item.points_change !== undefined && item.points_change !== "")
      .map((item) => Number(item.points_change))
      .filter((value) => Number.isFinite(value))
    const pointsChange = pointChanges.length ? pointChanges.reduce((total, value) => total + value, 0) : null
    const pointsLabel = formatSignedNumber(pointsChange)
    const executedAt = latest ? formatTime(latest.executed_at) : ""
    const trigger =
      {
        scheduled: "定时执行",
        retry: "异常重试",
        manual: "手动执行",
      }[latest?.trigger || "manual"] || "手动执行"
    const executionLabel = latest
      ? [executedAt, trigger, records.length > 1 ? `${records.length} 次` : ""].filter(Boolean).join(" · ")
      : ""
    return {
      ...date,
      latest,
      status,
      statusLabel,
      icon: {
        already: "mdi-calendar-check",
        success: "mdi-check-bold",
        retry: "mdi-refresh",
        error: "mdi-alert-outline",
        none: "mdi-minus",
      }[status],
      pointsDetail: pointsChange === null ? "" : `积分 ${pointsLabel || "0"}`,
      lotteryDetail: latest?.lottery_target_count
        ? `转盘 ${latest.lottery_executed || 0}/${latest.lottery_target_count} 次`
        : "",
      executionLabel,
      message: latest && !latest.success ? latest.message || "" : "",
      ariaLabel: [date.fullLabel, statusLabel, pointsLabel].filter(Boolean).join("，"),
    }
  })
}

function formatTime(value) {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return "时间未知"
  return parsed.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  })
}

function formatSignedNumber(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return ""
  const normalized = Number(value)
  return normalized > 0 ? `+${normalized}` : String(normalized)
}

function formatOptionalNumber(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "未记录"
  const normalized = Number(value)
  return Number.isFinite(normalized) ? `${normalized}${suffix}` : "未记录"
}

async function loadProviderHistory(provider) {
  try {
    const response = unwrapResponse(
      await props.api.get("plugin/PanSearch/checkin/" + encodeURIComponent(provider.key) + "/history?limit=60"),
    )
    if (response.success === false) throw new Error(response.message || "读取签到记录失败")
    const data = response.data?.data || response.data || {}
    histories[provider.key] = {
      total: Number(data.total || 0),
      items: Array.isArray(data.items) ? data.items : [],
      error: "",
    }
  } catch (error) {
    histories[provider.key] = {
      total: 0,
      items: [],
      error: error.message || String(error),
    }
  }
}

async function loadHistories() {
  await Promise.all(enabledProviders.value.map(loadProviderHistory));
}

function requestCheckin(provider) {
  if (runningProvider.value) return
  runCheckin(provider)
}

function wait(delay) {
  return new Promise((resolve) => window.setTimeout(resolve, delay))
}

async function waitForProviderResult(provider, previousRecordId) {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    await wait(attempt === 0 ? 500 : 2000)
    await loadProviderHistory(provider)
    const record = latestRecord(provider)
    if (record?.id && record.id !== previousRecordId) return record
  }
  return null
}

async function runCheckin(provider) {
  if (runningProvider.value) return
  runningProvider.value = provider.key
  const previousRecordId = latestRecord(provider)?.id || ""
  try {
    const response = unwrapResponse(
      await props.api.post("plugin/PanSearch/checkin/" + encodeURIComponent(provider.key), {
        mode: providerMode(provider),
      }),
    )
    let success = response.success !== false
    let message = response.message || (success ? "签到完成" : "签到失败")
    if (success && response.data?.running) {
      const record = await waitForProviderResult(provider, previousRecordId)
      if (record) {
        success = Boolean(record.success)
        message = record.message || record.status || message
      } else {
        message = "签到任务已提交，仍在后台执行"
      }
    }
    emit("result", {
      providerKey: provider.key,
      providerName: provider.name,
      success,
      message,
    })
  } catch (error) {
    emit("result", {
      providerKey: provider.key,
      providerName: provider.name,
      success: false,
      message: error.message || String(error),
    })
  } finally {
    await loadProviderHistory(provider)
    runningProvider.value = ""
  }
}

watch(() => props.providers.map((provider) => `${provider.key}:${providerEnabled(provider)}`).join(","), loadHistories);
onMounted(loadHistories)
</script>

<style scoped>
.checkin-timeline {
  min-width: 0;
}

.checkin-matrix-scroll {
  overflow-x: auto;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 10px;
  background: rgb(var(--v-theme-surface));
}

.checkin-matrix {
  min-width: 800px;
  display: grid;
  grid-template-columns: 156px 108px 78px 86px repeat(7, minmax(52px, 1fr));
  align-items: stretch;
}

.checkin-matrix-head {
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.75rem;
  font-weight: 600;
}

.checkin-matrix-row {
  min-height: 48px;
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.06);
}

.checkin-provider-cell {
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 6px;
  padding: 0 10px;
}

.checkin-summary-cell {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 6px;
  color: rgba(var(--v-theme-on-surface), 0.76);
  font-size: 0.72rem;
  white-space: nowrap;
}

.checkin-provider-name {
  overflow: hidden;
  font-size: 0.82rem;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.checkin-status-chip {
  height: 24px;
  padding: 0 9px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 600;
  line-height: 1;
}

.checkin-status-chip--success {
  background: rgba(var(--v-theme-success), 0.12);
  color: rgb(var(--v-theme-success));
}

.checkin-status-chip--already {
  background: rgba(var(--v-theme-primary), 0.1);
  color: rgb(var(--v-theme-primary));
}

.checkin-status-chip--retry {
  background: rgba(var(--v-theme-warning), 0.14);
  color: rgb(var(--v-theme-warning));
}

.checkin-status-chip--error {
  background: rgba(var(--v-theme-error), 0.1);
  color: rgb(var(--v-theme-error));
}

.checkin-status-chip--running {
  background: rgba(var(--v-theme-primary), 0.1);
  color: rgb(var(--v-theme-primary));
}

.checkin-status-chip--running .v-icon {
  animation: checkin-status-spin 0.8s linear infinite;
}

.checkin-day-cell {
  display: flex;
  align-items: center;
  justify-content: center;
}

.checkin-status-dot {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 999px;
  cursor: help;
  outline: none;
  transition:
    color 0.16s ease,
    border-color 0.16s ease,
    background-color 0.16s ease,
    transform 0.16s ease;
}

.checkin-status-dot:focus-visible {
  box-shadow: 0 0 0 3px rgba(var(--v-theme-primary), 0.22);
}

.checkin-tooltip {
  min-width: 200px;
  padding: 2px;
}

.checkin-tooltip-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 5px;
  font-size: 0.78rem;
  font-weight: 700;
}

.checkin-tooltip-status--success {
  color: rgb(var(--v-theme-success));
}

.checkin-tooltip-status--already {
  color: rgb(var(--v-theme-primary));
}

.checkin-tooltip-status--retry {
  color: rgb(var(--v-theme-warning));
}

.checkin-tooltip-status--error {
  color: rgb(var(--v-theme-error));
}

.checkin-tooltip-meta {
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.7rem;
}

.checkin-tooltip-message {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.14);
  color: rgba(var(--v-theme-on-surface), 0.78);
  font-size: 0.7rem;
  line-height: 1.4;
}

.checkin-status-dot--success {
  color: rgb(var(--v-theme-success));
  border-color: rgba(var(--v-theme-success), 0.42);
  background: rgba(var(--v-theme-success), 0.12);
}

.checkin-status-dot--already {
  color: rgb(var(--v-theme-primary));
  border-color: rgba(var(--v-theme-primary), 0.42);
  background: rgba(var(--v-theme-primary), 0.1);
}

.checkin-status-dot--error {
  color: rgb(var(--v-theme-error));
  border-color: rgba(var(--v-theme-error), 0.44);
  background: rgba(var(--v-theme-error), 0.1);
}

.checkin-status-dot--retry {
  color: rgb(var(--v-theme-warning));
  border-color: rgba(var(--v-theme-warning), 0.48);
  background: rgba(var(--v-theme-warning), 0.12);
}

.checkin-status-dot--empty {
  cursor: default;
  color: rgba(var(--v-theme-on-surface), 0.38);
  border-color: rgba(var(--v-theme-on-surface), 0.16);
  background: rgba(var(--v-theme-on-surface), 0.04);
}

.checkin-status-dot--clickable {
  cursor: pointer;
}

.checkin-status-dot--clickable:hover {
  border-color: rgba(var(--v-theme-primary), 0.5);
  background: rgba(var(--v-theme-primary), 0.1);
  color: rgb(var(--v-theme-primary));
  transform: scale(1.08);
}

.checkin-status-dot--running {
  cursor: wait;
  border-color: rgba(var(--v-theme-primary), 0.46);
  background: rgba(var(--v-theme-primary), 0.1);
  color: rgb(var(--v-theme-primary));
}

.checkin-status-dot--running .v-icon {
  animation: checkin-status-spin 0.8s linear infinite;
}

@keyframes checkin-status-spin {
  to {
    transform: rotate(360deg);
  }
}

:global(.checkin-status-tooltip) {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  background: rgb(var(--v-theme-surface)) !important;
  box-shadow: 0 6px 20px rgba(var(--v-theme-on-surface), 0.14) !important;
  color: rgb(var(--v-theme-on-surface)) !important;
  opacity: 1 !important;
}
</style>
