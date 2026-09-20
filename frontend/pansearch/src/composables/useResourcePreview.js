import {computed, ref, watch} from "vue";
import {getNormalizedResourceType, getResourceTypeName, getSourceName, previewResourceKey} from "./resourceUtils";

/** 管理资源预览、目录下钻、选择和转存编排。 */
export function useResourcePreview({
                                     api,
                                     pluginId,
                                     submitDownload,
                                     requestCrossTransfer,
                                     isCrossTransfer,
                                     showMessage,
                                   }) {
  const previewVisible = ref(false);
  const previewingKey = ref("");
  const previewLoading = ref(false);
  const previewError = ref("");
  const previewItems = ref([]);
  const selectedPreviewItems = ref([]);
  const previewMeta = ref({});
  const previewBreadcrumbs = ref([]);
  const previewResourceType = ref("");
  const previewShareUrl = ref("");
  const previewSource = ref("");
  const previewProviderData = ref({});
  const previewPendingResource = ref({});
  const previewTargetSeason = ref(null);
  const previewTargetEpisodes = ref([]);
  let requestId = 0;

  const nextPreviewRequest = () => ++requestId;
  const isPreviewRequestCurrent = (value) => value === requestId;

  const isAllPreviewSelected = computed(() => {
    if (!previewItems.value.length) return false;
    return previewItems.value.every((file) => isItemSelected(file));
  });

  function getPreviewShareUrl() {
    return String(previewShareUrl.value || previewMeta.value?.share_url || "").trim();
  }

  function canTransferPreviewResource() {
    return Boolean(
      getPreviewShareUrl() ||
      previewPendingResource.value?.resource_ref ||
      previewPendingResource.value?.provider_data?.resource_id ||
      previewPendingResource.value?.provider_data?.seed_id ||
      previewPendingResource.value?.provider_data?.token,
    );
  }

  async function previewResource(item) {
    if (previewingKey.value) return;
    const token = nextPreviewRequest();
    previewingKey.value = previewResourceKey(item);
    const shareUrl = String(item?.url || "");
    previewVisible.value = true;
    previewError.value = "";
    previewItems.value = [];
    selectedPreviewItems.value = [];
    previewResourceType.value = getNormalizedResourceType(item);
    previewShareUrl.value = shareUrl;
    previewSource.value = String(item?.source || "").toLowerCase();
    previewProviderData.value = {...(item?.provider_data || {})};
    previewPendingResource.value = {
      pending_resolution: Boolean(item?.pending_resolution),
      resource_ref: String(item?.resource_ref || ""),
      is_unlocked: Boolean(item?.is_unlocked),
      supports_file_preview: item?.supports_file_preview,
      target_season: item?.target_season ?? null,
      target_episodes: Array.isArray(item?.target_episodes) ? [...item.target_episodes] : [],
      provider_data: {...(item?.provider_data || {})},
    };
    previewTargetSeason.value = item?.target_season ?? null;
    previewTargetEpisodes.value = Array.isArray(item?.target_episodes) ? [...item.target_episodes] : [];
    previewMeta.value = {
      provider_name: getSourceName(item?.source),
      resource_type_name: getResourceTypeName(previewResourceType.value),
      display_name: String(item?.title || ""),
      share_url: shareUrl,
      size: Number(item?.size || item?.size_bytes || 0),
    };
    const breadcrumbs = [{id: "", name: "根目录"}];
    previewBreadcrumbs.value = breadcrumbs;
    await loadPreviewDirectory("", breadcrumbs, token);
    if (isPreviewRequestCurrent(token)) previewingKey.value = "";
  }

  async function loadPreviewDirectory(parentId, breadcrumbs, token = nextPreviewRequest()) {
    const pendingJuying = previewSource.value === "juying" && previewProviderData.value?.resource_id;
    const pendingHdhive = previewSource.value === "hdhive" && Boolean(previewPendingResource.value.resource_ref);
    const pendingSource =
      ["seedhub", "pinglian"].includes(previewSource.value) &&
      previewPendingResource.value.pending_resolution &&
      !previewShareUrl.value;
    if (!previewShareUrl.value && !pendingJuying && !pendingHdhive && !pendingSource) return;

    const shareUrl = previewShareUrl.value;
    previewLoading.value = true;
    previewError.value = "";
    selectedPreviewItems.value = [];
    try {
      const response = await api.value.post(`plugin/${pluginId.value}/search/preview`, {
        resource_type: previewResourceType.value,
        url: shareUrl,
        resource_ref: previewPendingResource.value.resource_ref || "",
        parent_id: parentId || "",
        source: previewSource.value,
        pending_resolution: Boolean(previewPendingResource.value.pending_resolution),
        is_unlocked: Boolean(previewPendingResource.value.is_unlocked),
        supports_file_preview: previewPendingResource.value.supports_file_preview,
        provider_data: previewProviderData.value,
        target_season: previewTargetSeason.value,
        target_episodes: previewTargetEpisodes.value,
      });
      if (!isPreviewRequestCurrent(token) || !previewVisible.value) return;
      if (response?.success === false) throw new Error(response.message || "资源预览失败");
      const data = response?.data?.data || response?.data || {};
      if (data.resource_type) previewResourceType.value = String(data.resource_type).toLowerCase();
      if (data.share_url) {
        previewShareUrl.value = String(data.share_url);
        previewPendingResource.value.pending_resolution = false;
      }
      previewItems.value = Array.isArray(data.items) ? data.items : [];
      previewMeta.value = {
        provider_name: String(data.provider_name || previewMeta.value.provider_name || ""),
        resource_type_name: String(data.resource_type_name || previewMeta.value.resource_type_name || ""),
        display_name: String(data.display_name || previewMeta.value.display_name || ""),
        info_hash: String(data.info_hash || ""),
        size: Number(data.size || previewMeta.value.size || 0),
        share_url: String(data.share_url || shareUrl),
      };
      previewBreadcrumbs.value = breadcrumbs;
    } catch (error) {
      if (!isPreviewRequestCurrent(token) || !previewVisible.value) return;
      previewItems.value = [];
      previewError.value = error?.response?.data?.message || error?.message || String(error);
    } finally {
      if (isPreviewRequestCurrent(token)) previewLoading.value = false;
    }
  }

  function openPreviewFolder(file) {
    if (!file?.can_enter || previewLoading.value) return;
    loadPreviewDirectory(String(file.id || ""), [
      ...previewBreadcrumbs.value,
      {id: String(file.id || ""), name: String(file.name || "未命名目录")},
    ]);
  }

  function openPreviewBreadcrumb(index) {
    const breadcrumb = previewBreadcrumbs.value[index];
    if (!breadcrumb || previewLoading.value) return;
    loadPreviewDirectory(String(breadcrumb.id || ""), previewBreadcrumbs.value.slice(0, index + 1));
  }

  function isItemSelected(file) {
    const id = file?.id || file?.name;
    return selectedPreviewItems.value.some((item) => (item.id || item.name) === id);
  }

  function toggleSelectPreviewItem(file) {
    const id = file?.id || file?.name;
    const index = selectedPreviewItems.value.findIndex((item) => (item.id || item.name) === id);
    if (index >= 0) selectedPreviewItems.value.splice(index, 1);
    else selectedPreviewItems.value.push(file);
  }

  function toggleSelectAllPreview(value) {
    selectedPreviewItems.value = value ? [...previewItems.value] : [];
  }

  async function runTransfer(files, title, emptySelection = false) {
    if (!emptySelection && !files.length) return;
    const fileIds = files.map((file) => file.id).filter(Boolean);
    const fileNames = files.map((file) => file.name).filter(Boolean);
    const resource = {
      ...previewPendingResource.value,
      resource_type: previewResourceType.value,
      ...(files.length ? {target_file_ids: fileIds, target_file_names: fileNames} : {}),
    };
    const cross = isCrossTransfer(resource);
    const action = async (targetCloud = "") => {
      await submitDownload(getPreviewShareUrl(), title, resource, {
        target_cloud: targetCloud,
        is_cross: cross,
      });
    };
    if (cross) requestCrossTransfer(resource, action);
    else await action();
  }

  async function handleSingleFileTransfer(file) {
    try {
      showMessage?.(`正在提交单个内容【${file.name}】的转存...`, "info");
      await runTransfer([file], file.name);
    } catch (error) {
      showMessage?.(`转存失败: ${error?.message || error}`, "error");
    }
  }

  async function handleBatchTransfer() {
    const files = [...selectedPreviewItems.value];
    if (!files.length) return;
    try {
      showMessage?.(`正在提交已选 ${files.length} 个项目的批量转存...`, "info");
      await runTransfer(files, files[0]?.name || previewMeta.value.display_name);
    } catch (error) {
      showMessage?.(`批量转存失败: ${error?.message || error}`, "error");
    }
  }

  async function handleQuickDownloadFromPreview() {
    try {
      await runTransfer([], previewMeta.value.display_name, true);
    } catch (error) {
      showMessage?.(`提交下载失败: ${error?.message || error}`, "error");
    }
  }

  watch(previewVisible, (visible) => {
    if (visible) return;
    nextPreviewRequest();
    previewLoading.value = false;
    previewingKey.value = "";
    selectedPreviewItems.value = [];
  });

  return {
    previewVisible,
    previewingKey,
    previewLoading,
    previewError,
    previewItems,
    selectedPreviewItems,
    previewMeta,
    previewBreadcrumbs,
    previewResourceType,
    previewShareUrl,
    previewSource,
    previewProviderData,
    previewPendingResource,
    previewTargetSeason,
    previewTargetEpisodes,
    isAllPreviewSelected,
    getPreviewShareUrl,
    canTransferPreviewResource,
    previewResourceKey,
    previewResource,
    openPreviewFolder,
    openPreviewBreadcrumb,
    isItemSelected,
    toggleSelectPreviewItem,
    toggleSelectAllPreview,
    handleSingleFileTransfer,
    handleBatchTransfer,
    handleQuickDownloadFromPreview,
  };
}
