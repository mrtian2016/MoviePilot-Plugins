<template>
  <div class="runtime-panel">
    <div v-if="tasks.length" class="task-list">
      <div v-for="task in tasks" :key="task.id" class="task-row">
        <v-icon
          :icon="
            task.task_kind === 'cross_transfer'
              ? 'mdi-swap-horizontal-bold'
              : task.media_type === '电影'
                ? 'mdi-movie-outline'
                : 'mdi-television-classic'
          "
          :color="taskColor(task.status)"
          size="small" />
        <div class="task-content">
          <div class="task-line">
            <span class="task-name text-body-2 font-weight-medium">{{ task.title }}</span>
            <span v-if="task.season" class="text-caption text-medium-emphasis">
              S{{ String(task.season).padStart(2, "0") }}
            </span>
            <v-chip
              v-if="task.task_kind === 'pt_upgrade' || task.task_kind === 'cloud_upgrade'"
              size="x-small"
              variant="outlined"
              :color="task.task_kind === 'pt_upgrade' ? 'warning' : 'primary'">
              {{ task.task_kind === "pt_upgrade" ? "PT 洗版" : "网盘洗版" }}
            </v-chip>
            <v-chip :color="taskColor(task.status)" size="x-small" variant="tonal">
              {{ taskStatus(task.status) }}
            </v-chip>
          </div>
          <div class="task-meta">
            <div class="task-phase-wrap">
              <span class="task-phase text-caption text-medium-emphasis">
                {{
                  task.postprocess_active || task.status === "postprocessing"
                    ? postprocessingSummary(task)
                    : task.task_kind === "cross_transfer"
                      ? task.error || task.message || task.phase
                      : task.status === "failed"
                        ? task.message || task.phase || "处理失败"
                        : task.phase || "等待调度"
                }}
              </span>
              <v-btn
                v-if="hasPostprocessDetails(task)"
                class="task-detail-toggle"
                :icon="isTaskExpanded(task.id) ? 'mdi-chevron-up' : 'mdi-information-outline'"
                variant="text"
                size="x-small"
                :title="isTaskExpanded(task.id) ? '收起后处理详情' : '查看后处理详情'"
                :aria-label="isTaskExpanded(task.id) ? '收起后处理详情' : '查看后处理详情'"
                @click="toggleTaskDetails(task.id)" />
            </div>
            <span
              v-if="
                (task.transfer_active || ['pt_upgrade', 'cross_transfer'].includes(task.task_kind)) &&
                displayTotal(task) > 0
              "
              class="task-transfer text-caption text-medium-emphasis">
              {{ formatSize(displayTransferred(task)) }} / {{ formatSize(displayTotal(task)) }} ·
              {{ formatSpeed(task.speed_bytes_per_second || task.upload_speed) }}
            </span>
            <span v-else-if="task.postprocess_active" class="task-transfer text-caption text-medium-emphasis">
              {{ Math.round(taskProgress(task)) }}%
            </span>
          </div>
          <v-progress-linear
            :class="[
              'task-progress',
              { 'task-progress--active': task.postprocess_active || task.status === 'postprocessing' },
            ]"
            :model-value="taskProgress(task)"
            :style="progressStyle(task)"
            :indeterminate="
              task.postprocess_active || task.status === 'postprocessing'
                ? !task.postprocess_active
                : !task.transfer_active &&
                  !['pt_upgrade', 'cross_transfer'].includes(task.task_kind) &&
                  ['running', 'stopping'].includes(task.status)
            "
            :color="taskColor(task.status)"
            height="5"
            rounded />
          <v-expand-transition>
            <div v-if="hasPostprocessDetails(task) && isTaskExpanded(task.id)" class="task-details text-caption">
              <div v-if="task.current_file" class="task-detail-row">
                <span class="task-detail-label">当前文件</span>
                <span class="task-current-file" :title="task.current_file">{{ task.current_file }}</span>
              </div>
              <div v-if="Number(task.postprocess_file_total || 0) > 0" class="task-detail-row">
                <span class="task-detail-label">文件进度</span>
                <span>
                  第 {{ Number(task.postprocess_file_index || 1) }} / {{ Number(task.postprocess_file_total || 1) }} 个
                </span>
              </div>
              <div v-if="postprocessSteps(task).length" class="task-detail-row task-process-row">
                <span class="task-detail-label">处理过程</span>
                <div class="task-process">
                  <div
                    v-for="(step, index) in postprocessSteps(task)"
                    :key="step.key"
                    class="task-process-step"
                    :class="`task-process-step--${postprocessStepState(task, index)}`">
                    <v-icon :icon="postprocessStepIcon(task, index)" size="14" />
                    <span>{{ step.label }}</span>
                  </div>
                </div>
              </div>
            </div>
          </v-expand-transition>
        </div>
        <v-btn
          v-if="canStop(task)"
          icon="mdi-stop-circle-outline"
          color="warning"
          variant="text"
          size="x-small"
          :loading="task.status === 'stopping'"
          title="停止此任务"
          @click="emit('stop-task', task.id)" />
        <v-icon v-else :icon="resultIcon(task.status, task.task_kind)" :color="taskColor(task.status)" size="small" />
      </div>
    </div>
    <div v-else class="idle-state text-medium-emphasis">
      <v-progress-circular v-if="active" indeterminate size="44" width="3" color="primary" />
      <v-icon v-else icon="mdi-check-circle-outline" color="success" size="44" />
      <div class="text-subtitle-2 font-weight-medium mt-3">
        {{ active ? runtime.task || "正在准备订阅任务" : "当前没有订阅任务" }}
      </div>
      <div class="text-caption mt-1">
        {{ active ? "正在加载订阅任务列表" : runtime.task || "等待下一次订阅搜索" }}
      </div>
    </div>
  </div>
</template>
<script setup>
import {computed, ref} from "vue";

const props = defineProps({
  runtime: {type: Object, required: true},
  active: Boolean,
})
const emit = defineEmits(["stop-task"]);
const active = computed(() => props.active);
const expandedTaskIds = ref(new Set());
const tasks = computed(() =>
  (props.runtime.tasks || []).filter(
    (task) =>
      task.task_kind === "cross_transfer" || ["queued", "running", "stopping", "postprocessing"].includes(task.status),
  ),
)

function postprocessingSummary(task) {
  if (task?.postprocess_active) {
    return currentPostprocessStep(task)?.label || "正在处理文件";
  }
  const pendingCount = Number(task?.pending_count || 0);
  return pendingCount > 0 ? `${pendingCount} 个文件待完成后处理` : "正在完成文件后处理";
}

function hasPostprocessDetails(task) {
  return Boolean(
    task?.postprocess_active ||
    task?.current_file ||
    Number(task?.postprocess_file_total || 0) > 0 ||
    postprocessSteps(task).length,
  )
}

function postprocessSteps(task) {
  return Array.isArray(task?.postprocess_steps) ? task.postprocess_steps.filter((step) => step?.key && step?.label) : [];
}

function currentPostprocessStep(task) {
  const steps = postprocessSteps(task);
  const currentKey = String(task?.postprocess_step || "");
  return steps.find((step) => step.key === currentKey) || null;
}

function postprocessStepState(task, index) {
  const currentIndex = Math.max(0, Number(task?.postprocess_step_index || 0));
  if (index < currentIndex) return "completed";
  if (index === currentIndex) return "active";
  return "pending";
}

function postprocessStepIcon(task, index) {
  const state = postprocessStepState(task, index);
  if (state === "completed") return "mdi-check-circle";
  if (state === "active") return "mdi-progress-clock";
  return "mdi-circle-outline";
}

function isTaskExpanded(taskId) {
  return expandedTaskIds.value.has(taskId);
}

function toggleTaskDetails(taskId) {
  const nextIds = new Set(expandedTaskIds.value);
  if (nextIds.has(taskId)) {
    nextIds.delete(taskId);
  } else {
    nextIds.add(taskId);
  }
  expandedTaskIds.value = nextIds;
}

function canStop(task) {
  return ["queued", "running", "stopping", "postprocessing"].includes(task?.status);
}

function taskStatus(status) {
  return (
    {
      queued: "排队中",
      running: "运行中",
      stopping: "停止中",
      postprocessing: "后处理中",
      completed: "完成",
      success: "完成",
      failed: "失败",
      stopped: "已停止",
      canceled: "已取消",
    }[status] || "未知"
  )
}

function taskColor(status) {
  return (
    {
      queued: "secondary",
      running: "info",
      stopping: "warning",
      postprocessing: "primary",
      completed: "success",
      success: "success",
      failed: "error",
      stopped: "warning",
      canceled: "warning",
    }[status] || "secondary"
  )
}

const progressColors = [
  {progress: 0, color: [66, 165, 245]},
  {progress: 35, color: [38, 198, 218]},
  {progress: 65, color: [255, 179, 0]},
  {progress: 85, color: [102, 187, 106]},
  {progress: 100, color: [46, 125, 50]},
]

function progressColor(progress) {
  const value = Math.max(0, Math.min(100, Number(progress || 0)));
  const upperIndex = progressColors.findIndex((item) => value <= item.progress);
  if (upperIndex <= 0) return `rgb(${progressColors[0].color.join(", ")})`;
  const lower = progressColors[upperIndex - 1];
  const upper = progressColors[upperIndex];
  const ratio = (value - lower.progress) / (upper.progress - lower.progress);
  const color = lower.color.map((channel, index) => Math.round(channel + (upper.color[index] - channel) * ratio));
  return `rgb(${color.join(", ")})`;
}

function progressStyle(task) {
  if (["failed"].includes(task?.status)) {
    return {"--task-progress-gradient": "linear-gradient(90deg, #ff8a80, #d32f2f)"};
  }
  if (["stopping", "stopped", "canceled"].includes(task?.status)) {
    return {"--task-progress-gradient": "linear-gradient(90deg, #ffd54f, #fb8c00)"};
  }
  const progress = taskProgress(task);
  return {
    "--task-progress-gradient": `linear-gradient(90deg, ${progressColor(progress)}, ${progressColor(Math.min(100, progress + 18))})`,
  }
}

function taskProgress(task) {
  const value =
    task?.postprocess_active || task?.status === "postprocessing"
      ? Number(task?.postprocess_progress || 0)
      : Number(task?.progress || 0)
  return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
}

function resultIcon(status, taskKind) {
  if (taskKind === "cross_transfer" && status === "running") {
    return "mdi-cloud-upload-outline";
  }
  if (taskKind === "pt_upgrade" && status === "running") {
    return "mdi-cloud-upload-outline";
  }
  return status === "postprocessing"
    ? "mdi-cog-sync-outline"
    : ["completed", "success"].includes(status)
      ? "mdi-check-circle"
      : status === "failed"
        ? "mdi-alert-circle"
        : "mdi-stop-circle"
}

function formatSize(value) {
  let size = Math.max(0, Number(value || 0));
  const units = ["B", "KB", "MB", "GB", "TB"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatSpeed(value) {
  return `${formatSize(value)}/s`;
}

function displayTransferred(task) {
  return Number(task?.stage_total || 0) > 0 ? Number(task?.stage_transferred || 0) : Number(task?.transferred || 0);
}

function displayTotal(task) {
  return Number(task?.stage_total || 0) > 0 ? Number(task?.stage_total || 0) : Number(task?.total || 0);
}
</script>

<style scoped>
.runtime-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: rgb(var(--v-theme-surface));
}

.runtime-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
}

.min-width-0,
.task-content {
  min-width: 0;
}

.task-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.task-row {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) 30px;
  align-items: center;
  gap: 8px;
  min-height: 54px;
  padding: 7px 12px;
}

.task-row + .task-row {
  border-top: 1px solid rgba(var(--v-border-color), 0.08);
}

.task-line,
.task-meta {
  display: flex;
  align-items: center;
  gap: 7px;
}

.task-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.runtime-header > .d-flex {
  min-width: 0;
}

.task-meta {
  margin-top: 4px;
  justify-content: space-between;
}

.task-phase {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-phase-wrap {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 2px;
}

.task-detail-toggle {
  flex: 0 0 auto;
}

.task-details {
  display: grid;
  gap: 4px;
  margin-top: 7px;
  padding: 7px 0 1px;
  border-top: 1px dashed rgba(var(--v-border-color), 0.18);
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
}

.task-detail-row {
  display: grid;
  grid-template-columns: 56px minmax(0, 1fr);
  gap: 8px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.task-detail-label {
  color: rgba(var(--v-theme-on-surface), var(--v-high-emphasis-opacity));
  font-weight: 500;
}

.task-current-file {
  min-width: 0;
  overflow-wrap: anywhere;
}

.task-process-row {
  align-items: start;
}

.task-process {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 5px 12px;
}

.task-process-step {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: rgba(var(--v-theme-on-surface), var(--v-disabled-opacity));
  white-space: nowrap;
}

.task-process-step--completed {
  color: rgb(var(--v-theme-success));
}

.task-process-step--active {
  color: rgb(var(--v-theme-primary));
  font-weight: 500;
}

.task-transfer {
  flex: 0 0 auto;
  white-space: nowrap;
}

.task-progress {
  margin-top: 5px;
}

.task-progress :deep(.v-progress-linear__determinate) {
  background: var(--task-progress-gradient) !important;
  transition: width 0.35s ease,
  background 0.35s ease;
}

.task-progress--active :deep(.v-progress-linear__determinate) {
  background-image: linear-gradient(110deg, transparent 22%, rgba(255, 255, 255, 0.72) 48%, transparent 72%),
  var(--task-progress-gradient) !important;
  background-position: -120px 0,
  0 0;
  background-size: 120px 100%,
  100% 100%;
  background-repeat: no-repeat;
  animation: task-progress-shimmer 1.15s linear infinite;
}

@keyframes task-progress-shimmer {
  to {
    background-position: 120px 0,
    0 0;
  }
}

.idle-state {
  display: flex;
  flex: 1 1 auto;
  min-height: 220px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
}

@media (max-width: 600px) {
  .runtime-header > .d-flex {
    width: 100%;
    white-space: nowrap;
  }

  .task-line {
    flex-wrap: wrap;
  }

  .task-meta {
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
  }
}
</style>
