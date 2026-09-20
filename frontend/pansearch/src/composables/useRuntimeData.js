import {computed, onMounted, onUnmounted, reactive, ref} from "vue";
import {connectRuntimeStream} from "../utils/runtimeStream.js";

export function useRuntimeData(api, notify, pluginId = "PanSearch", {onSettled, onHistoryChanged} = {}) {
  const offlineSupported = ref(false);
  const runtime = reactive({
    status: "idle",
    task: "当前没有订阅处理任务",
    progress: 0,
    tasks: [],
  })
  const active = computed(
    () =>
      ["starting", "running", "stopping"].includes(runtime.status) ||
      (runtime.tasks || []).some((task) => ["queued", "running", "stopping"].includes(task.status)),
  )

  let runtimeRequest = null;
  let runtimeFallbackTimer = null;
  let runtimeStream = null;
  let runtimeStreamFailures = 0;
  let runtimeStreamDisabled = false;
  let startupGuardTimer = null;
  let runtimeRevision = -1;
  let historyRevision = null;
  let startRequestedUntil = 0;

  function hasActiveRuntime() {
    return (
      Date.now() < startRequestedUntil ||
      ["starting", "running", "stopping"].includes(runtime.status) ||
      (runtime.tasks || []).some((task) => ["queued", "running", "stopping", "postprocessing"].includes(task.status))
    )
  }

  function isPageVisible() {
    return typeof document === "undefined" || document.visibilityState !== "hidden";
  }

  function clearRuntimeFallback() {
    if (runtimeFallbackTimer !== null) {
      window.clearTimeout(runtimeFallbackTimer);
      runtimeFallbackTimer = null;
    }
  }

  function closeRuntimeStream() {
    if (runtimeStream) {
      runtimeStream.close();
      runtimeStream = null;
    }
  }

  function clearStartupGuard() {
    if (startupGuardTimer !== null) {
      window.clearTimeout(startupGuardTimer);
      startupGuardTimer = null;
    }
  }

  function pauseRuntimeUpdates() {
    clearRuntimeFallback();
    closeRuntimeStream();
  }

  function stopRuntimeUpdates() {
    pauseRuntimeUpdates();
    clearStartupGuard();
    runtimeStreamFailures = 0;
    runtimeStreamDisabled = false;
  }

  function scheduleRuntimeFallback(delay = hasActiveRuntime() ? 5000 : 60000) {
    clearRuntimeFallback();
    if (!isPageVisible()) return;
    runtimeFallbackTimer = window.setTimeout(
      async () => {
        runtimeFallbackTimer = null;
        await loadRuntime();
        if (!runtimeStream) scheduleRuntimeFallback();
      },
      Math.max(1000, Number(delay) || 0),
    )
  }

  function notifySettled() {
    if (typeof onSettled !== "function") return;
    try {
      onSettled();
    } catch (_) {
    }
  }

  function applyRuntimeSnapshot(nextRuntime) {
    if (!nextRuntime || typeof nextRuntime !== "object") return;
    const nextRevision = Number(nextRuntime.revision);
    if (Number.isFinite(nextRevision)) {
      if (nextRevision < runtimeRevision) return;
      runtimeRevision = nextRevision;
    }
    const nextHistoryRevision = Number(nextRuntime.history_revision);
    const historyChanged =
      historyRevision !== null && Number.isFinite(nextHistoryRevision) && nextHistoryRevision > historyRevision
    if (Number.isFinite(nextHistoryRevision)) {
      historyRevision = historyRevision === null ? nextHistoryRevision : Math.max(historyRevision, nextHistoryRevision);
    }
    const wasActive = hasActiveRuntime();
    if ("offline_supported" in nextRuntime) {
      offlineSupported.value = Boolean(nextRuntime.offline_supported);
    }
    if (nextRuntime.status === "idle" && Date.now() < startRequestedUntil) {
      Object.assign(runtime, {
        ...nextRuntime,
        status: "starting",
        task: "正在准备订阅任务",
      })
    } else {
      Object.assign(runtime, nextRuntime);
      if (nextRuntime.status === "running") {
        startRequestedUntil = 0;
        clearStartupGuard();
      }
    }
    const isActive = hasActiveRuntime();
    if (historyChanged && typeof onHistoryChanged === "function") {
      try {
        onHistoryChanged(nextHistoryRevision);
      } catch (_) {
      }
    }
    if (wasActive && !isActive) notifySettled();
    ensureRuntimeUpdates();
  }

  function openRuntimeStream() {
    if (runtimeStreamDisabled || runtimeStream || !isPageVisible()) return false;
    const source = connectRuntimeStream(pluginId, {
      onOpen() {
        runtimeStreamFailures = 0;
        clearRuntimeFallback();
      },
      onRuntime: applyRuntimeSnapshot,
      onError() {
        runtimeStreamFailures += 1;
        if (runtimeStreamFailures < 2) return;
        closeRuntimeStream();
        runtimeStreamDisabled = true;
        scheduleRuntimeFallback(1000);
      },
    })
    if (!source) return false;
    runtimeStream = source;
    return true;
  }

  function ensureRuntimeUpdates() {
    if (!isPageVisible()) {
      pauseRuntimeUpdates();
      return;
    }
    if (runtimeStream || runtimeFallbackTimer !== null) return;
    if (!openRuntimeStream()) scheduleRuntimeFallback(1000);
  }

  function armStartupGuard() {
    clearStartupGuard();
    startupGuardTimer = window.setTimeout(
      async () => {
        startupGuardTimer = null;
        await loadRuntime();
        ensureRuntimeUpdates();
      },
      Math.max(0, startRequestedUntil - Date.now() + 100),
    )
  }

  function handleVisibilityChange() {
    if (!isPageVisible()) {
      pauseRuntimeUpdates();
      return;
    }
    runtimeStreamDisabled = false;
    runtimeStreamFailures = 0;
    void loadRuntime().finally(ensureRuntimeUpdates);
  }

  async function loadRuntime() {
    if (runtimeRequest) return runtimeRequest;
    runtimeRequest = (async () => {
      try {
        const result = await api.get(`plugin/${pluginId}/runtime`);
        if (result?.success) applyRuntimeSnapshot(result.data || {});
      } catch (_) {
      } finally {
        runtimeRequest = null;
      }
    })()
    return runtimeRequest;
  }

  async function startSync(selection = null) {
    try {
      const selectedCount = Math.max(0, Number(selection?.groupCount || 0));
      const payload = selectedCount
        ? {
            selected_count: selectedCount,
            subscribe_ids: Array.isArray(selection?.subscribeIds) ? selection.subscribeIds : [],
            history_targets: Array.isArray(selection?.targets) ? selection.targets : [],
          }
        : {}
      const result = await api.post(`plugin/${pluginId}/sync/start`, payload);
      if (!result?.success) throw new Error(result?.message || "启动失败");
      const selectedScope = result?.data?.scope === "selected";
      const subscribeCount = Number(result?.data?.subscribe_count || 0);
      const historyTargetCount = Number(result?.data?.history_target_count || 0);
      const mediaCount = Number(result?.data?.media_count || subscribeCount + historyTargetCount);
      startRequestedUntil = Date.now() + 10000;
      runtimeStreamDisabled = false;
      runtimeStreamFailures = 0;
      Object.assign(runtime, {
        status: "starting",
        task: selectedScope ? `正在准备所选 ${mediaCount} 个媒体目标` : "正在准备全部订阅",
        progress: 0,
        tasks: [],
      })
      notify(result.message || "订阅搜索任务已启动");
      armStartupGuard();
      ensureRuntimeUpdates();
      return true;
    } catch (error) {
      notify(error.message || "启动失败", "error");
      return false;
    }
  }

  async function stopSync() {
    try {
      runtime.status = "stopping";
      runtime.task = "正在停止当前任务";
      const result = await api.post(`plugin/${pluginId}/sync/stop`);
      if (!result?.success) throw new Error(result?.message || "停止失败");
      ensureRuntimeUpdates();
      return true;
    } catch (error) {
      notify(error.message || "停止失败", "error");
      await loadRuntime();
      return false;
    }
  }

  async function stopTask(taskId) {
    try {
      const task = (runtime.tasks || []).find((item) => item.id === taskId);
      if (task) {
        task.status = "stopping";
        task.phase = "等待安全停止";
      }
      const result = await api.post(`plugin/${pluginId}/sync/task/stop`, {
        task_id: taskId,
      })
      if (!result?.success) throw new Error(result?.message || "停止任务失败");
      ensureRuntimeUpdates();
      return true;
    } catch (error) {
      notify(error.message || "停止任务失败", "error");
      await loadRuntime();
      return false;
    }
  }

  async function upgradeHistory(records, scope = "record") {
    const identities = (records || []).map((record) => ({
      record_id: record.record_id,
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
      tmdb_id: record.tmdb_id,
      season: record.season,
      episode: record.episode,
    }))
    const result = await api.post(`plugin/${pluginId}/history/upgrade`, {
      source: "history",
      scope,
      records: identities,
    })
    if (!result?.success) throw new Error(result?.message || "洗版任务提交失败");
    startRequestedUntil = Date.now() + 10000;
    runtimeStreamDisabled = false;
    runtimeStreamFailures = 0;
    Object.assign(runtime, {
      status: "starting",
      task: "正在准备洗版任务",
      progress: 0,
      tasks: [],
    })
    armStartupGuard();
    ensureRuntimeUpdates();
    return result.message || "洗版任务已提交";
  }

  onMounted(() => {
    document.addEventListener("visibilitychange", handleVisibilityChange);
    void loadRuntime().finally(ensureRuntimeUpdates);
  })
  onUnmounted(() => {
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    stopRuntimeUpdates();
  })

  return {
    offlineSupported,
    runtime,
    active,
    loadRuntime,
    startSync,
    stopSync,
    stopTask,
    upgradeHistory,
  }
}
