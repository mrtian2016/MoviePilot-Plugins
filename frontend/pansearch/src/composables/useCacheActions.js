export function useCacheActions(api, pluginId = "PanSearch") {
  async function clearCache(categories) {
    const result = await api.post(`plugin/${pluginId}/cache/clear`, {
      categories,
    })
    if (!result?.success) throw new Error(result?.message || "清理缓存失败")
    return result.message || "缓存已清理"
  }

  return { clearCache }
}
