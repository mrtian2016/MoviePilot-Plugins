<template>
  <div class="cloud-resource-page">
    <TabsHeader
      :tabs="recommendTabs"
      :active-tab="activeTab"
      :search-mode="isSearchMode"
      :filter-visible="showFilterBar"
      :filter-count="activeMediaFilterCount"
      @change="onTabChange"
      @toggle-filter="showFilterBar = !showFilterBar" />

    <FilterBar
      v-model="mediaFilters"
      :visible="showFilterBar"
      :options="mediaFilterOptions"
      :active-count="activeMediaFilterCount"
      @reset="resetMediaFilters" />

    <div v-if="isSearchMode" class="search-tip-bar">
      <span>
        正在展示
        <strong>“{{ currentSearchText }}”</strong>
        的搜索结果
      </span>
      <v-btn variant="text" color="primary" size="small" prepend-icon="mdi-arrow-left" @click="exitSearchMode">
        返回推荐榜单
      </v-btn>
    </div>

    <MediaGrid
      ref="mediaGridSection"
      :items="filteredMediaItems"
      :all-items="mediaItems"
      :loading="loadingMedias"
      :loading-more="loadingMore"
      :has-more="hasMore"
      :search-mode="isSearchMode"
      @open="openMediaDetail"
      @load-more="loadNextPage(isSearchMode)"
      @exit-search="exitSearchMode"
      @sentinel="scrollSentinel = $event" />

    <DetailDialog
      v-model="detailVisible"
      v-model:active-detail-season="activeDetailSeason"
      v-model:active-channel-tab="activeChannelTab"
      v-model:active-resource-tab="activeResourceTab"
      v-model:resource-search-query="resourceSearchQuery"
      v-model:selected-resource-specs="selectedResourceSpecs"
      :active-resource-filter-count="activeResourceFilterCount"
      :reset-resource-filters="resetResourceFilters"
      :current-channel-resources="currentChannelResources"
      :fullscreen="isMobile"
      :active-media="activeMedia"
      :detail-loading="detailLoading"
      :active-media-seasons="activeMediaSeasons"
      :current-season-missing-episodes="currentSeasonMissingEpisodes"
      :available-channels="availableChannels"
      :channel-loading="channelLoading"
      :channel-searched="channelSearched"
      :current-channel-resource-tabs="currentChannelResourceTabs"
      :current-channel-filtered-resources="currentChannelFilteredResources"
      :raw-icons="RAW_ICONS"
      :downloading-index="downloadingIndex"
      :unlocking-key="unlockingKey"
      :previewing-key="previewingKey"
      :get-backdrop-style="getBackdropStyle"
      :handle-poster-error="handlePosterError"
      :copy-id-text="copyIdText"
      :get-library-summary="getLibrarySummary"
      :get-media-rating-info="getMediaRatingInfo"
      :get-rating-value="getRatingValue"
      :get-rating-text="getRatingText"
      :get-media-genres-list="getMediaGenresList"
      :on-channel-tab-change="onChannelTabChange"
      :get-channel-count="getChannelCount"
      :get-source-name="getSourceName"
      :search-channel="searchChannel"
      :res-key="resKey"
      :get-extracted-tags="getExtractedTags"
      :get-tag-color-class="getTagColorClass"
      :get-resource-size="getResourceSize"
      :open-unlock-dialog="openUnlockDialog"
      :can-preview-resource="canPreviewResource"
      :preview-resource-key="previewResourceKey"
      :preview-resource="previewResource"
      :copy-to-clipboard="copyToClipboard"
      :handle-quick-download="handleQuickDownload"
      :is-cross-transfer-resource="isCrossTransferResource"
      :open-cross-transfer-dialog="openCrossTransferDialog"
      :api="api"
      :plugin-id="pluginId"
      :show-message="showMessage" />

    <UnlockDialog
      v-model="unlockDialogVisible"
      :item="targetUnlockItem"
      :error="unlockError"
      :loading="unlocking"
      @confirm="doUnlockResource" />

    <PreviewDialog
      v-model="previewVisible"
      :loading="previewLoading"
      :error="previewError"
      :items="previewItems"
      :selected-items="selectedPreviewItems"
      :meta="previewMeta"
      :breadcrumbs="previewBreadcrumbs"
      :share-url="getPreviewShareUrl()"
      :all-selected="isAllPreviewSelected"
      :can-transfer="canTransferPreviewResource()"
      :is-selected="isItemSelected"
      @copy="copyToClipboard"
      @open-breadcrumb="openPreviewBreadcrumb"
      @open-folder="openPreviewFolder"
      @toggle-all="toggleSelectAllPreview"
      @toggle-item="toggleSelectPreviewItem"
      @single-transfer="handleSingleFileTransfer"
      @batch-transfer="handleBatchTransfer"
      @quick-transfer="handleQuickDownloadFromPreview" />

    <CrossTransferDialog
      v-model="crossDialogVisible"
      :source-name="crossSourceDriveName"
      :target="crossTargetDrive"
      :drives="selectableTargetDrives"
      :icon="getResourceTypeIcon(crossTargetDrive)"
      :loading="crossSubmitting"
      @update:target="crossTargetDrive = $event"
      @confirm="confirmCrossTransfer" />

    <v-snackbar v-model="snackbarVisible" :color="snackbarColor" location="top" timeout="2500">
      {{ snackbarText }}
    </v-snackbar>
  </div>
</template>

<script setup>
import {computed, nextTick, onMounted, onUnmounted, ref} from "vue";
import {useDisplay} from "vuetify";
import TabsHeader from "./resource/TabsHeader.vue";
import FilterBar from "./resource/FilterBar.vue";
import MediaGrid from "./resource/MediaGrid.vue";
import DetailDialog from "./resource/DetailDialog.vue";
import PreviewDialog from "./resource/PreviewDialog.vue";
import UnlockDialog from "./resource/UnlockDialog.vue";
import CrossTransferDialog from "./resource/CrossTransferDialog.vue";
import {useMediaDetail} from "../composables/useMediaDetail";
import {useMediaData} from "../composables/useMediaData";
import {useSnackbar} from "../composables/useSnackbar";
import {useSearchIntercept} from "../composables/useSearchIntercept";
import {useResourcePreview} from "../composables/useResourcePreview";

const props = defineProps({
  api: {type: Object, default: () => ({})},
  pluginId: {type: String, default: "PanSearch"},
  navKey: {type: String, default: "resource"},
});

const resourceApi = computed(() => props.api);
const resourcePluginId = computed(() => props.pluginId);
const {snackbarVisible, snackbarText, snackbarColor, showMessage} = useSnackbar();

const RAW_ICONS = {
  douban: `<svg class="icon" viewBox="0 0 1024 1024" version="1.1" xmlns="http://www.w3.org/2000/svg" width="18" height="18" data-rating-logo="douban" style="flex-shrink: 0;"><path d="M1003.52 872.448c0 72.38997333-58.68202667 131.072-131.072 131.072H151.552c-72.38997333 0-131.072-58.68202667-131.072-131.072V151.552c0-72.38997333 58.68202667-131.072 131.072-131.072h720.896c72.38997333 0 131.072 58.68202667 131.072 131.072v720.896z" fill="#00B51D"></path><path d="M822.59694933 184.32H201.40305067a11.501568 11.501568 0 0 0-11.501568 11.501568v46.01173333c0 6.35153067 5.15003733 11.501568 11.501568 11.501568h621.19389866a11.501568 11.501568 0 0 0 11.501568-11.501568v-46.01173333A11.501568 11.501568 0 0 0 822.59694933 184.32zM822.59694933 770.654208h-158.75549866l47.45352533-150.09928533h58.88955733a11.501568 11.501568 0 0 0 11.501568-11.501568V330.41066667a11.49610667 11.49610667 0 0 0-11.501568-11.501568h-516.36906666a11.501568 11.501568 0 0 0-11.501568 11.501568v278.642688c0 6.35153067 5.144576 11.501568 11.501568 11.501568h364.183552l-47.45898667 150.09928533h-131.23584l-34.799616-110.051328c-2.00430933-6.356992-8.781824-11.501568-15.13335467-11.501568H319.05655467c-6.35153067 0-9.87409067 5.144576-7.85885867 11.501568l34.79415467 110.051328H201.40305067a11.501568 11.501568 0 0 0-11.501568 11.501568v46.01719467c0 6.35153067 5.15003733 11.501568 11.501568 11.501568h621.19389866a11.501568 11.501568 0 0 0 11.501568-11.501568v-46.01719467a11.501568 11.501568 0 0 0-11.501568-11.501568z m-471.007232-233.82152533V402.620416c0-6.35153067 5.144576-11.501568 11.501568-11.501568h297.811968c6.35153067 0 11.501568 5.15549867 11.501568 11.501568v134.21226667a11.501568 11.501568 0 0 1-11.501568 11.501568H363.09128533a11.501568 11.501568 0 0 1-11.501568-11.501568z" fill="#FFFFFF"></path></svg>`,
  imdb: `<svg id="home_img" class="ipc-logo" xmlns="http://www.w3.org/2000/svg" width="36" height="18" viewBox="0 0 64 32" data-rating-logo="imdb" style="flex-shrink: 0;"><g fill="#F5C518"><rect x="0" y="0" width="100%" height="100%" rx="4"></rect></g><g transform="translate(8.000000, 7.000000)" fill="#000000" fill-rule="nonzero"><polygon points="0 18 5 18 5 0 0 0"></polygon><path d="M15.6725178,0 L14.5534833,8.40846934 L13.8582008,3.83502426 C13.65661,2.37009263 13.4632474,1.09175121 13.278113,0 L7,0 L7,18 L11.2416347,18 L11.2580911,6.11380679 L13.0436094,18 L16.0633571,18 L17.7583653,5.8517865 L17.7707076,18 L22,18 L22,0 L15.6725178,0 Z"></path><path d="M24,18 L24,0 L31.8045586,0 C33.5693522,0 35,1.41994415 35,3.17660424 L35,14.8233958 C35,16.5777858 33.5716617,18 31.8045586,18 L24,18 Z M29.8322479,3.2395236 C29.6339219,3.13233348 29.2545158,3.08072342 28.7026524,3.08072342 L28.7026524,14.8914865 C29.4312846,14.8914865 29.8796736,14.7604764 30.0478195,14.4865461 C30.2159654,14.2165858 30.3021941,13.486105 30.3021941,12.2871637 L30.3021941,5.3078959 C30.3021941,4.49404499 30.272014,3.97397442 30.2159654,3.74371416 C30.1599168,3.5134539 30.0348852,3.34671372 29.8322479,3.2395236 Z"></path><path d="M44.4299079,4.50685823 L44.749518,4.50685823 C46.5447098,4.50685823 48,5.91267586 48,7.64486762 L48,14.8619906 C48,16.5950653 46.5451816,18 44.749518,18 L44.4299079,18 C43.3314617,18 42.3602746,17.4736618 41.7718697,16.6682739 L41.4838962,17.7687785 L37,17.7687785 L37,0 L41.7843263,0 L41.7843263,5.78053556 C42.4024982,5.01015739 43.3551514,4.50685823 44.4299079,4.50685823 Z M43.4055679,13.2842155 L43.4055679,9.01907814 C43.4055679,8.31433946 43.3603268,7.85185468 43.2660746,7.63896485 C43.1718224,7.42607505 42.7955881,7.2893916 42.5316822,7.2893916 C42.267776,7.2893916 41.8607934,7.40047379 41.7816216,7.58767002 L41.7816216,9.01907814 L41.7816216,13.4207851 L41.7816216,14.8074788 C41.8721037,15.0130276 42.2602358,15.1274059 42.5316822,15.1274059 C42.8031285,15.1274059 43.1982131,15.0166981 43.281155,14.8074788 C43.3640968,14.5982595 43.4055679,14.0880581 43.4055679,13.2842155 Z"></path></g></svg>`,
  tmdb: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 273.42 35.52" width="38" height="18" data-rating-logo="tmdb" style="flex-shrink: 0;"><defs><linearGradient id="tmdb-grad" y1="17.76" x2="273.42" y2="17.76" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#90cea1"></stop><stop offset="0.56" stop-color="#3cbec9"></stop><stop offset="1" stop-color="#00b3e5"></stop></linearGradient></defs><path fill="url(#tmdb-grad)" d="M191.85,35.37h63.9A17.67,17.67,0,0,0,273.42,17.7h0A17.67,17.67,0,0,0,255.75,0h-63.9A17.67,17.67,0,0,0,174.18,17.7h0A17.67,17.67,0,0,0,191.85,35.37ZM10.1,35.42h7.8V6.92H28V0H0v6.9H10.1Zm28.1,0H46V8.25h.1L55.05,35.4h6L70.3,8.25h.1V35.4h7.8V0H66.45l-8.2,23.1h-.1L50,0H38.2ZM89.14.12h11.7a33.56,33.56,0,0,1,8.08,1,18.52,18.52,0,0,1,6.67,3.08,15.09,15.09,0,0,1,4.53,5.52,18.5,18.5,0,0,1,1.67,8.25,16.91,16.91,0,0,1-1.62,7.58,16.3,16.3,0,0,1-4.38,5.5,19.24,19.24,0,0,1-6.35,3.37,24.53,24.53,0,0,1-7.55,1.15H89.14Zm7.8,28.2h4a21.66,21.66,0,0,0,5-.55A10.58,10.58,0,0,0,110,26a8.73,8.73,0,0,0,2.68-3.35,11.9,11.9,0,0,0,1-5.08,9.87,9.87,0,0,0-1-4.52,9.17,9.17,0,0,0-2.63-3.18A11.61,11.61,0,0,0,106.22,8a17.06,17.06,0,0,0-4.68-.63h-4.6ZM133.09.12h13.2a32.87,32.87,0,0,1,4.63.33,12.66,12.66,0,0,1,4.17,1.3,7.94,7.94,0,0,1,3,2.72,8.34,8.34,0,0,1,1.15,4.65,7.48,7.48,0,0,1-1.67,5,9.13,9.13,0,0,1-4.43,2.82V17a10.28,10.28,0,0,1,3.18,1,8.51,8.51,0,0,1,2.45,1.85,7.79,7.79,0,0,1,1.57,2.62,9.16,9.16,0,0,1,.55,3.2,8.52,8.52,0,0,1-1.2,4.68,9.32,9.32,0,0,1-3.1,3A13.38,13.38,0,0,1,152.32,35a22.5,22.5,0,0,1-4.73.5h-14.5Zm7.8,14.15h5.65a7.65,7.65,0,0,0,1.78-.2,4.78,4.78,0,0,0,1.57-.65,3.43,3.43,0,0,0,1.13-1.2,3.63,3.63,0,0,0,.42-1.8A3.3,3.3,0,0,0,151,8.6a3.42,3.42,0,0,0-1.23-1.13A6.07,6.07,0,0,0,148,6.9a9.9,9.9,0,0,0-1.85-.18h-5.3Zm0,14.65h7a8.27,8.27,0,0,0,1.83-.2,4.67,4.67,0,0,0,1.67-.7,3.93,3.93,0,0,0,1.23-1.3,3.8,3.8,0,0,0,.47-1.95,3.16,3.16,0,0,0-.62-2,4,4,0,0,0-1.58-1.18,8.23,8.23,0,0,0-2-.55,15.12,15.12,0,0,0-2.05-.15h-5.9Z"></path></svg>`,
};

function formatVoteCount(count) {
  if (!count && count !== 0) return "";
  const num = Number(count);
  if (!num || num <= 0) return "";
  if (num >= 10000) {
    return `(${(num / 10000).toFixed(1).replace(/\.0$/, "")}w)`;
  }
  if (num >= 1000) {
    return `(${(num / 1000).toFixed(1).replace(/\.0$/, "")}k)`;
  }
  return `(${num})`;
}

function getMediaRatingInfo(item, platform) {
  if (!item) return null;
  if (platform === "tmdb") {
    const rawScore =
      item.tmdb_rating ??
      (item.media_source === "themoviedb" || (!item.imdb_rating && !item.douban_rating && !item.douban_id)
        ? (item.vote_average ?? item.rating)
        : null);
    const val = Number(rawScore);
    if (!Number.isFinite(val) || val <= 0) return null;
    return {
      score: `${val.toFixed(1)}/10`,
      votes: formatVoteCount(item.tmdb_vote_count ?? item.vote_count),
    };
  }
  if (platform === "imdb") {
    const rawScore = item.imdb_rating ?? (item.media_source === "imdb" ? (item.vote_average ?? item.rating) : null);
    const val = Number(rawScore);
    if (!Number.isFinite(val) || val <= 0) return null;
    return {
      score: `${val.toFixed(1)}/10`,
      votes: formatVoteCount(item.imdb_vote_count),
    };
  }
  if (platform === "douban") {
    const rawScore =
      item.douban_rating ??
      (item.douban_id || item.media_source === "douban" ? (item.vote_average ?? item.rating) : null);
    const val = Number(rawScore);
    if (!Number.isFinite(val) || val <= 0) return null;
    return {
      score: `${val.toFixed(1)}/10`,
      votes: formatVoteCount(item.douban_vote_count),
    };
  }
  return null;
}

const TMDB_GENRES = {
  28: "动作",
  12: "冒险",
  16: "动画",
  35: "喜剧",
  80: "犯罪",
  99: "纪录",
  18: "剧情",
  10751: "家庭",
  14: "奇幻",
  36: "历史",
  27: "恐怖",
  10402: "音乐",
  9648: "悬疑",
  10749: "爱情",
  878: "科幻",
  10770: "电视电影",
  53: "惊悚",
  10752: "战争",
  37: "西部",
  10759: "动作冒险",
  10762: "儿童",
  10763: "新闻",
  10764: "真人秀",
  10765: "科幻奇幻",
  10766: "肥皂剧",
  10767: "脱口秀",
  10768: "战争政治",
};

function getMediaGenresList(item) {
  if (!item) return [];
  const labels = mediaGenres(item);
  if (labels && labels.length) return labels;
  if (Array.isArray(item.genre_ids)) {
    const list = item.genre_ids.map((id) => TMDB_GENRES[id] || id).filter(Boolean);
    if (list.length) return list;
  }
  if (Array.isArray(item.tags)) return item.tags;
  return [];
}

const display = useDisplay();
const isMobile = computed(() => display.mobile?.value || display.smAndDown?.value || display.xs?.value);

const activePluginConfig = ref({cloud_drive: "115"});
const searchInputRef = ref(null);

const recommendTabs = [
  {value: "tmdb_trending", title: "流行趋势", icon: "mdi-fire"},
  {value: "douban_movie_showing", title: "正在热映", icon: "mdi-movie-open"},
  {value: "douban_movie_hot", title: "热门电影", icon: "mdi-filmstrip"},
  {value: "douban_tv_hot", title: "热门剧集", icon: "mdi-television-classic"},
  {value: "bangumi_calendar", title: "每日放送", icon: "mdi-calendar-star"},
  {value: "anilist_popular", title: "本季新番", icon: "mdi-star-face"},
];
const {
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
  filteredMediaItems: filterMediaItems,
  loadRecommend,
  loadNextPage,
  checkAndTriggerNextPage,
  handleScroll,
  setupSentinelObserver,
  onTabChange: changeMediaTab,
  performSearch,
  cleanupTimers,
} = useMediaData({api: resourceApi, pluginId: resourcePluginId, showMessage});
const {
  isSearchMode,
  searchKeyword,
  currentSearchText,
  searching,
  handleSearch,
  exitSearchMode,
  clearSearch,
  setup: setupTopSearchInterceptor,
  cleanup: cleanupTopSearchInterceptor,
} = useSearchIntercept({
  onSearch: performSearch,
  onExit: () => {
    currentPage.value = 1;
    hasMore.value = true;
    loadRecommend(activeTab.value, 1, false);
  },
});
const {
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
  currentChannelResources,
  channelLoading,
  channelSearched,
  currentChannelResourceTabs,
  currentChannelFilteredResources,
  openMediaDetail,
  searchChannel,
  onChannelTabChange,
  getChannelCount,
  closeMediaDetail,
} = useMediaDetail({
  api: resourceApi,
  pluginId: resourcePluginId,
  pluginConfig: activePluginConfig,
  showMessage,
});
const downloadingIndex = ref(-1);
const {
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
} = useResourcePreview({
  api: resourceApi,
  pluginId: resourcePluginId,
  submitDownload: submitManualDownload,
  requestCrossTransfer: openCrossTransferDialog,
  isCrossTransfer: isCrossTransferResource,
  showMessage,
});
const unlockDialogVisible = ref(false);
const targetUnlockItem = ref(null);
const targetUnlockIndex = ref(-1);
const unlocking = ref(false);
const unlockError = ref("");
const unlockingKey = ref("");

function isPointUnlockResource(res) {
  const source = String(res?.source || "").toLowerCase();
  return ["hdhive", "dian115"].includes(source);
}

function getResourcePointStatus(res) {
  if (res?.need_unlock && Number(res.unlock_points || 0) > 0) {
    return {
      label: `${Number(res.unlock_points || 0)} 积分`,
      color: "warning",
    };
  }
  if (isPointUnlockResource(res) && Number(res?.unlock_points || 0) === 0) {
    return {label: "免费", color: "success"};
  }
  if (res?.need_access) {
    return {label: "需获取", color: "info"};
  }
  return null;
}

function openUnlockDialog(res, idx) {
  targetUnlockItem.value = res;
  targetUnlockIndex.value = idx;
  unlockError.value = "";
  unlockDialogVisible.value = true;
}

async function unlockResource(item, index = 0) {
  if (!item || unlocking.value) return false;
  unlocking.value = true;
  unlockError.value = "";
  unlockingKey.value = resKey(item, index);
  try {
    const payload = {
      source: item.source || activeChannelTab.value,
      item,
    };
    const response = await props.api.post(`plugin/${props.pluginId}/search/unlock`, payload);
    const res = unwrapApiResponse(response);
    if (!res?.success) {
      throw new Error(res?.message || "资源解锁失败");
    }
    const data = res.data?.data || res.data || {};
    item.url = data.download_url || data.url || item.url;
    item.need_unlock = false;
    item.is_unlocked = true;
    unlockDialogVisible.value = false;
    showMessage(res.message || "资源解锁成功，已获取直接链接", "success");
    return true;
  } catch (err) {
    unlockError.value = err.response?.data?.message || err.message || String(err);
    showMessage(unlockError.value, "error");
    return false;
  } finally {
    unlocking.value = false;
    unlockingKey.value = "";
  }
}

async function doUnlockResource() {
  const item = targetUnlockItem.value;
  if (!item || unlocking.value) return;
  await unlockResource(item, targetUnlockIndex.value);
}

function copyIdText(type, val) {
  if (!val) return;
  copyToClipboard(String(val));
  showMessage(`已复制 ${type} ID: ${val}`);
}

function mediaItemKey(item) {
  return `${item.media_type || "m"}_${item.media_id || item.tmdb_id || item.douban_id || item.bangumi_id || item.anilist_id || item.title}_${item.year || ""}`;
}

function resKey(res, idx) {
  return `${res.source || "s"}_${res.url || res.title || idx}_${idx}`;
}

function getRatingText(item) {
  return getRatingValue(item).toFixed(1);
}

function getRatingValue(item) {
  const raw = item?.vote_average ?? item?.rating ?? 0;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

function formatBytes(bytes) {
  const num = Number(bytes || 0);
  if (!num || num < 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let val = num;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(2)} ${units[i]}`;
}

function getResourceSize(resource) {
  const formatted = String(resource?.size_formatted || "").trim();
  if (formatted && formatted !== "0") return formatted;
  return formatBytes(resource?.size ?? resource?.size_bytes);
}

function getBackdropStyle(media) {
  const url = String(media?.backdrop_url || media?.poster_url || "").trim();
  return url ? `url(${url})` : "none";
}

function handlePosterError(item) {
  if (!item || item._fallbackTried) {
    if (item) item._imgFailed = true;
    return;
  }
  item._fallbackTried = true;
  item._currentPoster = item.poster_url || "";
}

function getLibraryEpisodesCount(media) {
  if (!media) return 0;
  if (typeof media.library_episodes_total === "number") {
    return media.library_episodes_total;
  }
  return (media.seasons || []).reduce((acc, s) => acc + (s.library_episodes_count || 0), 0);
}

const activeMediaSeasons = computed(() => {
  const seasons = activeMedia.value?.seasons || [];
  return seasons.filter((season) => {
    const episodes = Array.isArray(season?.episodes) ? season.episodes : [];
    return episodes.some((episode) => !episode?.in_library);
  });
});
const currentSeasonEpisodes = computed(() => {
  const seasons = activeMediaSeasons.value;
  if (!seasons.length) return [];
  const found = seasons.find((s) => s.season_number === activeDetailSeason.value);
  return (found || seasons[0]).episodes || [];
});
const currentSeasonMissingEpisodes = computed(() => {
  return currentSeasonEpisodes.value.filter((e) => !e.in_library);
});

function getTagColorClass(tag) {
  const t = String(tag || "").toUpperCase();
  if (t.includes("4K") || t.includes("2160") || t.includes("UHD")) return "tag-4k";
  if (t.includes("1080") || t.includes("FHD")) return "tag-1080p";
  if (t.includes("720")) return "tag-720p";
  if (t.includes("ISO") || t.includes("原盘") || t.includes("BLURAY") || t.includes("REMUX")) return "tag-remux";
  if (t.includes("杜比视界") || t.includes("DV") || t.includes("VISION")) return "tag-dv";
  if (t.includes("HDR")) return "tag-hdr";
  if (t.includes("WEB")) return "tag-web";
  if (t.includes("中字") || t.includes("简繁") || t.includes("内嵌") || t.includes("双语")) return "tag-sub";
  if (t.includes("国语") || t.includes("国配") || t.includes("粤语")) return "tag-dub";
  if (t.includes("全景声") || t.includes("ATMOS")) return "tag-atmos";
  if (t.includes("60帧") || t.includes("120帧") || t.includes("FPS")) return "tag-fps";
  return "tag-generic";
}

function getLibrarySummary(media) {
  const names = (media?.library_items || [])
    .map((item) => item?.library || item?.title || item?.path || "")
    .map((value) => String(value).trim())
    .filter(Boolean);
  return [...new Set(names)].slice(0, 3).join("、");
}

function getSourceColor(source) {
  const map = {
    hdhive: "amber-darken-1",
    piratebay: "teal",
    uindex: "blue",
    seedhub: "deep-purple",
    pansou: "indigo",
    juying: "orange",
    pinglian: "cyan",
    dian115: "amber-darken-2",
  };
  return map[String(source).toLowerCase()] || "grey";
}

function getSourceName(source) {
  const map = {
    pansou: "PanSou",
    hdhive: "HDHive",
    dian115: "Dian115",
    juying: "聚影",
    seedhub: "SeedHub",
    pinglian: "盘链",
    piratebay: "海盗湾",
    uindex: "UIndex",
    online_docs: "在线文档",
  };
  return map[String(source).toLowerCase()] || source || "未知";
}

function getTypeColor(type) {
  const map = {
    115: "primary",
    quark: "amber-darken-3",
    alipan: "blue",
    baidu: "indigo",
    uc: "deep-orange",
    tianyi: "teal",
    123: "purple",
    xunlei: "light-blue-darken-1",
    magnet: "red-darken-1",
    ed2k: "blue-grey-darken-1",
    pikpak: "deep-purple",
  };
  return map[String(type || "").toLowerCase()] || "blue-grey";
}

function getResourceTypeName(type) {
  const map = {
    115: "115网盘",
    quark: "夸克网盘",
    alipan: "阿里云盘",
    baidu: "百度网盘",
    uc: "UC网盘",
    tianyi: "天翼云盘",
    123: "123网盘",
    xunlei: "迅雷网盘",
    magnet: "磁力链接",
    ed2k: "电驴链接",
    torrent: "BT种子",
    pikpak: "PikPak",
    guangya: "光鸭网盘",
  };
  const key = String(type || "").toLowerCase();
  return map[key] || (type ? String(type).toUpperCase() : "未知类型");
}

function getResourceTabIcon(type) {
  const map = {
    all: "mdi-apps",
    115: "mdi-cloud",
    quark: "mdi-cloud-outline",
    alipan: "mdi-cloud-sync-outline",
    baidu: "mdi-cloud-circle-outline",
    uc: "mdi-cloud-download-outline",
    tianyi: "mdi-cloud-check-outline",
    123: "mdi-cloud-refresh-outline",
    guangya: "mdi-cloud-outline",
    xunlei: "mdi-flash",
    magnet: "mdi-magnet",
    ed2k: "mdi-link-variant",
    torrent: "mdi-seed",
    pikpak: "mdi-cloud-upload-outline",
  };
  return map[String(type || "").toLowerCase()] || "mdi-folder-outline";
}

function getResourceTypeIcon(type) {
  return getResourceTabIcon(type);
}

function isCrossTransferResource(res) {
  const t = String(res?.resource_type || "").toLowerCase();
  const currentMainDrive = String(activePluginConfig.value?.cloud_drive || "115").toLowerCase();
  if (!t || ["magnet", "ed2k", "cloud"].includes(t)) return false;
  return t !== currentMainDrive;
}

const crossDialogVisible = ref(false);
const crossTargetDrive = ref("115");
const crossPendingAction = ref(null);
const crossSubmitting = ref(false);
const crossSourceDriveName = ref("");
const crossSourceDriveKey = ref("");

const selectableTargetDrives = computed(() => {
  return (availableDrives.value || []).filter(
    (drive) => String(drive?.key || "").toLowerCase() !== crossSourceDriveKey.value,
  );
});

function openCrossTransferDialog(res, actionOrIndex) {
  const src = String(res?.resource_type || "").toLowerCase();
  crossSourceDriveKey.value = src;
  crossSourceDriveName.value = getResourceTypeName(src) || "异构网盘";
  const mainDrive = String(activePluginConfig.value?.cloud_drive || "115").toLowerCase();
  if (src !== mainDrive) {
    crossTargetDrive.value = mainDrive;
  } else {
    const candidate = (availableDrives.value || []).find((d) => d.key !== src);
    crossTargetDrive.value = candidate?.key || "115";
  }

  if (typeof actionOrIndex === "function") {
    crossPendingAction.value = actionOrIndex;
  } else {
    crossPendingAction.value = async (targetCloud) => {
      downloadingIndex.value = typeof actionOrIndex === "number" ? actionOrIndex : -1;
      try {
        await submitManualDownload(res.url, res.title, res, {
          target_cloud: targetCloud,
          is_cross: true,
        });
      } finally {
        downloadingIndex.value = -1;
      }
    };
  }
  crossDialogVisible.value = true;
}

async function confirmCrossTransfer() {
  if (!crossTargetDrive.value) {
    showMessage("请选择目标网盘", "warning");
    return;
  }
  crossSubmitting.value = true;
  try {
    if (crossPendingAction.value) {
      await crossPendingAction.value(crossTargetDrive.value);
    }
    crossDialogVisible.value = false;
  } catch (err) {
    showMessage(`跨盘转存失败: ${err.message || err}`, "error");
  } finally {
    crossSubmitting.value = false;
  }
}

function getExtractedTags(res) {
  const specs = [];
  const rawTitle = String(res.title || "");
  const title = rawTitle.toUpperCase();
  if (/(?:\b|\[|\.)(?:4K|2160P|UHD)(?:\b|\]|\.)/i.test(title)) {
    specs.push("4K");
  } else if (/(?:\b|\[|\.)(?:1080P|1080I|FHD)(?:\b|\]|\.)/i.test(title)) {
    specs.push("1080P");
  } else if (/(?:\b|\[|\.)(?:720P)(?:\b|\]|\.)/i.test(title)) {
    specs.push("720P");
  }
  if (/\.ISO\b/i.test(title) || /\[\d+(?:\.\d+)?GB\]\.ISO/i.test(title)) {
    specs.push("原盘ISO");
  } else if (/REMUX/i.test(title)) {
    specs.push("REMUX");
  } else if (/(?:\b|\[|\.)(?:BDMV|BLURAY|BLU-RAY)(?:\b|\]|\.)/i.test(title)) {
    specs.push("BluRay");
  } else if (/WEB-DL|WEBDL|WEB-RIP/i.test(title)) {
    specs.push("WEB-DL");
  }
  if (/DV|DOLBY\s*VISION|杜比视界/i.test(title)) specs.push("杜比视界");
  if (/HDR10\+/i.test(title)) specs.push("HDR10+");
  else if (/(?:\b|\[|\.)HDR10?(?:\b|\]|\.)/i.test(title)) specs.push("HDR");
  if (/60FPS|60帧/i.test(title)) specs.push("60帧");
  else if (/120FPS|120帧/i.test(title)) specs.push("120帧");
  const languageTag = title.match(/内封(?:简繁英|简繁中英|简繁|简中|繁中|中文|英语|英文)/i);
  if (languageTag) specs.push(languageTag[0]);
  else if (/中字|内嵌|简繁|双语|中英|\bCHS\b|\bCHT\b/i.test(title)) specs.push("中字");
  if (/国语|国配|国粤/i.test(title)) specs.push("国语");
  if (/粤语/i.test(title)) specs.push("粤语");
  if (/ATMOS|全景声/i.test(title)) specs.push("杜比全景声");
  return specs;
}

function metadataLabels(value) {
  if (Array.isArray(value)) {
    return value.flatMap((item) => metadataLabels(item));
  }
  if (value && typeof value === "object") {
    return [value.name || value.label || value.value || value.iso_3166_1 || value.iso_639_1]
      .filter(Boolean)
      .map((item) => String(item).trim());
  }
  return value
    ? String(value)
      .split(/[\/|,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean)
    : [];
}

function mediaCategory(item) {
  const value = metadataLabels(item?.category || item?.metadata_category || item?.media_category);
  return value[0] || (item?.media_type === "tv" ? "剧集" : "电影");
}

function mediaGenres(item) {
  const parsed = metadataLabels(item?.genres || item?.genre || item?.types);
  if (parsed && parsed.length) return parsed;
  if (Array.isArray(item?.genre_ids)) {
    return item.genre_ids.map((id) => TMDB_GENRES[id]).filter(Boolean);
  }
  return [];
}

function mediaRegions(item) {
  return metadataLabels(item?.regions || item?.origin_country || item?.countries || item?.production_countries);
}

function mediaDisplayLabels(item) {
  const mediaTypeLabel = item?.media_type === "tv" ? "剧集" : "电影";
  return [
    ...new Set(
      [mediaCategory(item), ...mediaRegions(item), ...mediaGenres(item)].filter(
        (value) => value && value !== mediaTypeLabel,
      ),
    ),
  ];
}

const mediaFilterTranslations = {
  category: {
    movie: "电影",
    movies: "电影",
    film: "电影",
    tv: "剧集",
    television: "剧集",
    series: "剧集",
    anime: "动漫",
    animation: "动漫",
    documentary: "纪录片",
    variety: "综艺",
  },
  type: {
    action: "动作",
    adventure: "冒险",
    animation: "动画",
    comedy: "喜剧",
    crime: "犯罪",
    documentary: "纪录片",
    drama: "剧情",
    family: "家庭",
    fantasy: "奇幻",
    history: "历史",
    horror: "恐怖",
    music: "音乐",
    mystery: "悬疑",
    romance: "爱情",
    "science fiction": "科幻",
    "sci-fi": "科幻",
    "tv movie": "电视电影",
    thriller: "惊悚",
    war: "战争",
    western: "西部",
    reality: "真人秀",
    news: "新闻",
    kids: "儿童",
    talk: "脱口秀",
  },
  region: {
    china: "中国大陆",
    "mainland china": "中国大陆",
    "hong kong": "中国香港",
    taiwan: "中国台湾",
    japan: "日本",
    "south korea": "韩国",
    korea: "韩国",
    "united states": "美国",
    usa: "美国",
    "united kingdom": "英国",
    uk: "英国",
    france: "法国",
    germany: "德国",
    india: "印度",
    thailand: "泰国",
    canada: "加拿大",
    australia: "澳大利亚",
  },
};

function mediaFilterTitle(value, group) {
  const text = String(value || "").trim();
  if (!text || text === "全部" || /[\u3400-\u9fff]/.test(text)) return text;
  const translated = mediaFilterTranslations[group]?.[text.toLowerCase()];
  if (translated) return translated;
  if (group === "region" && /^[A-Za-z]{2}$/.test(text)) {
    try {
      return new Intl.DisplayNames(["zh-CN"], {type: "region"}).of(text.toUpperCase()) || text;
    } catch {
      return text;
    }
  }
  return text;
}

const filteredMediaItems = computed(() => filterMediaItems(isSearchMode.value));

function getNormalizedResourceType(item) {
  const raw = String(item?.resource_type || item?.pan_type || "")
    .toLowerCase()
    .trim();
  const aliases = {
    aliyun: "alipan",
    ali: "alipan",
    "115pan": "115",
    "123pan": "123",
    magnetlink: "magnet",
  };
  return aliases[raw] || raw || "other";
}

function unwrapApiResponse(response) {
  if (response?.success !== undefined) return response;
  if (response?.data?.success !== undefined) return response.data;
  return response || {};
}

function responseItems(response) {
  const result = unwrapApiResponse(response);
  const candidates = [result, result?.data, result?.data?.data, response, response?.data, response?.data?.data];
  for (const candidate of candidates) {
    if (Array.isArray(candidate?.items)) return candidate.items;
    if (Array.isArray(candidate)) return candidate;
  }
  return [];
}

function prepareMediaItems(items) {
  return Array.isArray(items) ? items : [];
}

function onTabChange(val) {
  const wasSearchMode = isSearchMode.value;
  isSearchMode.value = false;
  changeMediaTab(val, wasSearchMode);
}

async function copyToClipboard(text) {
  if (!text) return;
  try {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch (_) {
      }
    }
    if (!copied) {
      const input = document.createElement("textarea");
      input.value = text;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.focus();
      input.select();
      document.execCommand("copy");
      document.body.removeChild(input);
    }
    showMessage("已成功复制链接到剪贴板！");
  } catch (e) {
    showMessage("复制失败，请手动复制", "warning");
  }
}

function extractSeasonFromTitle(text) {
  if (!text) return null;
  const match = String(text).match(/(?:S|Season|第)\s*(\d{1,2})(?:季|\b)/i);
  if (match && match[1]) {
    const s = parseInt(match[1], 10);
    if (s > 0 && s <= 999) return s;
  }
  return null;
}

async function submitManualDownload(url, title, resItem = null, options = {}) {
  const normalizedUrl = String(url || "").trim();
  const source = String(resItem?.source || previewSource.value || activeChannelTab.value || "manual")
    .trim()
    .toLowerCase();
  const resourceType = String(resItem?.resource_type || previewResourceType.value || "cloud")
    .trim()
    .toLowerCase();
  const resourceRef = String(resItem?.resource_ref || "").trim();
  const media = activeMedia.value || {};
  const mediaType = media.media_type === "movie" ? "movie" : "tv";
  const mediaTitle = media.title || title || "未命名媒体";
  let tmdbId = Number(media.tmdb_id || 0);
  if (!tmdbId && mediaTitle) {
    try {
      const tmdbRes = await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
        title: mediaTitle,
        media_type: mediaType,
      });
      const candidates = tmdbRes?.data?.items || tmdbRes?.data || [];
      if (Array.isArray(candidates) && candidates.length) {
        const matched = candidates[0];
        tmdbId = Number(matched.tmdb_id || 0);
        if (tmdbId) {
          media.tmdb_id = tmdbId;
        }
      }
    } catch (_) {
    }
  }
  let seasons = [];
  if (mediaType === "tv") {
    if (activeDetailSeason.value && Number(activeDetailSeason.value) > 0) {
      seasons = [Number(activeDetailSeason.value)];
    } else {
      const explicitSeason = resItem?.target_season || extractSeasonFromTitle(resItem?.title || title);
      if (explicitSeason) {
        seasons = [explicitSeason];
      } else if (Array.isArray(media.seasons) && media.seasons.length) {
        seasons = media.seasons
          .map((s) => (typeof s === "object" && s !== null ? Number(s.season_number) : Number(s)))
          .filter((v) => v > 0);
      }
      if (!seasons.length) {
        seasons = [1];
      }
    }
  }
  const finalResourceTitle = resItem?.title || title || mediaTitle;

  const isCross = options.is_cross !== undefined ? options.is_cross : isCrossTransferResource(resItem);
  const actionName = isCross ? "跨盘转存" : "转存";
  const targetCloud = options.target_cloud || "";
  const resourcePayload = {
    ...(resItem || {}),
    ...(normalizedUrl ? {url: normalizedUrl} : {}),
    title: finalResourceTitle,
    resource_type: resourceType,
    source,
    resource_ref: resourceRef,
    provider_data: resItem?.provider_data || {},
    target_cloud: targetCloud,
    is_cross: isCross,
  };
  const payload = {
    media: {
      tmdb_id: tmdbId > 0 ? tmdbId : null,
      imdb_id: media.imdb_id || resItem?.imdb_id || null,
      tvdb_id: media.tvdb_id || resItem?.tvdb_id || null,
      douban_id: media.douban_id || resItem?.douban_id || null,
      bangumi_id: media.bangumi_id || resItem?.bangumi_id || null,
      anilist_id: media.anilist_id || resItem?.anilist_id || null,
      anidb_id: media.anidb_id || resItem?.anidb_id || null,
      media_source: media.media_source || null,
      media_id: media.media_id || null,
      media_type: mediaType,
      title: mediaTitle,
      year: media.year || "",
      seasons: seasons,
    },
    resource_links: [],
    resource_titles: {},
    resources: [resourcePayload],
    cross_transfer: isCross,
    allow_cross_transfer: true,
    target_cloud: targetCloud,
    manual_upgrade: false,
    skip_history: false,
  };

  const response = await props.api.post(`plugin/${props.pluginId}/sync/manual`, payload);
  if (response?.success) {
    showMessage(response.message || `已成功添加${actionName}并开启自动刮削整理！`, "success");
  } else {
    showMessage(response?.message || `${actionName}任务提交失败`, "error");
  }
}

let isSubmittingManualDownload = false;

async function handleQuickDownload(res, index) {
  if (isSubmittingManualDownload || downloadingIndex.value !== -1) {
    return;
  }
  isSubmittingManualDownload = true;
  downloadingIndex.value = index;
  try {
    if (res?.need_unlock && !(await unlockResource(res, index))) {
      return;
    }
    await submitManualDownload(res.url, res.title, res);
  } catch (err) {
    showMessage(`提交下载失败: ${err.message || err}`, "error");
  } finally {
    downloadingIndex.value = -1;
    setTimeout(() => {
      isSubmittingManualDownload = false;
    }, 600);
  }
}

async function loadPluginConfig() {
  try {
    const res = await props.api.get(`plugin/${props.pluginId}`);
    let config = res?.data?.config || res?.config || {};
    try {
      const options = await props.api.get(`plugin/${props.pluginId}/ui_options?scope=search`);
      const optionData = options?.data?.data || options?.data || options || {};
      if (optionData?.search_accounts) {
        config = {...config, search_accounts: optionData.search_accounts};
      }
    } catch (_) {
    }
    activePluginConfig.value = config;
  } catch (_) {
  }
}

function handleGlobalKeydown(e) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    searchInputRef.value?.focus?.();
  }
}

let observedScrollParents = [];

function attachScrollListeners() {
  window.addEventListener("scroll", handleScroll, {passive: true});
  document.addEventListener("scroll", handleScroll, {passive: true});

  let el = (mediaGridSection.value?.$el || mediaGridSection.value)?.parentElement;
  while (el && el !== document.body && el !== document.documentElement) {
    try {
      const overflowY = window.getComputedStyle(el).overflowY;
      if (overflowY === "auto" || overflowY === "scroll") {
        el.addEventListener("scroll", handleScroll, {passive: true});
        observedScrollParents.push(el);
      }
    } catch (_) {
    }
    el = el.parentElement;
  }
}

function detachScrollListeners() {
  window.removeEventListener("scroll", handleScroll);
  document.removeEventListener("scroll", handleScroll);
  for (const parent of observedScrollParents) {
    try {
      parent.removeEventListener("scroll", handleScroll);
    } catch (_) {
    }
  }
  observedScrollParents = [];
}

onMounted(() => {
  loadPluginConfig();
  loadRecommend(activeTab.value);
  setupTopSearchInterceptor();
  nextTick(() => {
    setupSentinelObserver();
    attachScrollListeners();
  });
  window.addEventListener("keydown", handleGlobalKeydown);
});

onUnmounted(() => {
  cleanupTopSearchInterceptor();
  cleanupTimers();
  detachScrollListeners();
  window.removeEventListener("keydown", handleGlobalKeydown);
});
</script>

<style scoped>
.cloud-resource-page {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 0 1.5rem 0;
  width: 100%;
  min-height: 100%;
  max-width: none;
  margin: 0;
  box-sizing: border-box;
}

.resource-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  flex-wrap: wrap;
  padding: 18px 24px;
  background: var(--glass-surface, rgba(var(--v-theme-surface), 0.85));
  backdrop-filter: var(--glass-surface-backdrop-filter, blur(12px));
  -webkit-backdrop-filter: var(--glass-surface-backdrop-filter, blur(12px));
  border-radius: 16px;
  border: 1px solid var(--glass-border, rgba(var(--v-border-color), 0.12));
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.04);
  flex: 0 0 auto;
}

.header-identity {
  display: flex;
  align-items: center;
  gap: 14px;
}

.header-avatar {
  border-radius: 12px;
}

.header-title {
  font-size: 1.25rem;
  font-weight: 700;
  margin: 0;
  line-height: 1.3;
}

.header-subtitle {
  font-size: 0.8rem;
  color: rgba(var(--v-theme-on-surface), 0.65);
  margin: 2px 0 0 0;
}

.header-search {
  flex-grow: 1;
  max-width: 480px;
  min-width: 280px;
}

.search-input {
  border-radius: 12px;
}

.search-tip-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(var(--v-theme-primary), 0.1);
  padding: 10px 16px;
  border-radius: 10px;
  font-size: 0.875rem;
  flex: 0 0 auto;
}

@media (max-width: 600px) {
  .cloud-resource-page {
    padding: 0 4px 16px 4px !important;
    gap: 6px !important;
    width: 100% !important;
    min-height: 100% !important;
  }

  .tabs-container {
    min-height: 44px !important;
    padding: 2px 4px !important;
    margin-top: 0 !important;
    margin-bottom: 6px !important;
    gap: 6px !important;
  }

  .header-tabs {
    margin-inline-start: 0 !important;
  }

  .header-tab {
    padding: 4px 10px !important;
    font-size: 0.82rem !important;
  }

  .media-grid {
    grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)) !important;
    gap: 8px !important;
  }

  .media-caption {
    padding: 4px 4px !important;
  }

  .media-title {
    font-size: 0.74rem !important;
    line-height: 1.25 !important;
  }

  .media-year {
    font-size: 0.65rem !important;
  }
}
</style>
