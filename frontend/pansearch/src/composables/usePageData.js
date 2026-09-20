import {computed, reactive, ref} from "vue";

export function useHistoryPageData(api, notify, pluginId = "PanSearch") {
  const historyGroups = ref([]);
  const embyPlayItems = ref({});
  const loading = ref(false);
  const historyPage = reactive({
    page: 1,
    pageSize: 10,
    total: 0,
    totalPages: 1,
    filterOptions: {
      resourceTypes: [],
      sources: [],
    },
    enableCloudUpgrade: false,
  })
  const historyStats = reactive({
    total: 0,
    today: 0,
    success: 0,
    failed: 0,
  })
  const historyQuery = reactive({
    page: 1,
    pageSize: 10,
    keyword: "",
    resourceTypes: [],
    sources: [],
    taskTypes: [],
    statuses: [],
  })
  const stats = computed(() => [
    {
      title: "总转存",
      value: historyStats.total,
      color: "primary",
      icon: "mdi-cloud-upload-outline",
    },
    {
      title: "今日转存",
      value: historyStats.today,
      color: "info",
      icon: "mdi-calendar-today",
    },
    {
      title: "成功",
      value: historyStats.success,
      color: "success",
      icon: "mdi-check-circle-outline",
    },
    {
      title: "失败",
      value: historyStats.failed,
      color: "error",
      icon: "mdi-alert-circle-outline",
    },
  ])

  let pageRequestSequence = 0;
  let summaryRequestSequence = 0;

  function normalizeQueryList(value) {
    return [...new Set((Array.isArray(value) ? value : []).map((item) => String(item || "").trim()).filter(Boolean))];
  }

  function pageQueryString() {
    const query = new URLSearchParams({
      page: String(historyQuery.page),
      page_size: String(historyQuery.pageSize),
    })
    if (historyQuery.keyword) query.set("keyword", historyQuery.keyword);
    for (const [key, values] of [
      ["resource_types", historyQuery.resourceTypes],
      ["sources", historyQuery.sources],
      ["task_types", historyQuery.taskTypes],
      ["statuses", historyQuery.statuses],
    ]) {
      if (values.length) query.set(key, values.join(","));
    }
    return query.toString();
  }

  async function loadPage(showLoading = true) {
    const requestId = ++pageRequestSequence;
    if (showLoading) loading.value = true;
    try {
      const result = await api.get(`plugin/${pluginId}/page_data?${pageQueryString()}`);
      if (!result?.success) throw new Error(result?.message || "加载失败");
      if (requestId !== pageRequestSequence) return false;
      const data = result.data || {};
      const pageData = data.history_page || {};
      historyGroups.value = Array.isArray(data.history_groups) ? data.history_groups : [];
      embyPlayItems.value = data.emby_play_items || {};
      Object.assign(historyPage, {
        page: Math.max(1, Number(pageData.page || 1)),
        pageSize: Math.max(1, Number(pageData.page_size || 10)),
        total: Math.max(0, Number(pageData.total || 0)),
        totalPages: Math.max(1, Number(pageData.total_pages || 1)),
        filterOptions: {
          resourceTypes: Array.isArray(pageData.filter_options?.resource_types)
            ? pageData.filter_options.resource_types
            : [],
          sources: Array.isArray(pageData.filter_options?.sources) ? pageData.filter_options.sources : [],
        },
        enableCloudUpgrade: Boolean(pageData.enable_cloud_upgrade),
      })
      historyQuery.page = historyPage.page;
      historyQuery.pageSize = historyPage.pageSize;
      return true;
    } catch (error) {
      if (requestId === pageRequestSequence && showLoading) {
        notify(error.message || "加载失败", "error");
      }
      return false;
    } finally {
      if (requestId === pageRequestSequence) loading.value = false;
    }
  }

  async function loadSummary(showError = true) {
    const requestId = ++summaryRequestSequence;
    try {
      const result = await api.get(`plugin/${pluginId}/history/summary`);
      if (!result?.success) {
        throw new Error(result?.message || "加载历史摘要失败");
      }
      if (requestId !== summaryRequestSequence) return false;
      const data = result.data || {};
      Object.assign(historyStats, {
        total: Math.max(0, Number(data.total || 0)),
        today: Math.max(0, Number(data.today || 0)),
        success: Math.max(0, Number(data.success || 0)),
        failed: Math.max(0, Number(data.failed || 0)),
      })
      return true;
    } catch (error) {
      if (requestId === summaryRequestSequence && showError) {
        notify(error.message || "加载历史摘要失败", "error");
      }
      return false;
    }
  }

  async function updateHistoryQuery(nextQuery = {}) {
    const normalized = {
      page: Math.max(1, Number(nextQuery.page ?? historyQuery.page) || 1),
      pageSize: Math.min(50, Math.max(1, Number(nextQuery.pageSize ?? historyQuery.pageSize) || 10)),
      keyword: String(nextQuery.keyword ?? historyQuery.keyword).trim(),
      resourceTypes: normalizeQueryList(nextQuery.resourceTypes ?? historyQuery.resourceTypes),
      sources: normalizeQueryList(nextQuery.sources ?? historyQuery.sources),
      taskTypes: normalizeQueryList(nextQuery.taskTypes ?? historyQuery.taskTypes),
      statuses: normalizeQueryList(nextQuery.statuses ?? historyQuery.statuses),
    }
    const unchanged = Object.entries(normalized).every(([key, value]) =>
      Array.isArray(value) ? JSON.stringify(value) === JSON.stringify(historyQuery[key]) : value === historyQuery[key],
    )
    if (unchanged) return false;
    Object.assign(historyQuery, normalized);
    return loadPage();
  }

  async function clearHistory(force = false, clearPointsHistory = false) {
    const result = await api.post(`plugin/${pluginId}/history/clear`, {
      force: Boolean(force),
      clear_points_history: Boolean(clearPointsHistory),
    })
    if (!result?.success) throw new Error(result?.message || "清空失败");
    await Promise.all([loadPage(false), loadSummary(false)]);
    return result.message || "历史已清空";
  }

  async function deleteHistory(record, deleteLinkedFiles = false) {
    const result = await api.post(`plugin/${pluginId}/history/delete`, {
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
      tmdb_id: record.tmdb_id,
      season: record.season,
      episode: record.episode,
      delete_linked_files: Boolean(deleteLinkedFiles),
    })
    if (!result?.success) throw new Error(result?.message || "删除失败");
    await Promise.all([loadPage(false), loadSummary(false)]);
    return result.message || "历史记录已删除";
  }

  async function deleteHistoryBatch(records, deleteLinkedFiles = false) {
    const identities = records.map((record) => ({
      record_id: record.record_id,
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
      tmdb_id: record.tmdb_id,
      season: record.season,
      episode: record.episode,
    }))
    const result = await api.post(`plugin/${pluginId}/history/delete_batch`, {
      records: identities,
      delete_linked_files: Boolean(deleteLinkedFiles),
    })
    if (!result?.success) throw new Error(result?.message || "批量删除失败");
    await Promise.all([loadPage(false), loadSummary(false)]);
    return result.message || "所选历史记录已删除";
  }

  async function notifyHistory(record) {
    const result = await api.post(`plugin/${pluginId}/history/notify`, {
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
      tmdb_id: record.tmdb_id,
      season: record.season,
      episode: record.episode,
    })
    if (!result?.success) throw new Error(result?.message || "通知失败");
    return result.message || "通知已补发";
  }

  return {
    historyGroups,
    historyPage,
    historyStats,
    embyPlayItems,
    loading,
    stats,
    loadPage,
    loadSummary,
    updateHistoryQuery,
    clearHistory,
    deleteHistory,
    deleteHistoryBatch,
    notifyHistory,
  }
}
