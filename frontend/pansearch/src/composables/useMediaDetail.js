import {computed, ref, watch} from "vue";
import {
  DEFAULT_CHANNELS,
  getChannelDefaultIcon,
  getExtractedTags,
  getNormalizedResourceType,
  getResourceTabIcon,
  getResourceTypeName,
  getSourceName,
  isPointUnlockResource,
  responseItems,
  unwrapApiResponse,
} from "./resourceUtils";

/** 管理详情弹窗的数据补全、并发取消和生命周期。 */
export function useMediaDetail({api, pluginId, pluginConfig, showMessage}) {
  const detailVisible = ref(false);
  const detailLoading = ref(false);
  const activeMedia = ref(null);
  const activeDetailSeason = ref(1);
  const configuredChannels = ref([...DEFAULT_CHANNELS]);
  const availableChannels = computed(() => configuredChannels.value.filter((channel) => isChannelConfigured(channel.key)));
  const availableDrives = ref([
    {key: "115", name: "115网盘"},
    {key: "quark", name: "夸克网盘"},
    {key: "alipan", name: "阿里云盘"},
    {key: "123", name: "123云盘"},
    {key: "tianyi", name: "天翼云盘"},
    {key: "guangya", name: "光鸭网盘"},
  ]);
  const activeChannelTab = ref("pansou");
  const activeResourceTab = ref("");
  const resourceSearchQuery = ref("");
  const selectedResourceSpecs = ref([]);
  const channelResults = ref({});
  const channelLoading = ref({});
  const channelSearched = ref({});
  const channelElapsed = ref({});
  let requestToken = 0;

  function isChannelConfigured(channelKey) {
    const config = pluginConfig?.value || {};
    if (channelKey === "pansou") return Boolean(config.pansou_enabled ?? true);
    if (channelKey === "seedhub") return Boolean(config.seedhub_enabled ?? true);
    if (channelKey === "juying") return Boolean(config.juying_enabled ?? true);
    if (channelKey === "pinglian") return Boolean(config.pinglian_enabled ?? true);
    if (channelKey === "hdhive") {
      if (!(config.hdhive_enabled ?? true)) return false;
      return Boolean(config.hdhive_token || config.search_accounts?.hdhive?.connected);
    }
    if (channelKey === "dian115") {
      if (!(config.dian115_enabled ?? true)) return false;
      return Boolean((config.dian115_email && config.dian115_password) || config.search_accounts?.dian115?.connected);
    }
    if (channelKey === "piratebay") return Boolean(config.piratebay_enabled ?? true);
    if (channelKey === "uindex") return Boolean(config.uindex_enabled ?? true);
    if (channelKey === "online_docs") return Boolean(config.online_docs_enabled ?? true);
    return false;
  }

  const currentChannelResources = computed(() => channelResults.value[activeChannelTab.value] || []);
  const currentChannelResourceTabs = computed(() => {
    const counts = {};
    for (const item of currentChannelResources.value) {
      const type = getNormalizedResourceType(item);
      counts[type] = (counts[type] || 0) + 1;
    }
    const order = [
      "115",
      "quark",
      "alipan",
      "uc",
      "guangya",
      "tianyi",
      "123",
      "xunlei",
      "baidu",
      "magnet",
      "ed2k",
      "other",
    ];
    return Object.keys(counts)
      .sort((a, b) => {
        const aIndex = order.indexOf(a);
        const bIndex = order.indexOf(b);
        if (aIndex >= 0 && bIndex >= 0) return aIndex - bIndex;
        if (aIndex >= 0) return -1;
        if (bIndex >= 0) return 1;
        return counts[b] - counts[a];
      })
      .map((type) => ({
        value: type,
        title: getResourceTypeName(type),
        count: counts[type],
        icon: getResourceTabIcon(type),
      }));
  });

  const activeResourceFilterCount = computed(() => {
    let count = 0;
    if (String(resourceSearchQuery.value || "").trim()) count += 1;
    if (Array.isArray(selectedResourceSpecs.value)) count += selectedResourceSpecs.value.length;
    return count;
  });

  function resetResourceFilters() {
    resourceSearchQuery.value = "";
    selectedResourceSpecs.value = [];
  }

  const currentChannelFilteredResources = computed(() => {
    const list = currentChannelResources.value;
    const selected = activeResourceTab.value;
    // 1. 过滤当前选中的子 tab
    const availableTabs = currentChannelResourceTabs.value;
    const effectiveSelected = availableTabs.some((tab) => tab.value === selected)
      ? selected
      : (availableTabs[0]?.value || "");
    const tabFiltered = !effectiveSelected
      ? list
      : list.filter((item) => getNormalizedResourceType(item) === effectiveSelected);

    // 2. 搜索当前子 tab 列表的文本
    const query = String(resourceSearchQuery.value || "").trim().toLowerCase();
    let searched = tabFiltered;
    if (query) {
      searched = searched.filter((item) => {
        const title = String(item?.title || "").toLowerCase();
        const desc = String(item?.description || "").toLowerCase();
        const fileName = String(item?.file_name || "").toLowerCase();
        const tags = (item?.tags || []).map((t) => String(t || "").toLowerCase()).join(" ");
        const extracted = (getExtractedTags(item) || []).map((t) => String(t || "").toLowerCase()).join(" ");
        return title.includes(query) || desc.includes(query) || fileName.includes(query) || tags.includes(query) || extracted.includes(query);
      });
    }

    // 3. 规格快筛过滤
    const specs = selectedResourceSpecs.value || [];
    if (specs.length > 0) {
      searched = searched.filter((item) => {
        const itemTags = [
          ...(item?.tags || []),
          ...(getExtractedTags(item) || []),
        ].map((t) => String(t).toUpperCase());
        const titleUpper = String(item?.title || "").toUpperCase();

        return specs.every((spec) => {
          const specUpper = String(spec).toUpperCase();
          if (specUpper === "免费") {
            return isPointUnlockResource(item) && Number(item?.unlock_points || 0) === 0;
          }
          if (specUpper === "4K") {
            return itemTags.includes("4K") || titleUpper.includes("4K") || titleUpper.includes("2160P");
          }
          if (specUpper === "1080P") {
            return itemTags.includes("1080P") || titleUpper.includes("1080P");
          }
          if (specUpper === "原盘") {
            return itemTags.some((t) => t.includes("原盘") || t.includes("REMUX") || t.includes("BLURAY") || t.includes("BDMV"))
              || /原盘|REMUX|BLURAY|BDMV/i.test(titleUpper);
          }
          if (specUpper === "HDR") {
            return itemTags.some((t) => t.includes("HDR")) || /HDR/i.test(titleUpper);
          }
          if (specUpper === "杜比视界" || specUpper === "DV") {
            return itemTags.some((t) => t.includes("杜比") || t.includes("DV") || t.includes("DOVI"))
              || /杜比视界|\bDV\b|DOVI|DOLBY\s*VISION/i.test(titleUpper);
          }
          return itemTags.includes(specUpper) || titleUpper.includes(specUpper);
        });
      });
    }

    return searched
      .map((item, index) => ({item, index}))
      .sort((a, b) => resourceTagCount(b.item) - resourceTagCount(a.item) || a.index - b.index)
      .map(({item}) => item);
  });

  function resourceTagCount(resource) {
    const extracted = getExtractedTags(resource) || [];
    const raw = Array.isArray(resource?.tags) ? resource.tags : [];
    return new Set([...extracted, ...raw.map((tag) => String(tag || "").trim()).filter(Boolean)]).size;
  }

  async function loadMediaDetail(item, token = requestToken) {
    const response = await api.value.post(`plugin/${pluginId.value}/resource/detail`, item);
    const result = unwrapApiResponse(response);
    const detail = result?.data?.item || result?.item;
    if (result?.success === false || !detail || typeof detail !== "object") {
      throw new Error(result?.message || "获取媒体详情失败");
    }
    if (token !== requestToken) return false;
    activeMedia.value = {...activeMedia.value, ...detail};
    return true;
  }

  async function openMedia(item) {
    const token = ++requestToken;
    activeMedia.value = {...item};
    detailLoading.value = true;
    detailVisible.value = true;
    try {
      await loadMediaDetail(item, token);
    } finally {
      if (token === requestToken) detailLoading.value = false;
    }
    return token === requestToken && detailVisible.value;
  }

  function resetChannelState() {
    channelResults.value = {};
    channelLoading.value = {};
    channelSearched.value = {};
    channelElapsed.value = {};
    activeResourceTab.value = "";
    resourceSearchQuery.value = "";
    selectedResourceSpecs.value = [];
  }

  function syncAvailableChannels(sources) {
    if (!Array.isArray(sources)) return;
    for (const source of sources) {
      const key = (typeof source === "object" && source?.key ? source.key : String(source)).toLowerCase();
      const name = typeof source === "object" && source?.name ? source.name : getSourceName(key);
      const existing = configuredChannels.value.find((channel) => channel.key === key);
      if (!existing) configuredChannels.value.push({key, name, icon: getChannelDefaultIcon(key)});
      else if (name) existing.name = name;
    }
  }

  async function searchChannel(channelKey, force = false) {
    if (!channelKey || !isChannelConfigured(channelKey) || !activeMedia.value || (!force && channelSearched.value[channelKey])) return;
    channelLoading.value = {...channelLoading.value, [channelKey]: true};
    try {
      const media = activeMedia.value;
      const response = await api.value.post(`plugin/${pluginId.value}/resource/search_resources`, {
        source: channelKey,
        force: Boolean(force),
        force_refresh: Boolean(force),
        title: media.title,
        original_title: media.original_title || "",
        year: media.year || "",
        media_type: media.media_type || "movie",
        tmdb_id: media.tmdb_id || 0,
        imdb_id: media.imdb_id || "",
        tvdb_id: media.tvdb_id || 0,
        douban_id: media.douban_id || 0,
        bangumi_id: media.bangumi_id || 0,
        anilist_id: media.anilist_id || 0,
        anidb_id: media.anidb_id || 0,
        media_source: media.media_source || "",
        media_id: media.media_id || "",
      });
      const result = unwrapApiResponse(response);
      channelResults.value = {...channelResults.value, [channelKey]: result?.success ? responseItems(result) : []};
      channelElapsed.value = {...channelElapsed.value, [channelKey]: result?.data?.elapsed ?? null};
      if (result?.success) {
        syncAvailableChannels(result.data?.available_sources);
        if (Array.isArray(result.data?.available_drives) && result.data.available_drives.length) {
          availableDrives.value = result.data.available_drives;
        }
        if (result.data?.main_cloud_drive && pluginConfig?.value)
          pluginConfig.value.cloud_drive = result.data.main_cloud_drive;
      } else {
        showMessage?.(result?.message || `${getSourceName(channelKey)} 检索失败`, "warning");
      }
    } catch (error) {
      channelResults.value = {...channelResults.value, [channelKey]: []};
      const unavailable = error?.response?.status === 502 || String(error?.message || "").includes("502");
      showMessage?.(
        unavailable
          ? `${getSourceName(channelKey)} 渠道服务暂不可用 (502)，请稍后重试`
          : `${getSourceName(channelKey)} 搜索异常: ${error?.message || error}`,
        "warning",
      );
    } finally {
      channelLoading.value = {...channelLoading.value, [channelKey]: false};
      channelSearched.value = {...channelSearched.value, [channelKey]: true};
    }
  }

  async function openMediaDetail(item) {
    resetChannelState();
    try {
      const stillOpen = await openMedia(item);
      if (!stillOpen) return;
    } catch (error) {
      console.warn("[网盘资源] 获取媒体详情失败，使用榜单媒体信息继续", error);
    }
    if (!detailVisible.value) return;
    const seasons = activeMedia.value?.seasons || [];
    const defaultSeason =
      seasons.find((season) => {
        const episodes = Array.isArray(season?.episodes) ? season.episodes : [];
        return !episodes.length || episodes.some((episode) => !episode?.in_library);
      }) || seasons[0];
    activeDetailSeason.value = defaultSeason?.season_number || 1;
    const firstChannel = availableChannels.value[0]?.key || "";
    activeChannelTab.value = firstChannel;
    if (firstChannel) await searchChannel(firstChannel);
  }

  function onChannelTabChange(channelKey) {
    activeResourceTab.value = "";
    resourceSearchQuery.value = "";
    if (!channelSearched.value[channelKey] && !channelLoading.value[channelKey]) searchChannel(channelKey);
  }

  function getChannelCount(channelKey) {
    return channelKey === activeChannelTab.value
      ? currentChannelResources.value.length
      : (channelResults.value[channelKey] || []).length;
  }

  function closeMediaDetail() {
    detailVisible.value = false;
  }

  watch(detailVisible, (visible) => {
    if (visible) return;
    requestToken += 1;
    detailLoading.value = false;
  });

  watch(
    currentChannelResourceTabs,
    (tabs) => {
      const first = tabs[0]?.value || "";
      if (!tabs.length) {
        activeResourceTab.value = "";
      } else if (!tabs.some((tab) => tab.value === activeResourceTab.value)) {
        activeResourceTab.value = first;
      }
    },
    {immediate: true},
  );

  watch(
    availableChannels,
    (channels) => {
      const first = channels[0]?.key || "";
      if (!channels.some((channel) => channel.key === activeChannelTab.value)) {
        activeChannelTab.value = first;
      }
    },
    {immediate: true},
  );

  return {
    detailVisible,
    detailLoading,
    activeMedia,
    activeDetailSeason,
    availableChannels,
    availableDrives,
    activeChannelTab,
    activeResourceTab,
    resourceSearchQuery,
    selectedResourceSpecs,
    activeResourceFilterCount,
    resetResourceFilters,
    channelResults,
    channelLoading,
    channelSearched,
    channelElapsed,
    currentChannelResources,
    currentChannelResourceTabs,
    currentChannelFilteredResources,
    openMediaDetail,
    searchChannel,
    onChannelTabChange,
    getChannelCount,
    closeMediaDetail,
  };
}
