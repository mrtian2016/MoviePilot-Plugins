import {computed, nextTick, ref, watch} from "vue";
import {
  getRatingValue,
  hasMissingEpisodes,
  mediaCategory,
  mediaFilterTitle,
  mediaGenres,
  mediaItemKey,
  mediaLanguages,
  mediaRegions,
  prepareMediaItems,
  responseItems,
  unwrapApiResponse,
} from "./resourceUtils";

/**
 * 推荐加载、搜索、分页、筛选的所有状态与逻辑。
 */
export function useMediaData({api, pluginId, showMessage}) {
  const activeTab = ref("tmdb_trending");
  const loadingMedias = ref(false);
  const mediaItems = ref([]);
  const currentPage = ref(1);
  const hasMore = ref(true);
  const loadingMore = ref(false);
  const mediaGridSection = ref(null);
  const scrollSentinel = ref(null);
  let sentinelObserver = null;

  const showFilterBar = ref(false);
  const mediaFilters = ref({
    status: "all",
    category: "全部",
    type: "全部",
    region: "全部",
    language: "全部",
    year: "全部",
    rating: 0,
    sort: "latest",
  });

  let currentRecommendToken = 0;
  let nextPageDebounceTimer = null;
  const recommendTabCache = new Map();

  // 筛选
  const mediaFilterOptions = computed(() => {
    const items = mediaItems.value || [];
    const collect = (getter, fallback) => {
      const values = [...new Set(items.flatMap(getter).filter(Boolean))].sort((a, b) =>
        String(a).localeCompare(String(b), "zh-CN"),
      );
      return [fallback, ...values.filter((value) => value !== fallback)];
    };
    const localizedCollect = (getter, group) =>
      collect(getter, "全部").map((value) => ({title: mediaFilterTitle(value, group), value}));
    return {
      status: [
        {title: "全部", value: "all"},
        {title: "未入库", value: "unlibrary"},
        {title: "缺失剧集", value: "missing"},
      ],
      category: localizedCollect((item) => [mediaCategory(item)], "category"),
      type: localizedCollect((item) => mediaGenres(item), "type"),
      region: localizedCollect((item) => mediaRegions(item), "region"),
      language: localizedCollect((item) => mediaLanguages(item), "language"),
      year: collect((item) => (item?.year ? [String(item.year)] : []), "全部"),
      rating: [
        {title: "不限", value: 0},
        {title: "6.0 分以上", value: 6},
        {title: "7.0 分以上", value: 7},
        {title: "8.0 分以上", value: 8},
        {title: "9.0 分以上", value: 9},
      ],
      sort: [
        {title: "最新", value: "latest"},
        {title: "评分最高", value: "rating"},
        {title: "评分最低", value: "rating_asc"},
        {title: "标题", value: "title"},
      ],
    };
  });

  const activeMediaFilterCount = computed(() => {
    const filters = mediaFilters.value;
    return [
      filters.status !== "all",
      filters.category !== "全部",
      filters.type !== "全部",
      filters.region !== "全部",
      filters.language !== "全部",
      filters.year !== "全部",
      Number(filters.rating || 0) > 0,
      filters.sort !== "latest",
    ].filter(Boolean).length;
  });

  function resetMediaFilters() {
    mediaFilters.value = {
      status: "all",
      category: "全部",
      type: "全部",
      region: "全部",
      language: "全部",
      year: "全部",
      rating: 0,
      sort: "latest",
    };
  }

  // 筛选后列表
  function filteredMediaItems(isSearchMode) {
    if (isSearchMode) return mediaItems.value || [];
    const filters = mediaFilters.value;
    const result = (mediaItems.value || []).filter((item) => {
      if (filters.status === "unlibrary" && item.in_library) return false;
      if (filters.status === "missing" && !hasMissingEpisodes(item)) return false;
      if (filters.category !== "全部" && mediaCategory(item) !== filters.category) return false;
      if (filters.type !== "全部" && !mediaGenres(item).includes(filters.type)) return false;
      if (filters.region !== "全部" && !mediaRegions(item).includes(filters.region)) return false;
      if (filters.language !== "全部" && !mediaLanguages(item).includes(filters.language)) return false;
      if (filters.year !== "全部" && String(item.year || "") !== filters.year) return false;
      if (getRatingValue(item) < Number(filters.rating || 0)) return false;
      return true;
    });
    return result.sort((a, b) => {
      if (filters.sort === "rating") return getRatingValue(b) - getRatingValue(a);
      if (filters.sort === "rating_asc") return getRatingValue(a) - getRatingValue(b);
      if (filters.sort === "title") return String(a.title || "").localeCompare(String(b.title || ""), "zh-CN");
      return 0;
    });
  }

  // 推荐加载
  async function loadRecommend(sourceName, page = 1, append = false, force = false) {
    if (append) {
      if (loadingMore.value || !hasMore.value || loadingMedias.value) return;
      loadingMore.value = true;
    } else {
      const cached = recommendTabCache.get(sourceName);
      if (!force && page === 1 && cached && Date.now() - cached.time < 15 * 60 * 1000) {
        mediaItems.value = [...cached.items];
        currentPage.value = cached.page;
        hasMore.value = cached.hasMore;
        loadingMedias.value = false;
        return;
      }
      loadingMedias.value = true;
      loadingMore.value = false;
      currentPage.value = 1;
      hasMore.value = true;
      mediaItems.value = [];
    }

    const token = ++currentRecommendToken;
    try {
      const res = unwrapApiResponse(
        await api.value.get(
          `plugin/${pluginId.value}/resource/recommend?source=${sourceName}&page=${page}&count=24${force ? "&force=true" : ""}`,
        ),
      );
      if (token !== currentRecommendToken) return;

      if (res?.success !== false) {
        const newItems = prepareMediaItems(responseItems(res));
        const serverHasMore =
          res?.data?.has_more !== undefined
            ? res.data.has_more
            : res?.has_more !== undefined
              ? res.has_more
              : newItems.length > 0;
        hasMore.value = Boolean(serverHasMore && newItems.length > 0);
        currentPage.value = page;

        if (append) {
          const existingKeys = new Set(mediaItems.value.map((it) => mediaItemKey(it)));
          for (const item of newItems) {
            const k = mediaItemKey(item);
            if (!existingKeys.has(k)) {
              mediaItems.value.push(item);
              existingKeys.add(k);
            }
          }
        } else {
          mediaItems.value = newItems;
        }
        recommendTabCache.set(sourceName, {
          items: [...mediaItems.value],
          page: currentPage.value,
          hasMore: hasMore.value,
          time: Date.now(),
        });
        nextTick(() => {
          setupSentinelObserver();
          setTimeout(() => checkAndTriggerNextPage(), 150);
        });
      } else {
        showMessage(res?.message || "获取推荐榜单失败", "error");
        hasMore.value = false;
      }
    } catch (err) {
      if (token === currentRecommendToken) {
        showMessage(`请求失败: ${err.message || err}`, "error");
        hasMore.value = false;
      }
    } finally {
      if (token === currentRecommendToken) {
        if (append) loadingMore.value = false;
        else loadingMedias.value = false;
      }
    }
  }

  function loadNextPage(isSearchMode = false) {
    const inSearch = Boolean(isSearchMode === true);
    if (loadingMedias.value || loadingMore.value || !hasMore.value || inSearch) return;
    if (!mediaItems.value.length) return;
    if (nextPageDebounceTimer) clearTimeout(nextPageDebounceTimer);
    nextPageDebounceTimer = setTimeout(() => {
      if (loadingMedias.value || loadingMore.value || !hasMore.value) return;
      loadRecommend(activeTab.value, currentPage.value + 1, true);
    }, 100);
  }

  function checkAndTriggerNextPage(isSearchMode) {
    if (loadingMedias.value || loadingMore.value || !hasMore.value || isSearchMode) return;
    if (!scrollSentinel.value) return;
    const rect = scrollSentinel.value.getBoundingClientRect();
    const windowHeight = window.innerHeight || document.documentElement.clientHeight || 800;
    if (rect.top <= windowHeight + 600) loadNextPage(isSearchMode);
  }

  let scrollThrottleTimer = null;

  function handleScroll(isSearchMode) {
    if (scrollThrottleTimer) return;
    scrollThrottleTimer = setTimeout(() => {
      scrollThrottleTimer = null;
      checkAndTriggerNextPage(isSearchMode);
    }, 80);
  }

  function setupSentinelObserver(isSearchMode) {
    if (sentinelObserver) {
      sentinelObserver.disconnect();
      sentinelObserver = null;
    }
    if (!scrollSentinel.value) return;
    sentinelObserver = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) loadNextPage(isSearchMode);
      },
      {root: null, rootMargin: "500px"},
    );
    sentinelObserver.observe(scrollSentinel.value);
  }

  watch(scrollSentinel, (el) => {
    if (el)
      nextTick(() => {
        setupSentinelObserver();
        checkAndTriggerNextPage();
      });
  });

  function onTabChange(val, isSearchMode) {
    if (activeTab.value === val && !isSearchMode) return;
    activeTab.value = val;
    if (nextPageDebounceTimer) {
      clearTimeout(nextPageDebounceTimer);
      nextPageDebounceTimer = null;
    }
    currentPage.value = 1;
    hasMore.value = true;
    resetMediaFilters();
    loadRecommend(val, 1, false);
    return val;
  }

  async function performSearch(keyword) {
    loadingMedias.value = true;
    hasMore.value = false;
    mediaItems.value = [];
    resetMediaFilters();
    try {
      const res = await api.value.post(`plugin/${pluginId.value}/search/tmdb`, {
        title: keyword,
        media_type: "",
      });
      const result = unwrapApiResponse(res);
      if (result?.success !== false) {
        mediaItems.value = prepareMediaItems(responseItems(result));
      } else {
        showMessage(res?.message || "搜索失败", "error");
      }
    } catch (err) {
      showMessage(`搜索出错: ${err.message || err}`, "error");
    } finally {
      loadingMedias.value = false;
      hasMore.value = false;
    }
  }

  function cleanupTimers() {
    if (sentinelObserver) {
      sentinelObserver.disconnect();
      sentinelObserver = null;
    }
    if (scrollThrottleTimer) {
      clearTimeout(scrollThrottleTimer);
      scrollThrottleTimer = null;
    }
    if (nextPageDebounceTimer) {
      clearTimeout(nextPageDebounceTimer);
      nextPageDebounceTimer = null;
    }
  }

  let observedScrollParents = [];

  function attachScrollListeners(isSearchModeRef) {
    const scrollHandler = () => handleScroll(isSearchModeRef?.value);
    window.addEventListener("scroll", scrollHandler, {passive: true});
    document.addEventListener("scroll", scrollHandler, {passive: true});
    let el = mediaGridSection.value?.parentElement;
    while (el && el !== document.body && el !== document.documentElement) {
      try {
        const overflowY = window.getComputedStyle(el).overflowY;
        if (overflowY === "auto" || overflowY === "scroll") {
          el.addEventListener("scroll", scrollHandler, {passive: true});
          observedScrollParents.push(el);
        }
      } catch (_) {
      }
      el = el.parentElement;
    }
    return scrollHandler;
  }

  function detachScrollListeners(scrollHandler) {
    window.removeEventListener("scroll", scrollHandler);
    document.removeEventListener("scroll", scrollHandler);
    for (const parent of observedScrollParents) {
      try {
        parent.removeEventListener("scroll", scrollHandler);
      } catch (_) {
      }
    }
    observedScrollParents = [];
  }

  return {
    activeTab,
    loadingMedias,
    mediaItems,
    currentPage,
    hasMore,
    loadingMore,
    mediaGridSection,
    scrollSentinel,
    showFilterBar,
    mediaFilters,
    mediaFilterOptions,
    activeMediaFilterCount,
    resetMediaFilters,
    filteredMediaItems,
    loadRecommend,
    loadNextPage,
    checkAndTriggerNextPage,
    handleScroll,
    setupSentinelObserver,
    onTabChange,
    performSearch,
    cleanupTimers,
    attachScrollListeners,
    detachScrollListeners,
  };
}
