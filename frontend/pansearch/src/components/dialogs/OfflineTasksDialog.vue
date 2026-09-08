<template>
  <v-dialog
    :model-value="modelValue"
    max-width="820"
    content-class="offline-dialog-overlay"
    scrollable
    @update:model-value="emit('update:modelValue', $event)">
    <v-card class="offline-dialog">
      <v-card-title class="offline-header d-flex align-center ga-1 px-3 py-2 bg-primary-lighten-5">
        <div class="offline-heading">
          <v-icon icon="mdi-cloud-download-outline" class="mr-1" color="primary" size="small" />
          <span>{{ providerName }}离线任务</span>
          <v-chip size="x-small" variant="tonal" class="ml-1">
            {{ tasks.length }}
          </v-chip>
        </div>
        <div class="offline-header-actions">
          <v-btn v-if="tasks.length" variant="text" size="small" title="全选或取消全选" @click="toggleSelectAll">
            <v-icon :icon="allSelected ? 'mdi-checkbox-marked' : 'mdi-checkbox-blank-outline'" />
            <span class="offline-action-label">{{ allSelected ? "取消全选" : "全选" }}</span>
          </v-btn>
          <v-btn
            v-if="selectedRetryKeys.length"
            color="primary"
            variant="tonal"
            size="small"
            title="重试所选任务"
            :loading="retrying"
            @click="retryTasks(selectedRetryKeys)">
            <v-icon icon="mdi-reload" />
            <span class="offline-action-label">重试（{{ selectedRetryKeys.length }}）</span>
          </v-btn>
          <v-btn
            v-if="selectedKeys.length"
            color="error"
            variant="tonal"
            size="small"
            title="删除所选任务"
            @click="askDeleteSelected">
            <v-icon icon="mdi-delete-sweep-outline" />
            <span class="offline-action-label">删除（{{ selectedKeys.length }}）</span>
          </v-btn>
          <v-btn variant="text" size="small" title="刷新任务" :loading="loading" @click="refreshAll">
            <v-icon icon="mdi-refresh" />
            <span class="offline-action-label">刷新</span>
          </v-btn>
          <v-btn variant="text" size="small" title="关闭" @click="emit('update:modelValue', false)">
            <v-icon icon="mdi-close" />
            <span class="offline-action-label">关闭</span>
          </v-btn>
        </div>
      </v-card-title>

      <v-card-text class="offline-body pa-3">
        <v-sheet v-if="quota.total" class="quota-bar border rounded px-3 py-2 mb-3">
          <div class="d-flex align-center flex-wrap ga-2 text-caption">
            <v-icon icon="mdi-gauge" color="primary" size="small" />
            <span class="font-weight-medium">离线额度</span>
            <span>剩余 {{ quota.remaining }} / {{ quota.total }}</span>
            <span class="text-medium-emphasis">已用 {{ quota.used }}</span>
            <span v-if="quota.max_size_gb" class="text-medium-emphasis">单任务最大 {{ quota.max_size_gb }} GB</span>
          </div>
          <v-progress-linear :model-value="quotaPercent" color="primary" height="4" rounded class="mt-2" />
        </v-sheet>
        <div class="text-caption text-medium-emphasis mb-3">
          最近检查：{{ updatedText }} · 已下载文件最多等待系统处理30分钟
        </div>

        <v-alert
          v-if="error"
          type="error"
          variant="tonal"
          density="compact"
          closable
          class="mb-3"
          @click:close="error = ''">
          {{ error }}
        </v-alert>

        <v-alert
          v-if="success"
          type="success"
          variant="tonal"
          density="compact"
          closable
          class="mb-3"
          @click:close="success = ''">
          {{ success }}
        </v-alert>

        <v-skeleton-loader v-if="loading && !tasks.length" type="list-item-three-line@4" />

        <div v-else-if="!tasks.length" class="empty-state text-center text-medium-emphasis">
          <v-icon icon="mdi-cloud-download-outline" size="44" class="mb-2" />
          <div class="text-body-2">暂无离线任务</div>
        </div>

        <div v-else class="task-list">
          <v-sheet
            v-for="(task, index) in tasks"
            :key="task.id || `${task.name}-${index}`"
            class="task-item border"
            color="surface">
            <div class="task-row">
              <div class="task-leading">
                <v-checkbox-btn
                  v-if="taskSelectionKey(task)"
                  v-model="selectedKeys"
                  :value="taskSelectionKey(task)"
                  density="compact"
                  aria-label="选择离线任务" />
                <v-icon :icon="taskIcon(task)" :color="statusColor(task.state, task.failed)" size="20" />
              </div>
              <div class="task-copy">
                <div class="task-heading">
                  <div class="task-name text-body-2 font-weight-medium">
                    {{ task.target_name || task.name || "未命名任务" }}
                  </div>
                  <v-chip :color="statusColor(task.state, task.failed)" size="x-small" variant="tonal">
                    {{ taskStatusText(task) }}
                  </v-chip>
                  <span class="task-percent text-caption">
                    {{ formatPercent(progressValue(task)) }}
                  </span>
                </div>
                <v-progress-linear
                  :model-value="progressValue(task)"
                  :color="statusColor(task.state, task.failed)"
                  :stream="['queued', 'running', 'retrying', 'processing'].includes(task.state)"
                  :striped="['queued', 'running', 'retrying', 'processing'].includes(task.state)"
                  height="4"
                  rounded
                  class="my-1" />
                <div class="task-details text-caption text-medium-emphasis">
                  <span>{{ formatSize(task.size) }}</span>
                  <span v-if="task.cloud_dir" class="task-directory">
                    {{ task.cloud_dir }}
                  </span>
                  <span class="task-time">{{ formatTime(task.add_time) }}</span>
                </div>
              </div>
              <div class="task-actions">
                <v-btn
                  v-if="canRetry(task)"
                  color="primary"
                  variant="tonal"
                  size="x-small"
                  title="立即重试"
                  :loading="retryingKey === taskRetryKey(task)"
                  @click="retryTasks([taskRetryKey(task)])">
                  <v-icon icon="mdi-reload" />
                  <span class="task-action-label">立即重试</span>
                </v-btn>
                <v-btn
                  v-if="taskSelectionKey(task)"
                  color="error"
                  variant="text"
                  size="x-small"
                  title="删除任务"
                  @click="askDelete(task)">
                  <v-icon icon="mdi-delete-outline" />
                  <span class="task-action-label">删除任务</span>
                </v-btn>
              </div>
            </div>
          </v-sheet>
        </div>
      </v-card-text>
    </v-card>
  </v-dialog>

  <v-dialog v-model="confirmVisible" max-width="420" persistent>
    <v-card>
      <v-card-title class="text-subtitle-1">删除离线任务</v-card-title>
      <v-card-text>
        {{ deleteConfirmText }}
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" @click="confirmVisible = false">取消</v-btn>
        <v-btn color="error" :loading="deleting" @click="deleteTask">确认删除</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";

const props = defineProps({
  modelValue: Boolean,
  api: {type: [Object, Function], required: true},
})
const emit = defineEmits(["update:modelValue"]);
const pluginId = "PanSearch";
const tasks = ref([]);
const quota = ref({});
const providerName = ref("网盘");
const updatedAt = ref(0);
const loading = ref(false);
const deleting = ref(false);
const error = ref("");
const success = ref("");
const confirmVisible = ref(false);
const pendingTask = ref(null);
const selectedKeys = ref([]);
const retrying = ref(false);
const retryingKey = ref("");
const batchDeleting = ref(false);
let refreshTimer = null;
let loadRequest = null;

const updatedText = computed(() => (updatedAt.value ? new Date(updatedAt.value * 1000).toLocaleString() : "尚未检查"));
const selectableKeys = computed(() => tasks.value.map(taskSelectionKey).filter(Boolean));
const allSelected = computed(
  () => selectableKeys.value.length > 0 && selectableKeys.value.every((key) => selectedKeys.value.includes(key)),
)
const selectedTasks = computed(() => {
  const keys = new Set(selectedKeys.value);
  return tasks.value.filter((task) => keys.has(taskSelectionKey(task)));
})
const selectedHashes = computed(() => selectedTasks.value.map((task) => String(task.id || "").trim()).filter(Boolean));
const selectedPendingKeys = computed(() =>
  selectedTasks.value.map((task) => String(task.pending_key || "").trim()).filter(Boolean),
)
const selectedRetryKeys = computed(() =>
  selectedKeys.value.filter((key) => {
    const task = tasks.value.find((item) => taskSelectionKey(item) === key);
    return task && canRetry(task);
  }),
)
const quotaPercent = computed(() =>
  quota.value.total ? Math.min(100, Math.max(0, (Number(quota.value.used || 0) / Number(quota.value.total)) * 100)) : 0,
)
const deleteConfirmText = computed(() =>
  batchDeleting.value
    ? `确认删除所选 ${selectedKeys.value.length} 个任务？已下载文件会保留。`
    : `确认删除${
        pendingTask.value?.finalize_pending ? "后处理任务" : "离线任务"
    }“${pendingTask.value?.name || "未命名任务"}”？已下载文件会保留。`,
)

function unwrapResponse(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) {
    return raw.data;
  }
  return raw || {};
}

function formatSize(bytes) {
  let value = Number(bytes || 0);
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (value < 1024 || unit === "TB") return `${value.toFixed(1)} ${unit}`;
    value /= 1024;
  }
}

function formatPercent(value) {
  return `${Number(value || 0).toFixed(1)}%`;
}

function formatTime(timestamp) {
  return Number(timestamp) > 0 ? new Date(Number(timestamp) * 1000).toLocaleString() : "未知";
}

function statusColor(state, failed = false) {
  return failed ? "error" : state === "completed" ? "success" : state === "retrying" ? "warning" : "info";
}

function taskIcon(task) {
  if (task?.failed) return "mdi-alert-circle-outline";
  if (task?.finalize_pending) return "mdi-file-sync-outline";
  if (task?.state === "completed") return "mdi-check-circle-outline";
  return "mdi-drive-download-outline";
}

function taskStatusText(task) {
  if (task?.finalize_pending) return "待处理";
  return task?.status_text || "未知状态";
}

function progressValue(task) {
  if (task?.completed && !task?.failed) return 100;
  return Math.max(0, Math.min(100, Number(task?.percent || 0)));
}

async function load(force = false) {
  if (loadRequest) return loadRequest;
  stopAutoRefresh();
  loadRequest = (async () => {
    loading.value = true;
    error.value = "";
    try {
      const response = unwrapResponse(
        force
          ? await props.api.post(`plugin/${pluginId}/offline/refresh`)
          : await props.api.get(`plugin/${pluginId}/offline`),
      )
      if (response.success === false) {
        throw new Error(response.message || "加载失败");
      }
      const snapshot = response.data?.data || response.data || response;
      tasks.value = Array.isArray(snapshot) ? snapshot : Array.isArray(snapshot?.tasks) ? snapshot.tasks : [];
      updatedAt.value = Number(snapshot?.updated_at || 0);
      quota.value = snapshot?.quota && typeof snapshot.quota === "object" ? snapshot.quota : {};
      providerName.value = String(snapshot?.provider_name || "网盘");
      const availableKeys = new Set(tasks.value.map(taskSelectionKey).filter(Boolean));
      selectedKeys.value = selectedKeys.value.filter((key) => availableKeys.has(key));
    } catch (loadError) {
      error.value = loadError.message || "加载失败";
    } finally {
      loading.value = false;
      loadRequest = null;
      scheduleAutoRefresh();
    }
  })()
  return loadRequest;
}

async function refreshAll() {
  success.value = "";
  await load(true);
}

function hasPendingTasks() {
  return tasks.value.some((task) => task?.finalize_pending || (!task?.completed && !task?.failed));
}

function scheduleAutoRefresh() {
  stopAutoRefresh();
  if (!props.modelValue || document.visibilityState === "hidden") return;
  const delay = hasPendingTasks() ? 30000 : 120000;
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null;
    void load(false);
  }, delay)
}

function stopAutoRefresh() {
  if (refreshTimer === null) return;
  window.clearTimeout(refreshTimer);
  refreshTimer = null;
}

function handleVisibilityChange() {
  if (document.visibilityState === "hidden") {
    stopAutoRefresh();
  } else if (props.modelValue) {
    void load(false);
  }
}

async function retryTasks(pendingKeys) {
  const keys = [...new Set((pendingKeys || []).filter(Boolean))];
  if (!keys.length) return;
  retrying.value = keys.length > 1;
  retryingKey.value = keys.length === 1 ? keys[0] : "";
  error.value = "";
  try {
    const response = unwrapResponse(
      await props.api.post(`plugin/${pluginId}/offline/retry`, {
        pending_keys: keys.filter((key) => key.startsWith("pending:")).map((key) => key.slice(8)),
        task_ids: keys.filter((key) => key.startsWith("offline:")).map((key) => key.slice(8)),
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "重试失败");
    }
    await load(true);
  } catch (retryError) {
    error.value = retryError.message || "重试失败";
  } finally {
    retrying.value = false;
    retryingKey.value = "";
  }
}

function canRetry(task) {
  return Boolean(task?.failed || task?.finalize_pending);
}

function taskRetryKey(task) {
  if (task?.failed && task?.id) return `offline:${task.id}`;
  if (task?.finalize_pending && task?.pending_key) return `pending:${task.pending_key}`;
  return "";
}

function taskSelectionKey(task) {
  if (task?.id) return `offline:${task.id}`;
  if (task?.pending_key) return `pending:${task.pending_key}`;
  return "";
}

function toggleSelectAll() {
  selectedKeys.value = allSelected.value ? [] : [...selectableKeys.value];
}

function askDelete(task) {
  batchDeleting.value = false;
  pendingTask.value = task;
  confirmVisible.value = true;
}

function askDeleteSelected() {
  if (!selectedKeys.value.length) return;
  batchDeleting.value = true;
  pendingTask.value = null;
  confirmVisible.value = true;
}

async function deleteTask() {
  if (!pendingTask.value && !batchDeleting.value) return;
  deleting.value = true;
  success.value = "";
  try {
    const response = unwrapResponse(
      batchDeleting.value
        ? await props.api.post(`plugin/${pluginId}/offline/delete_batch`, {
          task_ids: selectedHashes.value,
          pending_keys: selectedPendingKeys.value,
        })
        : await props.api.post(`plugin/${pluginId}/offline/delete`, {
          task_id: pendingTask.value.id,
          pending_key: pendingTask.value.pending_key,
        }),
    )
    if (response.success === false) {
      throw new Error(response.message || "删除失败");
    }
    confirmVisible.value = false;
    pendingTask.value = null;
    selectedKeys.value = [];
    batchDeleting.value = false;
    await load(false);
    success.value = response.message || "任务已删除";
  } catch (deleteError) {
    error.value = deleteError.message || "删除失败";
  } finally {
    deleting.value = false;
  }
}

watch(
  () => props.modelValue,
  (value) => {
    if (value) {
      refreshAll();
    } else {
      stopAutoRefresh();
    }
  },
  {immediate: true},
)

onMounted(() => document.addEventListener("visibilitychange", handleVisibilityChange));
onBeforeUnmount(() => {
  document.removeEventListener("visibilitychange", handleVisibilityChange);
  stopAutoRefresh();
})
</script>

<style scoped>
.offline-dialog {
  max-height: 70vh;
}

.offline-body {
  min-height: 220px;
  max-height: 56vh;
  overflow-y: auto;
}

.offline-header {
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  width: 100%;
}

.offline-heading {
  display: flex;
  align-items: center;
  min-width: 0;
}

.offline-header-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  margin-left: auto;
}

.empty-state {
  padding: 64px 16px;
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.task-item {
  border-radius: 6px;
  padding: 8px 10px;
}

.task-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
}

.task-leading {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 4px;
}

.task-copy {
  min-width: 0;
}

.task-heading {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.task-name {
  min-width: 80px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-percent {
  min-width: 48px;
  text-align: right;
}

.task-heading :deep(.v-chip) {
  justify-self: end;
}

.task-details {
  display: flex;
  align-items: flex-end;
  flex-wrap: nowrap;
  gap: 6px 14px;
  line-height: 1.25;
}

.task-directory {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-time {
  flex: 0 0 auto;
  margin-left: auto;
  white-space: nowrap;
}

.task-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  flex-wrap: nowrap;
  white-space: nowrap;
}

@media (max-width: 600px) {
  :global(.offline-dialog-overlay) {
    width: calc(100vw - 24px) !important;
    max-width: 520px !important;
    margin: 12px !important;
  }

  .offline-dialog {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-height: 0;
    max-height: 86dvh;
    border-radius: 8px !important;
  }

  .offline-header {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 4px !important;
    padding: 8px 10px !important;
  }

  .offline-heading {
    display: flex;
    align-items: center;
    min-width: 0;
    min-height: 32px;
    white-space: nowrap;
  }

  .offline-heading > span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .offline-header-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 2px;
    margin-left: auto;
  }

  .offline-header-actions :deep(.v-btn) {
    min-width: 30px;
    width: 30px;
    height: 30px;
    padding: 0;
  }

  .offline-action-label {
    display: none;
  }

  .offline-body {
    flex: 1 1 auto;
    min-height: 0;
    max-height: none;
    padding: 8px 10px 10px !important;
  }

  .offline-body > .text-caption {
    margin-bottom: 8px !important;
    font-size: 0.7rem !important;
    line-height: 1.35;
  }

  .task-row {
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 4px;
  }

  .task-item {
    padding: 6px 8px;
  }

  .task-leading {
    gap: 0;
  }

  .task-leading > :deep(.v-icon) {
    display: none;
  }

  .task-leading :deep(.v-selection-control) {
    min-height: 28px;
  }

  .task-leading :deep(.v-selection-control__wrapper) {
    width: 28px;
    height: 28px;
  }

  .task-heading {
    grid-template-columns: minmax(0, 1fr) auto auto;
    gap: 4px;
  }

  .task-name {
    min-width: 0;
  }

  .task-details {
    flex-wrap: nowrap;
    gap: 8px;
    overflow: hidden;
    font-size: 0.7rem !important;
  }

  .task-directory {
    flex: 1 1 auto;
  }

  .task-actions {
    grid-column: 3;
    flex-direction: column;
    gap: 0;
    margin: 0;
  }

  .task-action-label {
    display: none;
  }

  .task-actions :deep(.v-btn) {
    min-width: 30px;
    width: 30px;
    height: 28px;
    padding: 0;
  }
}
</style>
