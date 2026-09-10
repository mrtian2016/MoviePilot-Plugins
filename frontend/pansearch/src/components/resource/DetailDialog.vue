<template>
  <v-dialog
    v-model="model"
    max-width="960"
    :fullscreen="fullscreen"
    transition="dialog-bottom-transition"
    class="resource-detail-dialog">
    <v-card v-if="activeMedia" class="detail-card">
      <transition name="loading-fade">
        <div v-if="detailLoading" class="detail-loading-overlay" role="status" aria-live="polite">
          <div class="detail-loading-content">
            <v-progress-circular indeterminate color="primary" size="48" width="4" />
            <span class="detail-loading-text">正在补全媒体信息...</span>
          </div>
        </div>
      </transition>

      <v-btn
        icon="mdi-close"
        size="small"
        variant="flat"
        class="detail-floating-close"
        title="关闭弹窗"
        aria-label="关闭弹窗"
        @click="closeMediaDetail" />

      <div class="detail-header-hero">
        <v-progress-linear v-if="detailLoading" indeterminate color="primary" height="2" class="hero-top-progress" />
        <div class="hero-backdrop" :style="{ backgroundImage: getBackdropStyle(activeMedia) }"></div>
        <div class="hero-overlay"></div>
        <div class="hero-content">
          <div class="hero-poster-wrapper">
            <!-- 类型标签 -->
            <span
              class="poster-media-type-badge font-weight-bold"
              :class="activeMedia.media_type === 'movie' ? 'badge--movie' : 'badge--tv'">
              {{ activeMedia.media_type === "movie" ? "电影" : "剧集" }}
            </span>

            <!-- 入库状态 -->
            <span
              class="poster-library-badge"
              :class="activeMedia.in_library ? 'poster-library-badge--in' : 'poster-library-badge--out'"
              :title="
                activeMedia.in_library
                  ? `已入库 · ${activeMedia.library_servers?.[0] || getLibrarySummary(activeMedia) || '媒体服务器'}`
                  : '未入库'
              ">
              <v-icon :icon="activeMedia.in_library ? 'mdi-check-circle' : 'mdi-circle-outline'" size="12" />
              <span class="poster-library-text font-weight-bold ml-1">
                {{ activeMedia.in_library ? "已入库" : "未入库" }}
              </span>
            </span>

            <v-img
              :src="activeMedia._currentPoster || activeMedia.poster_url"
              referrerpolicy="no-referrer"
              aspect-ratio="2/3"
              class="hero-poster w-100 fill-height"
              cover
              @error="handlePosterError(activeMedia)">
              <template #placeholder>
                <div class="poster-placeholder d-flex align-center justify-center fill-height">
                  <v-icon
                    :icon="activeMedia.media_type === 'tv' ? 'mdi-television-classic' : 'mdi-movie-open'"
                    size="36"
                    color="rgba(255, 255, 255, 0.25)" />
                </div>
              </template>
            </v-img>
          </div>

          <div class="hero-text min-w-0 flex-grow-1">
            <!-- 标题 -->
            <div class="hero-title-group mb-1">
              <div class="d-flex align-baseline ga-2 hero-title-row">
                <h2 class="hero-title" :title="displayTitle">
                  {{ displayTitle }}
                </h2>
                <span v-if="activeMedia.year" class="hero-title-year">({{ activeMedia.year }})</span>
              </div>

              <div v-if="displayOriginalTitle" class="hero-original-title mt-0.5">
                原始片名：{{ displayOriginalTitle }}
              </div>
            </div>
            <div class="hero-meta-bar d-flex align-center flex-wrap ga-2 my-1.5">
              <!-- 评分与题材标签 -->
              <div class="hero-meta-badges d-flex align-center ga-1.5 flex-wrap">
                <div
                  v-if="getMediaRatingInfo(activeMedia, 'tmdb')"
                  class="meta-tag-pill meta-tag-pill--rating d-flex align-center ga-1">
                  <span class="pill-brand-svg tmdb-svg-wrap" v-html="RAW_ICONS.tmdb" />
                  <span class="rating-val font-weight-bold color-tmdb">
                    {{ getMediaRatingInfo(activeMedia, "tmdb").score }}
                  </span>
                  <span v-if="getMediaRatingInfo(activeMedia, 'tmdb').votes" class="rating-votes text-caption">
                    {{ getMediaRatingInfo(activeMedia, "tmdb").votes }}
                  </span>
                </div>
                <div
                  v-if="getMediaRatingInfo(activeMedia, 'imdb')"
                  class="meta-tag-pill meta-tag-pill--rating d-flex align-center ga-1">
                  <span class="pill-brand-svg imdb-svg-wrap" v-html="RAW_ICONS.imdb" />
                  <span class="rating-val font-weight-bold color-imdb">
                    {{ getMediaRatingInfo(activeMedia, "imdb").score }}
                  </span>
                  <span v-if="getMediaRatingInfo(activeMedia, 'imdb').votes" class="rating-votes text-caption">
                    {{ getMediaRatingInfo(activeMedia, "imdb").votes }}
                  </span>
                </div>
                <div
                  v-if="getMediaRatingInfo(activeMedia, 'douban')"
                  class="meta-tag-pill meta-tag-pill--rating d-flex align-center ga-1">
                  <span class="pill-brand-svg douban-svg-wrap" v-html="RAW_ICONS.douban" />
                  <span class="rating-val font-weight-bold color-douban">
                    {{ getMediaRatingInfo(activeMedia, "douban").score }}
                  </span>
                  <span v-if="getMediaRatingInfo(activeMedia, 'douban').votes" class="rating-votes text-caption">
                    {{ getMediaRatingInfo(activeMedia, "douban").votes }}
                  </span>
                </div>
                <div
                  v-if="
                    !getMediaRatingInfo(activeMedia, 'tmdb') &&
                    !getMediaRatingInfo(activeMedia, 'imdb') &&
                    !getMediaRatingInfo(activeMedia, 'douban') &&
                    getRatingValue(activeMedia) > 0
                  "
                  class="meta-tag-pill meta-tag-pill--rating d-flex align-center ga-1">
                  <span class="pill-brand-svg tmdb-svg-wrap" v-html="RAW_ICONS.tmdb" />
                  <span class="rating-val font-weight-bold color-tmdb">{{ getRatingText(activeMedia) }}/10</span>
                </div>
                <span
                  v-for="genre in getMediaGenresList(activeMedia)"
                  :key="genre"
                  class="meta-tag-pill meta-tag-pill--genre">
                  {{ genre }}
                </span>
              </div>

              <!-- ID 徽章 -->
              <div
                v-if="
                  !fullscreen &&
                  (activeMedia.tmdb_id ||
                    activeMedia.imdb_id ||
                    activeMedia.douban_id ||
                    activeMedia.tvdb_id ||
                    activeMedia.bangumi_id)
                "
                class="hero-top-ids d-none d-sm-flex align-center ga-1.5 flex-wrap">
                <span
                  v-if="activeMedia.tmdb_id"
                  class="media-id-pill media-id-pill--tmdb"
                  title="点击复制 TMDB ID"
                  @click="copyIdText('TMDB', activeMedia.tmdb_id)">
                  <span class="id-brand-logo tmdb-logo-icon" v-html="RAW_ICONS.tmdb" />
                  <span class="id-label">{{ activeMedia.tmdb_id }}</span>
                </span>
                <span
                  v-if="activeMedia.imdb_id"
                  class="media-id-pill media-id-pill--imdb"
                  title="点击复制 IMDb ID"
                  @click="copyIdText('IMDb', activeMedia.imdb_id)">
                  <span class="id-brand-logo imdb-logo-icon" v-html="RAW_ICONS.imdb" />
                  <span class="id-label">{{ activeMedia.imdb_id }}</span>
                </span>
                <span
                  v-if="activeMedia.douban_id"
                  class="media-id-pill media-id-pill--douban"
                  title="点击复制豆瓣 ID"
                  @click="copyIdText('豆瓣', activeMedia.douban_id)">
                  <span class="id-brand-logo douban-logo-icon" v-html="RAW_ICONS.douban" />
                  <span class="id-label">{{ activeMedia.douban_id }}</span>
                </span>
                <span
                  v-if="activeMedia.bangumi_id"
                  class="media-id-pill"
                  title="点击复制 Bangumi ID"
                  @click="copyIdText('Bangumi', activeMedia.bangumi_id)">
                  <v-icon icon="mdi-television-classic" size="13" class="mr-1 text-pink-lighten-2" />
                  BGM: {{ activeMedia.bangumi_id }}
                </span>
                <span
                  v-if="activeMedia.tvdb_id"
                  class="media-id-pill"
                  title="点击复制 TVDB ID"
                  @click="copyIdText('TVDB', activeMedia.tvdb_id)">
                  <v-icon icon="mdi-database" size="13" class="mr-1 text-blue-lighten-2" />
                  TVDB: {{ activeMedia.tvdb_id }}
                </span>
                <span
                  v-if="activeMedia.anilist_id"
                  class="media-id-pill"
                  title="点击复制 AniList ID"
                  @click="copyIdText('AniList', activeMedia.anilist_id)">
                  <v-icon icon="mdi-format-list-bulleted" size="13" class="mr-1 text-purple-lighten-2" />
                  AniList: {{ activeMedia.anilist_id }}
                </span>
                <span
                  v-if="activeMedia.anidb_id"
                  class="media-id-pill"
                  title="点击复制 AniDB ID"
                  @click="copyIdText('AniDB', activeMedia.anidb_id)">
                  <v-icon icon="mdi-database-outline" size="13" class="mr-1 text-orange-lighten-2" />
                  AniDB: {{ activeMedia.anidb_id }}
                </span>
              </div>
            </div>
            <p class="hero-overview" :title="activeMedia.overview">
              {{ activeMedia.overview || "暂无作品简介。" }}
            </p>

            <div v-if="activeMedia.media_type === 'tv' && activeMediaSeasons.length" class="media-episode-console mt-2">
              <div class="console-header d-flex align-center justify-space-between mb-1.5">
                <div class="season-switcher d-flex align-center ga-1.5 flex-wrap">
                  <button
                    v-for="s in activeMediaSeasons"
                    :key="s.season_number"
                    type="button"
                    class="season-switch-tab"
                    :class="{ active: activeDetailSeason === s.season_number }"
                    @click="$emit('update:active-detail-season', s.season_number)">
                    {{ s.season_name }}
                  </button>
                </div>
                <div v-if="currentSeasonMissingEpisodes.length" class="console-missing-indicator flex-shrink-0">
                  <span class="missing-count-badge">缺 {{ currentSeasonMissingEpisodes.length }} 集</span>
                </div>
              </div>

              <div
                class="console-episodes-track d-flex align-center ga-1.5 flex-nowrap"
                @wheel.passive="
                  (e) => {
                    e.currentTarget.scrollLeft += e.deltaY
                  }
                ">
                <div
                  v-for="ep in currentSeasonMissingEpisodes"
                  :key="ep.episode"
                  class="media-ep-pill media-ep-pill--missing"
                  :title="`第 ${ep.episode} 集 · 缺失未入库`">
                  {{ ep.episode }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="channel-tabs-container px-4 pt-2">
        <div class="channel-tabs-shell d-flex align-center justify-space-between ga-2">
          <v-tabs
            v-model="channelModel"
            color="primary"
            align-tabs="start"
            density="comfortable"
            show-arrows
            class="channel-tabs"
            @update:model-value="onChannelTabChange">
            <v-tab v-for="ch in availableChannels" :key="ch.key" :value="ch.key" class="channel-tab">
              <v-icon start :icon="ch.icon" size="17" />
              {{ ch.name }}

              <v-badge
                v-if="channelSearched[ch.key]"
                :content="getChannelCount(ch.key)"
                inline
                :color="getChannelCount(ch.key) > 0 ? 'primary' : 'default'"
                class="ml-1.5" />
            </v-tab>
          </v-tabs>
        </div>

        <div
          v-if="currentChannelResourceTabs.length > 0 || currentChannelResources.length > 0"
          class="category-pill-group-container px-4 py-2">
          <div class="category-pill-toolbar d-flex align-center ga-2">
            <!-- 筛选浮层 -->
            <v-menu location="bottom start" offset="6" :close-on-content-click="false">
              <template #activator="{ props: menuProps }">
                <v-badge
                  :model-value="isFilterActive"
                  :content="activeResourceFilterCount || (selectedResourceSpecs.length + (resourceSearchQuery ? 1 : 0))"
                  color="primary"
                  class="category-filter-badge flex-shrink-0">
                  <v-btn
                    v-bind="menuProps"
                    icon="mdi-filter-variant"
                    size="small"
                    :variant="isFilterActive ? 'tonal' : 'text'"
                    :color="isFilterActive ? 'primary' : undefined"
                    :title="`筛选当前列表资源 (${currentSubTabLabel})`"
                    :aria-label="`筛选当前列表资源 (${currentSubTabLabel})`" />
                </v-badge>
              </template>
              <v-card min-width="290" max-width="340" class="resource-filter-menu" elevation="8">
                <v-card-title class="resource-filter-menu-title d-flex align-center px-4 py-2.5">
                  <span class="text-subtitle-2 font-weight-bold">筛选【{{ currentSubTabLabel }}】列表</span>
                  <v-spacer />
                  <v-btn
                    variant="text"
                    size="small"
                    color="primary"
                    :disabled="!isFilterActive"
                    @click="handleResetFilters">
                    重置
                  </v-btn>
                </v-card-title>
                <v-divider />
                <div class="px-4 py-3">
                  <v-text-field
                    :model-value="resourceSearchQuery"
                    prepend-inner-icon="mdi-magnify"
                    placeholder="搜索当前列表文本..."
                    variant="outlined"
                    density="compact"
                    hide-details
                    clearable
                    @update:model-value="$emit('update:resource-search-query', $event)" />

                  <div class="quick-spec-filter mt-3">
                    <div class="text-caption text-medium-emphasis mb-1.5 font-weight-medium">规格特征过滤</div>
                    <div class="d-flex flex-wrap ga-1.5">
                      <v-chip
                        v-for="spec in quickSpecOptions"
                        :key="spec.key"
                        size="small"
                        filter
                        :color="selectedResourceSpecs.includes(spec.key) ? 'primary' : 'default'"
                        :variant="selectedResourceSpecs.includes(spec.key) ? 'flat' : 'tonal'"
                        @click="toggleSpec(spec.key)">
                        {{ spec.label }}
                      </v-chip>
                    </div>
                  </div>
                </div>
              </v-card>
            </v-menu>

            <!-- 子 tab 胶囊按钮列表 -->
            <div class="category-pill-track d-flex align-center ga-1.5">
              <button
                v-for="tab in currentChannelResourceTabs"
                :key="tab.value"
                type="button"
                :class="['category-pill-btn', { 'category-pill-btn--active': activeResourceTab === tab.value }]"
                @click="$emit('update:active-resource-tab', tab.value)">
                <v-icon v-if="tab.icon" :icon="tab.icon" size="14" class="pill-icon mr-1" />
                <span class="pill-title">{{ tab.title }}</span>
                <span class="pill-badge ml-1.5" :class="{ 'pill-badge--active': activeResourceTab === tab.value }">
                  {{ tab.count }}
                </span>
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="detail-body px-4 py-3">
        <div v-if="channelLoading[activeChannelTab]" class="resource-loading">
          <v-progress-circular indeterminate color="primary" size="44" width="3" />
          <p class="mt-3 text-body-2 text-medium-emphasis font-weight-medium">
            正在检索【{{ getSourceName(activeChannelTab) }}】资源，请稍候...
          </p>
        </div>

        <div v-else-if="!channelSearched[activeChannelTab]" class="resource-empty">
          <v-icon icon="mdi-cloud-search-outline" size="52" color="medium-emphasis" />
          <p class="mt-2 text-body-2 text-medium-emphasis">尚未检索该渠道资源</p>
          <v-btn
            color="primary"
            variant="tonal"
            size="small"
            class="mt-2"
            @click="searchChannel(activeChannelTab, true)">
            立即检索
          </v-btn>
        </div>

        <div v-else-if="!currentChannelFilteredResources.length" class="resource-empty">
          <template v-if="isFilterActive && currentChannelResources.length > 0">
            <v-icon icon="mdi-filter-remove-outline" size="52" color="medium-emphasis" />
            <p class="mt-2 text-body-2 font-weight-medium text-high-emphasis">
              在【{{ currentSubTabLabel }}】中未找到符合筛选条件的资源
            </p>
            <p class="text-caption text-medium-emphasis mt-1">当前已生效关键词或规格过滤，您可以点击下方一键清空</p>
            <v-btn
              color="primary"
              variant="tonal"
              size="small"
              class="mt-3"
              prepend-icon="mdi-filter-off-outline"
              @click="handleResetFilters">
              清空筛选条件
            </v-btn>
          </template>
          <template v-else>
            <v-icon icon="mdi-database-search-outline" size="52" color="medium-emphasis" />
            <p class="mt-2 text-body-2 font-weight-medium text-high-emphasis">
              {{ (!currentChannelResources || currentChannelResources.length === 0) ? "未检索到候选资源" : "当前网盘分类暂无资源"
              }}
            </p>
            <p class="text-caption text-medium-emphasis mt-1">可尝试切换上方其他渠道，或点击下方手动刷新重试</p>
            <v-btn
              color="primary"
              variant="tonal"
              size="small"
              class="mt-3"
              prepend-icon="mdi-refresh"
              :loading="channelLoading[activeChannelTab]"
              @click="searchChannel(activeChannelTab, true)">
              手动刷新
            </v-btn>
          </template>
        </div>

        <div v-else class="resource-list d-flex flex-column ga-1.5">
          <div v-for="(res, idx) in currentChannelFilteredResources" :key="resKey(res, idx)" class="resource-row-item">
            <div class="resource-row-header d-flex align-center justify-space-between ga-2">
              <div class="resource-title-box min-w-0 flex-grow-1">
                <span class="resource-title-text" :title="res.title">
                  {{ res.title }}
                </span>
              </div>

              <!-- 操作按钮组 -->
              <div class="resource-actions-group d-flex align-center ga-1.5 flex-shrink-0">
                <v-btn
                  v-if="res.need_unlock"
                  icon="mdi-lock-open-outline"
                  size="small"
                  variant="tonal"
                  color="warning"
                  class="res-btn-icon"
                  :title="`确认消耗 ${Number(res.unlock_points || 0)} 积分解锁`"
                  :loading="unlockingKey === resKey(res, idx)"
                  aria-label="积分解锁"
                  @click.stop="openUnlockDialog(res, idx)" />

                <v-btn
                  v-if="canPreviewResource(res)"
                  icon="mdi-folder-eye-outline"
                  size="small"
                  color="primary"
                  variant="tonal"
                  class="res-btn-icon"
                  :loading="previewingKey === previewResourceKey(res)"
                  :disabled="Boolean(previewingKey)"
                  title="预览目录"
                  aria-label="预览目录"
                  @click.stop="previewResource(res)" />

                <v-btn
                  v-if="res.url"
                  icon="mdi-content-copy"
                  size="small"
                  variant="tonal"
                  color="secondary"
                  class="res-btn-icon"
                  title="复制链接"
                  aria-label="复制链接"
                  @click.stop="copyToClipboard(res.url)" />

                <v-btn
                  v-if="['magnet', 'ed2k'].includes(String(res.resource_type || '').toLowerCase())"
                  icon="mdi-download"
                  size="small"
                  color="primary"
                  variant="flat"
                  class="res-btn-icon res-btn-primary"
                  :loading="downloadingIndex === idx"
                  :disabled="downloadingIndex !== -1"
                  title="离线下载"
                  aria-label="离线下载"
                  @click.stop="handleQuickDownload(res, idx)" />

                <v-btn
                  v-else-if="isCrossTransferResource(res)"
                  icon="mdi-swap-horizontal"
                  size="small"
                  color="primary"
                  variant="flat"
                  class="res-btn-icon res-btn-primary"
                  :loading="downloadingIndex === idx"
                  :disabled="downloadingIndex !== -1"
                  title="跨盘转存"
                  aria-label="跨盘转存"
                  @click.stop="openCrossTransferDialog(res, idx)" />

                <v-btn
                  v-else
                  icon="mdi-cloud-download"
                  size="small"
                  color="primary"
                  variant="flat"
                  class="res-btn-icon res-btn-primary"
                  :loading="downloadingIndex === idx"
                  :disabled="downloadingIndex !== -1"
                  title="一键转存"
                  aria-label="一键转存"
                  @click.stop="handleQuickDownload(res, idx)" />
              </div>
            </div>
            <div class="resource-row-meta d-flex align-center flex-wrap ga-1.5 mt-1">
              <span v-if="getResourceSize(res)" class="resource-size-badge font-weight-bold">
                {{ getResourceSize(res) }}
              </span>

              <span v-if="res.seeders" class="resource-seeders-badge text-success font-weight-bold" title="做种数">
                <v-icon icon="mdi-arrow-up" size="11" />
                {{ res.seeders }}
              </span>

              <span
                v-if="res.need_unlock"
                class="quality-tag-pill tag-points"
                :title="`需要消耗 ${Number(res.unlock_points || 0)} 积分解锁`">
                <v-icon icon="mdi-coins" size="11" class="mr-0.5 text-amber" />
                {{ Number(res.unlock_points || 0) }} 积分
              </span>
              <span
                v-else-if="isPointUnlockResource(res) && Number(res.unlock_points || 0) === 0"
                class="quality-tag-pill tag-free">
                免费
              </span>
              <span
                v-for="tag in getMergedResourceTags(res)"
                :key="tag"
                class="quality-tag-pill"
                :title="tag"
                :class="getTagColorClass(tag)">
                {{ tag }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed} from "vue";
import {
  canPreviewResource as defaultCanPreviewResource,
  copyToClipboard as defaultCopyToClipboard,
  getBackdropStyle as defaultGetBackdropStyle,
  getExtractedTags as defaultGetExtractedTags,
  getLibrarySummary as defaultGetLibrarySummary,
  getMediaGenresList as defaultGetMediaGenresList,
  getMediaRatingInfo as defaultGetMediaRatingInfo,
  getNormalizedResourceType as defaultGetNormalizedResourceType,
  getRatingText as defaultGetRatingText,
  getRatingValue as defaultGetRatingValue,
  getResourceSize as defaultGetResourceSize,
  getResourceTypeName as defaultGetResourceTypeName,
  getSourceName as defaultGetSourceName,
  getTagColorClass as defaultGetTagColorClass,
  handlePosterError as defaultHandlePosterError,
  isCrossTransferResource as defaultIsCrossTransferResource,
  isPointUnlockResource as defaultIsPointUnlockResource,
  previewResourceKey as defaultPreviewResourceKey,
  RAW_ICONS as DEFAULT_RAW_ICONS,
  resKey as defaultResKey,
} from "../../composables/resourceUtils";

const props = defineProps({
  modelValue: Boolean,
  fullscreen: Boolean,
  activeMedia: {type: Object, default: null},
  detailLoading: Boolean,
  activeDetailSeason: {type: Number, default: 1},
  activeMediaSeasons: {type: Array, default: () => []},
  currentSeasonMissingEpisodes: {type: Array, default: () => []},
  availableChannels: {type: Array, default: () => []},
  activeChannelTab: {type: String, default: ""},
  activeResourceTab: {type: String, default: ""},
  resourceSearchQuery: {type: String, default: ""},
  selectedResourceSpecs: {type: Array, default: () => []},
  activeResourceFilterCount: {type: Number, default: 0},
  resetResourceFilters: {type: Function, default: null},
  currentChannelResources: {type: Array, default: () => []},
  channelLoading: {type: Object, default: () => ({})},
  channelSearched: {type: Object, default: () => ({})},
  currentChannelResourceTabs: {type: Array, default: () => []},
  currentChannelFilteredResources: {type: Array, default: () => []},
  rawIcons: {type: Object, default: () => ({})},
  downloadingIndex: {type: Number, default: -1},
  unlockingKey: {type: String, default: ""},
  previewingKey: {type: String, default: ""},
  getBackdropStyle: {type: Function, default: null},
  handlePosterError: {type: Function, default: null},
  copyIdText: {type: Function, default: null},
  getLibrarySummary: {type: Function, default: null},
  getMediaRatingInfo: {type: Function, default: null},
  getRatingValue: {type: Function, default: null},
  getRatingText: {type: Function, default: null},
  getMediaGenresList: {type: Function, default: null},
  onChannelTabChange: {type: Function, default: null},
  getChannelCount: {type: Function, default: null},
  getSourceName: {type: Function, default: null},
  searchChannel: {type: Function, default: null},
  resKey: {type: Function, default: null},
  getExtractedTags: {type: Function, default: null},
  getTagColorClass: {type: Function, default: null},
  getResourceSize: {type: Function, default: null},
  openUnlockDialog: {type: Function, default: null},
  canPreviewResource: {type: Function, default: null},
  previewResourceKey: {type: Function, default: null},
  previewResource: {type: Function, default: null},
  copyToClipboard: {type: Function, default: null},
  handleQuickDownload: {type: Function, default: null},
  isCrossTransferResource: {type: Function, default: null},
  openCrossTransferDialog: {type: Function, default: null},
  api: {type: Object, default: null},
  pluginId: {type: String, default: "PanSearch"},
  showMessage: {type: Function, default: null},
});

const emit = defineEmits([
  "update:modelValue",
  "update:active-detail-season",
  "update:active-channel-tab",
  "update:active-resource-tab",
  "update:resource-search-query",
  "update:selected-resource-specs",
]);

const model = computed({get: () => props.modelValue, set: (value) => emit("update:modelValue", value)});

const channelModel = computed({
  get: () => props.activeChannelTab,
  set: (value) => emit("update:active-channel-tab", value),
});
const isPointUnlockResource = (res) => defaultIsPointUnlockResource(res);

const currentSubTabLabel = computed(() => {
  const found = (props.currentChannelResourceTabs || []).find((t) => t.value === props.activeResourceTab);
  if (found) return found.title;
  if (props.currentChannelResourceTabs && props.currentChannelResourceTabs.length > 0) {
    return props.currentChannelResourceTabs[0].title;
  }
  return props.activeResourceTab || "资源列表";
});

const isFilterActive = computed(() => {
  return (
    Boolean(String(props.resourceSearchQuery || "").trim()) ||
    (Array.isArray(props.selectedResourceSpecs) && props.selectedResourceSpecs.length > 0)
  );
});

const quickSpecOptions = computed(() => {
  const options = [
    {key: "4K", label: "4K"},
    {key: "1080P", label: "1080P"},
    {key: "原盘", label: "原盘"},
    {key: "HDR", label: "HDR"},
    {key: "杜比视界", label: "杜比视界"},
  ];
  const channel = String(props.activeChannelTab || "").toLowerCase();
  if (["hdhive", "dian115"].includes(channel)) {
    options.push({key: "免费", label: "免费"});
  }
  return options;
});

function toggleSpec(specKey) {
  const current = [...(props.selectedResourceSpecs || [])];
  const idx = current.indexOf(specKey);
  if (idx >= 0) {
    current.splice(idx, 1);
  } else {
    current.push(specKey);
  }
  emit("update:selected-resource-specs", current);
}

function handleResetFilters() {
  emit("update:resource-search-query", "");
  emit("update:selected-resource-specs", []);
  if (props.resetResourceFilters) {
    props.resetResourceFilters();
  }
}

// 工具函数安全封装：优先使用外部传入，缺失时使用共享工具库兜底，彻底防止未捕获异常
const getBackdropStyle = (media) =>
  props.getBackdropStyle ? props.getBackdropStyle(media) : defaultGetBackdropStyle(media);
const handlePosterError = (media) =>
  props.handlePosterError ? props.handlePosterError(media) : defaultHandlePosterError(media);
const getLibrarySummary = (media) =>
  props.getLibrarySummary ? props.getLibrarySummary(media) : defaultGetLibrarySummary(media);
const getMediaRatingInfo = (media, platform) =>
  props.getMediaRatingInfo ? props.getMediaRatingInfo(media, platform) : defaultGetMediaRatingInfo(media, platform);
const getRatingValue = (media) => (props.getRatingValue ? props.getRatingValue(media) : defaultGetRatingValue(media));
const getRatingText = (media) => (props.getRatingText ? props.getRatingText(media) : defaultGetRatingText(media));
const getMediaGenresList = (media) =>
  props.getMediaGenresList ? props.getMediaGenresList(media) : defaultGetMediaGenresList(media);
const getSourceName = (source) => (props.getSourceName ? props.getSourceName(source) : defaultGetSourceName(source));
const resKey = (res, idx) => (props.resKey ? props.resKey(res, idx) : defaultResKey(res, idx));
const getExtractedTags = (res) => (props.getExtractedTags ? props.getExtractedTags(res) : defaultGetExtractedTags(res));
const getTagColorClass = (tag) => (props.getTagColorClass ? props.getTagColorClass(tag) : defaultGetTagColorClass(tag));
const getResourceSize = (res) => (props.getResourceSize ? props.getResourceSize(res) : defaultGetResourceSize(res));
const canPreviewResource = (res) =>
  props.canPreviewResource ? props.canPreviewResource(res) : defaultCanPreviewResource(res);
const previewResourceKey = (res) =>
  props.previewResourceKey ? props.previewResourceKey(res) : defaultPreviewResourceKey(res);
const isCrossTransferResource = (res) =>
  props.isCrossTransferResource ? props.isCrossTransferResource(res) : defaultIsCrossTransferResource(res);
const copyToClipboard = (text) => (props.copyToClipboard ? props.copyToClipboard(text) : defaultCopyToClipboard(text));
const RAW_ICONS = props.rawIcons && Object.keys(props.rawIcons).length ? props.rawIcons : DEFAULT_RAW_ICONS;

function getMergedResourceTags(res) {
  if (!res) return [];
  // 1. 提取标准化规格标签（4K, HDR, 中字, 杜比视界, 杜比全景声等）
  const tags = [...(getExtractedTags(res) || [])];
  const set = new Set(tags.map((t) => String(t).toUpperCase()));

  // 2. 收集资源标题及影视剧相关标题，用于过滤重复标题/片名标签
  const resTitle = String(res.title || "").toLowerCase();
  const media = props.activeMedia;
  const mediaTitles = [
    media?.title,
    media?.cn_title,
    media?.name,
    media?.original_title,
    media?.original_name,
    media?.en_name,
  ]
    .filter(Boolean)
    .map((t) => String(t).trim().toLowerCase());

  const trashWords = [
    "盘酱酱", "PANWEB", "国产剧", "美剧", "日韩剧", "韩剧", "日剧", "泰剧",
    "动漫", "电影", "电视剧", "全集", "合集", "更新", "更新至", "最新",
    "分享", "免费", "链接", "未删减", "超清", "高清", "热播", "完结",
    "首发", "独家", "推荐", "资源",
  ];

  // 3. 处理渠道原生 tags（过滤掉与标题、片名、剧集信息重复的冗余标签）
  if (Array.isArray(res.tags)) {
    for (const t of res.tags) {
      if (!t) continue;
      const parts = String(t).split(/[\s,，;；|/]+/);
      for (const rawPart of parts) {
        const clean = rawPart.replace(/^#+/, "").trim();
        if (!clean || clean.length < 2 || clean.length > 10) continue;
        const cleanLower = clean.toLowerCase();
        const cleanUpper = clean.toUpperCase();

        // 垃圾词过滤
        if (trashWords.some((w) => cleanLower.includes(w.toLowerCase()))) continue;

        // 季/集/年份词过滤（如 S01, E09, 第1季, 2026）
        if (/^(s\d+|e\d+|ep\d+|第[0-9一二三四五六七八九十]+[季期集]|19\d\d|20\d\d)$/i.test(clean)) continue;

        // 核心过滤：不应该重复显示标题的标签
        // (1) 资源标题中已经包含该标签词（如“末日地堡”在“末日地堡/羊毛战记...”中已存在）
        if (resTitle && resTitle.includes(cleanLower)) continue;

        // (2) 媒体本身的名字包含该标签，或者该标签包含媒体名
        if (mediaTitles.some((mTitle) => mTitle && (mTitle === cleanLower || mTitle.includes(cleanLower) || cleanLower.includes(mTitle)))) {
          continue;
        }

        const driveType = defaultGetNormalizedResourceType(res);
        const driveLabel = driveType && driveType !== "other" ? defaultGetResourceTypeName(driveType).toLowerCase() : "";
        if (driveType && cleanLower === driveType) continue;
        if (driveLabel && (cleanLower === driveLabel || cleanLower.includes(driveLabel) || driveLabel.includes(cleanLower))) continue;

        // 避免重复规格或重复tag
        if (!set.has(cleanUpper)) {
          set.add(cleanUpper);
          tags.push(clean);
          if (tags.length >= 6) break;
        }
      }
      if (tags.length >= 6) break;
    }
  }

  const standardSpecs = [
    "4K", "1080P", "720P", "原盘ISO", "REMUX", "BLURAY", "WEB-DL",
    "杜比视界", "HDR", "HDR10+", "HDR10", "60帧", "120帧", "中字", "国语", "粤语", "杜比全景声",
  ];
  return tags.filter((tag) => {
    const tagUpper = String(tag).toUpperCase();
    if (standardSpecs.some((s) => s.toUpperCase() === tagUpper)) return true;
    const tagLower = String(tag).toLowerCase();
    if (resTitle && resTitle.includes(tagLower)) return false;
    if (mediaTitles.some((mTitle) => mTitle && (mTitle === tagLower || mTitle.includes(tagLower) || tagLower.includes(mTitle)))) return false;
    const driveType = defaultGetNormalizedResourceType(res);
    const driveLabel = driveType && driveType !== "other" ? defaultGetResourceTypeName(driveType).toLowerCase() : "";
    if (driveType && tagLower === driveType) return false;
    if (driveLabel && (tagLower === driveLabel || tagLower.includes(driveLabel) || driveLabel.includes(tagLower))) return false;
    return true;
  });
}

function copyIdText(type, id) {
  if (props.copyIdText) {
    props.copyIdText(type, id);
  } else {
    copyToClipboard(String(id));
  }
}

// 影视中文标题与原始片名展示计算（对齐图3排版规范）
const displayTitle = computed(() => {
  const m = props.activeMedia;
  if (!m) return "";
  return m.cn_title || m.title || m.name || "未命名媒体";
});

const displayOriginalTitle = computed(() => {
  const m = props.activeMedia;
  if (!m) return "";
  const orig = String(m.original_title || m.original_name || "").trim();
  const disp = String(displayTitle.value || "").trim();
  if (!orig || orig.toLowerCase() === disp.toLowerCase()) return "";
  return orig;
});

const closeMediaDetail = () => {
  model.value = false;
};
</script>

<style scoped>
:global(.resource-detail-dialog.v-overlay .v-overlay__content),
.resource-detail-dialog :deep(.v-overlay__content) {
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  width: min(1060px, calc(100vw - 48px)) !important;
  max-width: min(1060px, calc(100vw - 48px)) !important;
  height: min(760px, calc(90vh - 32px)) !important;
  min-height: min(680px, calc(86vh - 32px)) !important;
  max-height: min(840px, calc(92vh - 20px)) !important;
  margin: auto !important;
  overflow: hidden !important;
  border-radius: var(--app-overlay-radius, 18px) !important;
  background: transparent !important;
  box-shadow: none !important;
}

/* 全屏模式下的强制铺满视口 */
:global(.resource-detail-dialog.v-overlay.v-overlay--fullscreen .v-overlay__content),
:global(.resource-detail-dialog.v-overlay--fullscreen .v-overlay__content),
.resource-detail-dialog.v-overlay--fullscreen :deep(.v-overlay__content) {
  width: 100vw !important;
  max-width: 100vw !important;
  height: 100% !important;
  min-height: 100% !important;
  max-height: 100% !important;
  margin: 0 !important;
  border-radius: 0 !important;
  top: 0 !important;
  left: 0 !important;
  position: fixed !important;
}

:global(.resource-detail-dialog.v-overlay--fullscreen) .detail-card,
.resource-detail-dialog.v-overlay--fullscreen :deep(.detail-card) {
  border-radius: 0 !important;
  border: none !important;
  width: 100vw !important;
  height: 100% !important;
  max-height: 100% !important;
}

.detail-loading-overlay {
  position: absolute !important;
  inset: 0 !important;
  top: 0 !important;
  left: 0 !important;
  right: 0 !important;
  bottom: 0 !important;
  width: 100% !important;
  height: 100% !important;
  z-index: 60 !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  background: rgba(11, 15, 25, 0.78) !important;
  pointer-events: all !important;
}

.detail-loading-content {
  display: flex !important;
  flex-direction: column !important;
  align-items: center !important;
  justify-content: center !important;
  gap: 16px !important;
}

.detail-loading-text {
  font-size: 1.02rem !important;
  font-weight: 600 !important;
  color: #ffffff !important;
  letter-spacing: 0.5px !important;
  text-shadow: 0 2px 8px rgba(0, 0, 0, 0.85) !important;
}

.missing-count-badge {
  display: inline-flex !important;
  align-items: center !important;
  padding: 1.5px 7px !important;
  border-radius: 10px !important;
  font-size: 0.72rem !important;
  font-weight: 700 !important;
  background: rgba(245, 158, 11, 0.18) !important;
  border: 1px solid rgba(245, 158, 11, 0.45) !important;
  color: #f59e0b !important;
  line-height: 1.2 !important;
}

.hero-top-progress {
  position: absolute !important;
  top: 0 !important;
  left: 0 !important;
  right: 0 !important;
  width: 100% !important;
  z-index: 10 !important;
}

.loading-fade-enter-active,
.loading-fade-leave-active {
  transition: opacity 0.22s ease;
}

.loading-fade-enter-from,
.loading-fade-leave-to {
  opacity: 0;
}

.detail-card {
  position: relative;
  display: flex !important;
  flex-direction: column !important;
  width: 100% !important;
  height: 100% !important;
  min-height: 100% !important;
  max-height: 100% !important;
  overflow: hidden !important;
  border-radius: var(--app-overlay-radius, 18px) !important;
  background-color: rgb(var(--v-theme-surface)) !important;
  background-image: none !important;
  -webkit-backdrop-filter: none !important;
  backdrop-filter: none !important;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity, 0.12)) !important;
  box-shadow: 0 16px 48px rgba(0, 0, 0, 0.28) !important;
  color: rgb(var(--v-theme-on-surface)) !important;
}

/* 仅在玻璃主题（glass 或 transparent）下才开启毛玻璃与透明渐变 */
:global(html[data-theme="glass"]) .detail-card,
:global(html[data-theme="transparent"]) .detail-card {
  -webkit-backdrop-filter: var(--glass-overlay-backdrop-filter, blur(28px) saturate(120%)) !important;
  backdrop-filter: var(--glass-overlay-backdrop-filter, blur(28px) saturate(120%)) !important;
  background-color: var(--glass-overlay-surface, rgba(15, 23, 42, 0.82)) !important;
  background-image: var(
    --glass-sheen,
    linear-gradient(180deg, rgba(255, 255, 255, 0.07) 0%, transparent 40%)
  ) !important;
  border: 1px solid var(--glass-border-raised, rgba(255, 255, 255, 0.15)) !important;
  box-shadow: var(--glass-shadow-raised, 0 24px 64px rgba(0, 0, 0, 0.55)) !important;
}

.detail-floating-close {
  position: absolute !important;
  top: 14px !important;
  right: 14px !important;
  z-index: 20 !important;
  background: rgba(15, 23, 42, 0.7) !important;
  color: #ffffff !important;
  border: 1px solid rgba(255, 255, 255, 0.22) !important;
  border-radius: 50% !important;
  backdrop-filter: blur(10px) !important;
  -webkit-backdrop-filter: blur(10px) !important;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3) !important;
  transition: all 0.2s ease !important;
}

:global(html[data-theme="glass"]) .detail-floating-close,
:global(html[data-theme="transparent"]) .detail-floating-close {
  background: var(--glass-control, rgba(15, 23, 42, 0.65)) !important;
  color: #ffffff !important;
  border: 1px solid var(--glass-border, rgba(255, 255, 255, 0.2)) !important;
}

.detail-floating-close:hover {
  background: rgba(239, 68, 68, 0.9) !important;
  color: #ffffff !important;
  transform: scale(1.08) !important;
}

.detail-header-hero {
  position: relative;
  flex: 0 0 auto !important;
  flex-shrink: 0 !important;
  min-height: 170px;
  padding: 16px 20px;
  overflow: hidden;
  background: #0b0f19 !important;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.12);
}

.hero-backdrop {
  position: absolute;
  inset: -30px;
  z-index: 1;
  background-size: cover;
  background-position: center 32%;
  filter: blur(28px) brightness(0.72) saturate(1.28);
  transform: scale(1.15);
  opacity: 0.95;
  pointer-events: none;
}

.hero-overlay {
  position: absolute;
  inset: 0;
  z-index: 2;
  background: linear-gradient(180deg, rgba(11, 15, 25, 0.42) 0%, rgba(11, 15, 25, 0.82) 100%) !important;
  pointer-events: none;
}

.hero-content {
  position: relative;
  z-index: 3;
  display: flex;
  align-items: flex-start;
  gap: 16px;
  width: 100%;
}

.hero-poster-wrapper {
  flex: 0 0 clamp(114px, 14vw, 138px) !important;
  width: clamp(114px, 14vw, 138px) !important;
  aspect-ratio: 2 / 3 !important;
  height: auto !important;
  border-radius: 10px !important;
  overflow: hidden !important;
  background: rgba(15, 23, 42, 0.7) !important;
  border: 1px solid rgba(255, 255, 255, 0.16) !important;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.35) !important;
  position: relative !important;
}

.poster-media-type-badge {
  position: absolute !important;
  top: 6px !important;
  left: 6px !important;
  z-index: 10 !important;
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  padding: 1.5px 6px !important;
  border-radius: 4px !important;
  font-size: 0.65rem !important;
  letter-spacing: 0.3px !important;
  line-height: 1.2 !important;
  pointer-events: none !important;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.4) !important;
}

.badge--movie {
  background: rgba(14, 165, 233, 0.9) !important;
  color: #fff !important;
}

.badge--tv {
  background: rgba(139, 92, 246, 0.9) !important;
  color: #fff !important;
}

.poster-library-badge {
  position: absolute !important;
  top: 6px !important;
  right: 6px !important;
  z-index: 10 !important;
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  padding: 2px 7px !important;
  border-radius: 5px !important;
  font-size: 0.68rem !important;
  line-height: 1.2 !important;
  letter-spacing: 0.2px !important;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45) !important;
  pointer-events: none !important;
}

.poster-library-badge--in {
  background: rgba(22, 163, 74, 0.92) !important;
  border: 1px solid rgba(34, 197, 94, 0.6) !important;
  color: #ffffff !important;
}

.poster-library-badge--out {
  background: rgba(15, 23, 42, 0.82) !important;
  border: 1px solid rgba(255, 255, 255, 0.22) !important;
  color: rgba(255, 255, 255, 0.75) !important;
}

.hero-poster {
  width: 100% !important;
  height: 100% !important;
  min-height: 100% !important;
  display: block !important;
  position: absolute !important;
  inset: 0 !important;
}

.hero-poster :deep(.v-responsive__sizer) {
  display: none !important;
}

.hero-poster :deep(.v-img__img),
.hero-poster :deep(img) {
  width: 100% !important;
  height: 100% !important;
  object-fit: cover !important;
  object-position: center !important;
  display: block !important;
}

.poster-placeholder {
  background: rgba(var(--v-theme-surface-variant), 0.2);
}

.hero-text {
  flex-grow: 1;
  color: #fff;
  min-width: 0;
  padding-right: 36px;
}

.hero-title-group {
  margin-bottom: 8px;
}

.hero-title-combined-row {
  display: flex !important;
  align-items: center !important;
  flex-wrap: wrap !important;
  gap: 12px !important;
}

.hero-title-row {
  line-height: 1;
  display: flex !important;
  align-items: baseline !important;
  gap: 6px !important;
}

.hero-title {
  font-size: 1.45rem;
  font-weight: 800;
  color: #ffffff !important;
  letter-spacing: 0.3px;
  margin: 0;
  text-shadow: 0 2px 6px rgba(0, 0, 0, 0.7);
  display: inline-block;
  line-height: 1.2;
}

.hero-title-year {
  font-size: 1.1rem;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.85);
  letter-spacing: 0.2px;
  text-shadow: 0 2px 4px rgba(0, 0, 0, 0.6);
}

.hero-meta-badges {
  display: flex !important;
  align-items: center !important;
  flex-wrap: wrap !important;
  gap: 6px !important;
}

.meta-tag-pill {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  height: 24px !important;
  min-height: 24px !important;
  max-height: 24px !important;
  padding: 0 8px !important;
  border-radius: 5px !important;
  font-size: 0.72rem !important;
  font-weight: 600 !important;
  line-height: 1 !important;
  letter-spacing: 0.2px !important;
  user-select: none !important;
  box-sizing: border-box !important;
  background: rgba(255, 255, 255, 0.1) !important;
  border: 1px solid rgba(255, 255, 255, 0.16) !important;
  color: #f1f5f9 !important;
  vertical-align: middle !important;
  transition: all 0.15s ease !important;
}

.meta-tag-pill:hover {
  background: rgba(255, 255, 255, 0.18) !important;
  border-color: rgba(255, 255, 255, 0.28) !important;
}

.meta-tag-pill--rating {
  gap: 5px !important;
}

.pill-brand-svg {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  line-height: 0 !important;
}

.pill-brand-svg svg {
  display: block !important;
  height: 13px !important;
  width: auto !important;
}

.rating-val {
  font-size: 0.76rem !important;
  font-weight: 700 !important;
  letter-spacing: 0.2px !important;
  line-height: 1 !important;
}

.color-tmdb {
  color: #00d2c4 !important;
}

.color-imdb {
  color: #f5c518 !important;
}

.color-douban {
  color: #22c55e !important;
}

.rating-votes {
  font-size: 0.68rem !important;
  color: rgba(255, 255, 255, 0.55) !important;
  font-weight: 400 !important;
  line-height: 1 !important;
}

.hero-original-title {
  font-size: 0.82rem;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.72);
  margin-top: 3px;
  letter-spacing: 0.2px;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.6);
}

.hero-overview {
  font-size: 0.84rem;
  line-height: 1.65;
  color: rgba(255, 255, 255, 0.85) !important;
  letter-spacing: 0.3px;
  text-align: justify;
  text-justify: inter-ideograph;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-top: 6px;
  margin-bottom: 0;
  max-width: 860px;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.7);
}

.hero-library-merged {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  height: 24px !important;
  min-height: 24px !important;
  max-height: 24px !important;
  padding: 0 8px !important;
  border-radius: 5px !important;
  font-size: 0.72rem !important;
  font-weight: 600 !important;
  line-height: 1 !important;
  letter-spacing: 0.2px !important;
  box-sizing: border-box !important;
  vertical-align: middle !important;
}

.hero-library-merged--in {
  background: rgba(34, 197, 94, 0.22) !important;
  border: 1px solid rgba(34, 197, 94, 0.5) !important;
  color: #4ade80 !important;
}

.hero-library-merged--out {
  background: rgba(255, 255, 255, 0.08) !important;
  border: 1px solid rgba(255, 255, 255, 0.14) !important;
  color: rgba(255, 255, 255, 0.65) !important;
}

.hero-top-ids {
  display: inline-flex !important;
  align-items: center !important;
  gap: 6px !important;
  flex-wrap: wrap !important;
}

.media-id-pill {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  height: 24px !important;
  min-height: 24px !important;
  max-height: 24px !important;
  padding: 0 8px !important;
  border-radius: 5px !important;
  font-size: 0.72rem !important;
  font-weight: 600 !important;
  gap: 5px !important;
  color: #f8fafc !important;
  background: rgba(255, 255, 255, 0.1) !important;
  border: 1px solid rgba(255, 255, 255, 0.16) !important;
  box-sizing: border-box !important;
  cursor: pointer !important;
  letter-spacing: 0.2px !important;
  line-height: 1 !important;
  vertical-align: middle !important;
  transition: all 0.2s ease !important;
}

.media-id-pill:hover {
  background: rgba(var(--v-theme-primary), 0.35) !important;
  color: #ffffff !important;
  border-color: rgba(var(--v-theme-primary), 0.6) !important;
  transform: translateY(-1px);
}

.id-brand-logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 0;
}

.id-brand-logo svg {
  display: block;
  height: 14px;
  width: auto;
}

.media-episode-console {
  background: rgba(15, 23, 42, 0.62);
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: 8px;
  padding: 8px 12px;
  backdrop-filter: blur(12px);
  margin-top: 8px;
}

.console-header {
  min-height: 22px;
}

.season-switcher {
  gap: 4px;
}

.season-switch-tab {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.72rem;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.65);
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.1);
  cursor: pointer;
  transition: all 0.15s ease;
  line-height: 1.2;
}

.season-switch-tab:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.15);
}

.season-switch-tab.active {
  color: #fff;
  background: rgba(var(--v-theme-primary), 0.4);
  border-color: rgba(var(--v-theme-primary), 0.7);
  font-weight: 700;
}

.missing-track-label {
  font-size: 0.72rem;
  color: #fbbf24;
  font-weight: 700;
  white-space: nowrap;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  padding-right: 2px;
}

.console-episodes-track {
  display: flex !important;
  flex-wrap: nowrap !important;
  gap: 6px !important;
  height: 36px !important;
  max-height: 36px !important;
  overflow-x: auto !important;
  overflow-y: hidden !important;
  padding: 2px 2px 4px 0 !important;
  scrollbar-width: thin;
  scrollbar-color: rgba(255, 255, 255, 0.28) transparent;
  -webkit-overflow-scrolling: touch;
}

.console-episodes-track::-webkit-scrollbar {
  height: 4px;
}

.console-episodes-track::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.28);
  border-radius: 4px;
}

.console-episodes-track::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.45);
}

.media-ep-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 36px;
  height: 28px;
  padding: 0 6px;
  border-radius: 6px;
  font-size: 0.82rem;
  font-weight: 700;
  user-select: none;
  cursor: default;
  transition: all 0.15s ease;
  flex-shrink: 0 !important;
}

.media-ep-pill:hover {
  transform: translateY(-1px);
}

.media-ep-pill--missing {
  background: rgba(245, 158, 11, 0.16) !important;
  border: 1px solid rgba(245, 158, 11, 0.45) !important;
  color: #fbbf24 !important;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18) !important;
}

.media-ep-pill--missing:hover {
  background: rgba(245, 158, 11, 0.28) !important;
  border-color: rgba(245, 158, 11, 0.8) !important;
  color: #fef3c7 !important;
}

.media-ep-pill--in {
  background: rgba(34, 197, 94, 0.2) !important;
  border: 1px solid rgba(34, 197, 94, 0.55) !important;
  color: #4ade80 !important;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18) !important;
}

.media-ep-pill--in:hover {
  background: rgba(34, 197, 94, 0.28) !important;
  border-color: rgba(34, 197, 94, 0.8) !important;
}

.media-ep-pill--out {
  background: rgba(255, 255, 255, 0.04) !important;
  border: 1px dashed rgba(255, 255, 255, 0.18) !important;
  color: rgba(255, 255, 255, 0.4) !important;
}

.media-ep-pill--out:hover {
  background: rgba(255, 255, 255, 0.08) !important;
  color: rgba(255, 255, 255, 0.65) !important;
}

.channel-tabs-container {
  flex: 0 0 auto;
  background: transparent !important;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.1) !important;
}

:global(html[data-theme="glass"]) .channel-tabs-container,
:global(html[data-theme="transparent"]) .channel-tabs-container {
  background: var(--glass-surface-soft, rgba(15, 23, 42, 0.45)) !important;
  border-bottom: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08)) !important;
  backdrop-filter: var(--glass-surface-backdrop-filter, blur(10px));
  -webkit-backdrop-filter: var(--glass-surface-backdrop-filter, blur(10px));
}

.channel-tabs-shell {
  min-height: 44px;
}

.channel-tabs {
  flex: 1 1 auto;
  min-width: 0;
}

.channel-tabs :deep(.v-slide-group__content) {
  flex-wrap: nowrap;
}

.channel-tabs :deep(.v-tab) {
  flex: 0 0 auto;
}

.channel-refresh-btn {
  border-radius: 8px;
}

.channel-tab {
  font-size: 0.85rem !important;
  font-weight: 600;
  text-transform: none;
  color: rgba(var(--v-theme-on-surface), 0.72) !important;
}

.channel-tab.v-tab--selected {
  color: rgb(var(--v-theme-primary)) !important;
  font-weight: 700 !important;
}

.category-pill-group-container {
  flex: 0 0 auto;
  padding: 6px 16px 6px 16px;
  background: transparent !important;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.08) !important;
}

.category-pill-track {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  background: transparent;
  border: none;
  border-radius: 0;
  width: auto;
  max-width: 100%;
  flex-wrap: wrap;
  overflow-x: auto;
  overflow-y: hidden;
}

.category-pill-btn {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  border: 1px solid rgba(var(--v-border-color), 0.14);
  outline: none;
  background: transparent;
  color: rgba(var(--v-theme-on-surface), 0.78);
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 0.8rem;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.18s ease;
  white-space: nowrap;
  user-select: none;
}

:global(html[data-theme="glass"]) .category-pill-btn,
:global(html[data-theme="transparent"]) .category-pill-btn {
  border-radius: 20px;
  border: 1px solid var(--glass-border, rgba(255, 255, 255, 0.14));
  background: var(--glass-surface-soft, rgba(255, 255, 255, 0.06));
  color: rgba(255, 255, 255, 0.85);
}

.category-pill-btn:hover {
  background: rgba(var(--v-theme-on-surface), 0.05);
  color: rgb(var(--v-theme-on-surface));
}

.category-pill-btn--active {
  background: rgba(var(--v-theme-primary), 0.1) !important;
  color: rgb(var(--v-theme-primary)) !important;
  border-color: rgba(var(--v-theme-primary), 0.4) !important;
  font-weight: 600 !important;
  box-shadow: none !important;
}

:global(html[data-theme="glass"]) .category-pill-btn--active,
:global(html[data-theme="transparent"]) .category-pill-btn--active {
  background: rgba(var(--v-theme-primary), 0.22) !important;
  border-color: rgba(var(--v-theme-primary), 0.6) !important;
  box-shadow: 0 2px 8px rgba(var(--v-theme-primary), 0.2) !important;
}

.category-pill-btn--active .pill-icon {
  color: rgb(var(--v-theme-primary)) !important;
}

.pill-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  font-size: 0.68rem;
  font-weight: 700;
  background: rgba(var(--v-theme-on-surface), 0.08);
  color: rgba(var(--v-theme-on-surface), 0.7);
  line-height: 1;
  transition: all 0.2s ease;
}

.category-pill-btn--active .pill-badge {
  background: rgb(var(--v-theme-primary)) !important;
  color: #fff !important;
}

.detail-body {
  flex: 1 1 0 !important;
  min-height: 0 !important;
  height: 100% !important;
  display: flex !important;
  flex-direction: column !important;
  overflow: hidden !important;
  background: transparent !important;
}

.resource-loading,
.resource-empty {
  flex: 1 1 auto !important;
  min-height: 0 !important;
  height: 100% !important;
  display: flex !important;
  flex-direction: column !important;
  align-items: center;
  justify-content: center;
}

.resource-list {
  flex: 1 1 0 !important;
  min-height: 0 !important;
  height: 100% !important;
  overflow-y: auto !important;
  padding-right: 2px;
}

.resource-list::-webkit-scrollbar {
  width: 6px;
}

.resource-list::-webkit-scrollbar-thumb {
  background: rgba(var(--v-theme-on-surface), 0.18);
  border-radius: 3px;
}

.resource-list::-webkit-scrollbar-thumb:hover {
  background: rgba(var(--v-theme-on-surface), 0.32);
}

/* ================= 现代化流媒体平整行系统 ================= */
.resource-row-item {
  position: relative;
  display: flex !important;
  flex-direction: column !important;
  padding: 8px 12px !important;
  border-radius: 7px !important;
  background: rgba(var(--v-theme-surface), 0.35) !important;
  border: 1px solid rgba(var(--v-border-color), 0.08) !important;
  transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1) !important;
}

.resource-row-item:hover {
  background: rgba(var(--v-theme-surface), 0.65) !important;
  border-color: rgba(var(--v-theme-primary), 0.35) !important;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.06) !important;
}

:global(html[data-theme="glass"]) .resource-row-item,
:global(html[data-theme="transparent"]) .resource-row-item {
  background: rgba(255, 255, 255, 0.03) !important;
  border: 1px solid rgba(255, 255, 255, 0.07) !important;
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
}

:global(html[data-theme="glass"]) .resource-row-item:hover,
:global(html[data-theme="transparent"]) .resource-row-item:hover {
  background: rgba(255, 255, 255, 0.07) !important;
  border-color: rgba(var(--v-theme-primary), 0.5) !important;
}

.resource-row-header {
  width: 100%;
}

.resource-title-box {
  line-height: 1.35;
  min-width: 0;
}

.resource-title-text {
  display: block;
  font-size: 0.88rem !important;
  font-weight: 600 !important;
  color: rgb(var(--v-theme-on-surface)) !important;
  white-space: nowrap !important;
  overflow: hidden !important;
  text-overflow: ellipsis !important;
  cursor: default;
}

.resource-actions-group {
  gap: 5px !important;
}

.res-btn-icon {
  width: 28px !important;
  height: 28px !important;
  min-width: 28px !important;
  max-width: 28px !important;
  border-radius: 6px !important;
  padding: 0 !important;
  box-shadow: none !important;
  transition: all 0.15s ease !important;
}

.res-btn-icon:hover {
  transform: translateY(-1px) !important;
}

.res-btn-icon :deep(.v-icon) {
  font-size: 15px !important;
}

.res-btn-primary {
  background: rgb(var(--v-theme-primary)) !important;
  color: #ffffff !important;
  box-shadow: 0 2px 6px rgba(var(--v-theme-primary), 0.35) !important;
}

.res-btn-primary:hover {
  box-shadow: 0 4px 10px rgba(var(--v-theme-primary), 0.5) !important;
}

.resource-row-meta {
  gap: 6px !important;
}

.resource-size-badge {
  font-size: 0.78rem !important;
  color: rgb(var(--v-theme-primary)) !important;
  white-space: nowrap !important;
  letter-spacing: 0.2px;
}

.resource-seeders-badge {
  font-size: 0.72rem !important;
  white-space: nowrap !important;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

/* ================= 资源规格与分类微标签 ================= */
.quality-tag-pill {
  display: inline-flex !important;
  flex: 0 0 auto !important;
  flex-shrink: 0 !important;
  align-items: center !important;
  justify-content: center !important;
  min-height: 20px !important;
  height: 20px !important;
  padding: 1px 7px !important;
  border-radius: 4px !important;
  font-size: 0.68rem !important;
  font-weight: 700 !important;
  line-height: 1.15 !important;
  letter-spacing: 0.2px !important;
  white-space: nowrap !important;
  user-select: none !important;
  border: 1px solid rgba(100, 116, 139, 0.3) !important;
  background: rgba(100, 116, 139, 0.08) !important;
  color: #64748b !important;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04) !important;
  transition: transform 0.12s ease !important;
}

.quality-tag-pill:hover {
  transform: translateY(-1px) !important;
}

.tag-4k {
  color: #0284c7 !important;
  border-color: rgba(2, 132, 199, 0.45) !important;
  background: rgba(2, 132, 199, 0.12) !important;
}

.tag-1080p {
  color: #4f46e5 !important;
  border-color: rgba(79, 70, 229, 0.4) !important;
  background: rgba(79, 70, 229, 0.1) !important;
}

.tag-720p {
  color: #0284c7 !important;
  border-color: rgba(2, 132, 199, 0.3) !important;
  background: rgba(2, 132, 199, 0.08) !important;
}

.tag-dv {
  color: #9333ea !important;
  border-color: rgba(147, 51, 234, 0.45) !important;
  background: rgba(147, 51, 234, 0.12) !important;
}

.tag-hdr {
  color: #ea580c !important;
  border-color: rgba(234, 88, 12, 0.45) !important;
  background: rgba(234, 88, 12, 0.12) !important;
}

.tag-remux {
  color: #d97706 !important;
  border-color: rgba(217, 119, 6, 0.45) !important;
  background: rgba(217, 119, 6, 0.12) !important;
}

.tag-web {
  color: #475569 !important;
  border-color: rgba(71, 85, 105, 0.35) !important;
  background: rgba(71, 85, 105, 0.1) !important;
}

.tag-sub {
  color: #059669 !important;
  border-color: rgba(5, 150, 105, 0.45) !important;
  background: rgba(5, 150, 105, 0.12) !important;
}

.tag-dub {
  color: #e11d48 !important;
  border-color: rgba(225, 29, 72, 0.42) !important;
  background: rgba(225, 29, 72, 0.1) !important;
}

.tag-atmos {
  color: #0891b2 !important;
  border-color: rgba(8, 145, 178, 0.42) !important;
  background: rgba(8, 145, 178, 0.11) !important;
}

.tag-fps {
  color: #7c3aed !important;
  border-color: rgba(124, 58, 237, 0.42) !important;
  background: rgba(124, 58, 237, 0.11) !important;
}

.tag-points {
  color: #f59e0b !important;
  border-color: rgba(245, 158, 11, 0.4) !important;
  background: rgba(245, 158, 11, 0.1) !important;
}

.tag-drive {
  color: #0284c7 !important;
  border-color: rgba(2, 132, 199, 0.4) !important;
  background: rgba(2, 132, 199, 0.1) !important;
}

.tag-generic {
  color: #94a3b8 !important;
  border-color: rgba(148, 163, 184, 0.3) !important;
  background: rgba(148, 163, 184, 0.08) !important;
}

.tag-free {
  color: #16a34a !important;
  border-color: rgba(16, 185, 129, 0.45) !important;
  background: rgba(16, 185, 129, 0.1) !important;
}

.category-pill-toolbar {
  min-width: 0;
  width: 100%;
  justify-content: flex-start;
}

.category-filter-badge {
  display: inline-flex;
  align-items: center;
}

.category-filter-badge :deep(.v-btn) {
  width: 32px !important;
  height: 32px !important;
  border-radius: 6px !important;
}

.resource-filter-menu {
  border-radius: 12px !important;
}

.poster-media-type-badge {
  position: absolute !important;
  top: 6px !important;
  left: 6px !important;
  z-index: 5 !important;
  font-size: 0.68rem !important;
  font-weight: 700 !important;
  height: 20px !important;
  padding: 0 7px !important;
  border-radius: 4px !important;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.5) !important;
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  color: #ffffff !important;
  letter-spacing: 0.3px !important;
  line-height: 1 !important;
}

.poster-media-type-badge.badge--movie {
  background: linear-gradient(135deg, #0284c7, #0369a1) !important;
  border: 1px solid rgba(255, 255, 255, 0.3) !important;
}

.poster-media-type-badge.badge--tv {
  background: linear-gradient(135deg, #8b5cf6, #6366f1) !important;
  border: 1px solid rgba(255, 255, 255, 0.3) !important;
}

.poster-library-badge {
  position: absolute !important;
  top: 6px !important;
  right: 6px !important;
  z-index: 5 !important;
  font-size: 0.65rem !important;
  font-weight: 700 !important;
  height: 20px !important;
  padding: 0 6px !important;
  border-radius: 4px !important;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.5) !important;
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  letter-spacing: 0.2px !important;
  line-height: 1 !important;
  white-space: nowrap !important;
}

.poster-library-badge--in {
  background: linear-gradient(135deg, rgba(16, 185, 129, 0.95), rgba(5, 150, 105, 0.95)) !important;
  border: 1px solid rgba(255, 255, 255, 0.35) !important;
  color: #ffffff !important;
}

.poster-library-badge--out {
  background: rgba(15, 23, 42, 0.8) !important;
  border: 1px solid rgba(255, 255, 255, 0.18) !important;
  color: rgba(255, 255, 255, 0.72) !important;
}

.quality-tag-pill {
  display: inline-flex !important;
  flex: 0 0 auto !important;
  flex-shrink: 0 !important;
  align-items: center !important;
  justify-content: center !important;
  min-height: 22px;
  padding: 2.5px 8px;
  border-radius: 5px;
  font-size: 0.7rem;
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: 0.3px;
  white-space: nowrap;
  user-select: none;
  border: 1px solid rgba(100, 116, 139, 0.3);
  background: rgba(100, 116, 139, 0.08);
  color: #64748b;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
  transition: transform 0.12s ease;
}

.quality-tag-pill:hover {
  transform: translateY(-1px);
}

.tag-4k {
  color: #0284c7 !important;
  border-color: rgba(2, 132, 199, 0.42) !important;
  background: rgba(2, 132, 199, 0.12) !important;
  box-shadow: 0 1px 3px rgba(2, 132, 199, 0.1) !important;
}

.tag-1080p {
  color: #4f46e5 !important;
  border-color: rgba(79, 70, 229, 0.4) !important;
  background: rgba(79, 70, 229, 0.1) !important;
}

.tag-720p {
  color: #0284c7 !important;
  border-color: rgba(2, 132, 199, 0.3) !important;
  background: rgba(2, 132, 199, 0.08) !important;
}

.tag-dv {
  color: #9333ea !important;
  border-color: rgba(147, 51, 234, 0.45) !important;
  background: rgba(147, 51, 234, 0.12) !important;
  box-shadow: 0 1px 3px rgba(147, 51, 234, 0.1) !important;
}

.tag-hdr {
  color: #ea580c !important;
  border-color: rgba(234, 88, 12, 0.45) !important;
  background: rgba(234, 88, 12, 0.12) !important;
  box-shadow: 0 1px 3px rgba(234, 88, 12, 0.1) !important;
}

.tag-remux {
  color: #d97706 !important;
  border-color: rgba(217, 119, 6, 0.45) !important;
  background: rgba(217, 119, 6, 0.12) !important;
}

.tag-web {
  color: #475569 !important;
  border-color: rgba(71, 85, 105, 0.35) !important;
  background: rgba(71, 85, 105, 0.1) !important;
}

.tag-sub {
  color: #059669 !important;
  border-color: rgba(5, 150, 105, 0.45) !important;
  background: rgba(5, 150, 105, 0.12) !important;
  box-shadow: 0 1px 3px rgba(5, 150, 105, 0.1) !important;
}

.tag-dub {
  color: #e11d48 !important;
  border-color: rgba(225, 29, 72, 0.42) !important;
  background: rgba(225, 29, 72, 0.1) !important;
}

.tag-atmos {
  color: #0891b2 !important;
  border-color: rgba(8, 145, 178, 0.42) !important;
  background: rgba(8, 145, 178, 0.11) !important;
}

.tag-fps {
  color: #7c3aed !important;
  border-color: rgba(124, 58, 237, 0.42) !important;
  background: rgba(124, 58, 237, 0.11) !important;
}

.tag-generic {
  color: #64748b !important;
  border-color: rgba(100, 116, 139, 0.35) !important;
  background: rgba(100, 116, 139, 0.08) !important;
}

.resource-row-actions {
  min-width: max-content;
}

@media (max-width: 768px) {
  /* 移动端彻底隐藏详情里面的 ID 显示 */
  .hero-top-ids {
    display: none !important;
  }

  /* 移动端详情全屏视口强制充满，绝不留白边 */
  :global(.resource-detail-dialog.v-overlay .v-overlay__content),
  .resource-detail-dialog :deep(.v-overlay__content) {
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100% !important;
    min-height: 100% !important;
    max-height: 100% !important;
    margin: 0 !important;
    border-radius: 0 !important;
    top: 0 !important;
    left: 0 !important;
    position: fixed !important;
    overflow: hidden !important;
  }

  .detail-card {
    border-radius: 0 !important;
    border: none !important;
    height: 100% !important;
    max-height: 100% !important;
    width: 100vw !important;
  }

  /* 移动端浮动关闭按钮（右上角半透明磨砂徽章） */
  .detail-floating-close {
    top: 8px !important;
    right: 8px !important;
    left: auto !important;
    z-index: 40 !important;
    width: 28px !important;
    height: 28px !important;
    border-radius: 50% !important;
    background: rgba(15, 23, 42, 0.75) !important;
    backdrop-filter: blur(8px) !important;
    -webkit-backdrop-filter: blur(8px) !important;
    border: 1px solid rgba(255, 255, 255, 0.2) !important;
    color: #ffffff !important;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3) !important;
  }

  .detail-header-hero {
    min-height: unset !important;
    padding: 8px 10px 6px 10px !important;
    background: #090d16 !important;
    border-bottom: 1px solid rgba(var(--v-border-color), 0.12) !important;
  }

  .hero-content {
    flex-direction: row !important;
    align-items: flex-start !important;
    text-align: left !important;
    gap: 10px !important;
    padding: 0 !important;
  }

  /* 移动端精致海报（黄金比例 80x120px，适当宽高并强制图片完全充满容器） */
  .hero-poster-wrapper {
    flex: 0 0 80px !important;
    width: 80px !important;
    aspect-ratio: 2 / 3 !important;
    height: 120px !important;
    max-height: 120px !important;
    border-radius: 6px !important;
    overflow: hidden !important;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45) !important;
    border: 1px solid rgba(255, 255, 255, 0.16) !important;
    position: relative !important;
    background: #000000 !important;
  }

  .hero-poster {
    width: 100% !important;
    height: 100% !important;
    min-height: 100% !important;
    position: absolute !important;
    inset: 0 !important;
    display: block !important;
  }

  .hero-poster :deep(.v-responsive__sizer) {
    display: none !important;
  }

  .hero-poster :deep(.v-img__img),
  .hero-poster :deep(img) {
    width: 100% !important;
    height: 100% !important;
    object-fit: cover !important;
    object-position: center !important;
    display: block !important;
  }

  .poster-media-type-badge {
    position: absolute !important;
    top: 4px !important;
    left: 4px !important;
    z-index: 10 !important;
    font-size: 0.58rem !important;
    height: 16px !important;
    padding: 0 4px !important;
    border-radius: 3px !important;
  }

  .poster-library-badge {
    position: absolute !important;
    top: 4px !important;
    right: 4px !important;
    z-index: 10 !important;
    width: 20px !important;
    height: 20px !important;
    min-width: 20px !important;
    max-width: 20px !important;
    padding: 0 !important;
    border-radius: 50% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45) !important;
  }

  .poster-library-badge--in {
    background: #16a34a !important;
    border: none !important;
    color: #ffffff !important;
  }

  .poster-library-badge--out {
    background: rgba(15, 23, 42, 0.85) !important;
    border: 1px solid rgba(255, 255, 255, 0.3) !important;
    color: rgba(255, 255, 255, 0.7) !important;
  }

  .poster-library-badge :deep(.v-icon) {
    font-size: 13px !important;
    margin: 0 !important;
  }

  .poster-library-text {
    display: none !important;
  }

  /* 移动端右侧核心内容区 */
  .hero-text {
    flex: 1 1 auto !important;
    min-width: 0 !important;
    padding-right: 22px !important;
  }

  .hero-top-meta {
    display: flex !important;
    align-items: center !important;
    margin-bottom: 2px !important;
    gap: 4px !important;
  }

  .hero-library-merged {
    height: 18px !important;
    min-height: 18px !important;
    max-height: 18px !important;
    padding: 0 5px !important;
    font-size: 0.62rem !important;
    border-radius: 3px !important;
  }

  .hero-title-group {
    margin-bottom: 2px !important;
  }

  .hero-title-combined-row {
    display: flex !important;
    flex-direction: column !important;
    align-items: flex-start !important;
    gap: 2px !important;
  }

  .hero-title-row {
    display: flex !important;
    align-items: baseline !important;
    gap: 4px !important;
    width: 100% !important;
  }

  .hero-title {
    font-size: 1.02rem !important;
    font-weight: 750 !important;
    line-height: 1.24 !important;
    display: -webkit-box !important;
    -webkit-line-clamp: 1 !important;
    -webkit-box-orient: vertical !important;
    overflow: hidden !important;
    white-space: normal !important;
  }

  .hero-title-year {
    font-size: 0.82rem !important;
    font-weight: 600 !important;
  }

  /* 移动端评分与题材标签紧凑单行流 */
  .hero-meta-badges {
    display: flex !important;
    align-items: center !important;
    gap: 4px !important;
    flex-wrap: nowrap !important;
    overflow-x: auto !important;
    margin-top: 1px !important;
    max-width: 100% !important;
  }

  .meta-tag-pill {
    height: 18px !important;
    min-height: 18px !important;
    max-height: 18px !important;
    padding: 0 5px !important;
    font-size: 0.62rem !important;
    border-radius: 3px !important;
    flex-shrink: 0 !important;
  }

  .pill-brand-svg svg {
    height: 10px !important;
  }

  .rating-val {
    font-size: 0.65rem !important;
  }

  .rating-votes {
    display: none !important;
  }

  .hero-original-title {
    font-size: 0.65rem !important;
    margin-top: 1px !important;
    color: rgba(255, 255, 255, 0.6) !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
  }

  .hero-overview {
    display: -webkit-box !important;
    -webkit-line-clamp: 2 !important;
    -webkit-box-orient: vertical !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    font-size: 0.7rem !important;
    line-height: 1.35 !important;
    color: rgba(255, 255, 255, 0.72) !important;
    margin-top: 2px !important;
    margin-bottom: 0 !important;
  }

  /* 移动端选集栏精简化 */
  .media-episode-console {
    margin-top: 4px !important;
    padding: 4px 6px !important;
    border-radius: 5px !important;
    background: rgba(0, 0, 0, 0.25) !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
  }

  .console-header {
    margin-bottom: 3px !important;
    min-height: 16px !important;
  }

  .season-switch-tab {
    padding: 1px 5px !important;
    font-size: 0.62rem !important;
    border-radius: 3px !important;
  }

  .missing-count-badge {
    font-size: 0.62rem !important;
    padding: 1px 4px !important;
  }

  .console-episodes-track {
    gap: 3px !important;
    height: 22px !important;
    max-height: 22px !important;
    padding: 0 !important;
  }

  .media-ep-pill {
    min-width: 24px !important;
    height: 20px !important;
    font-size: 0.68rem !important;
    border-radius: 3px !important;
    padding: 0 3px !important;
  }

  /* 渠道与资源标签切换移动端优化 */
  .channel-tabs-container {
    padding: 0 4px !important;
  }

  .channel-tabs {
    min-height: 36px !important;
  }

  .channel-tab {
    font-size: 0.78rem !important;
    padding: 0 6px !important;
    min-width: unset !important;
  }

  .category-pill-group-container {
    padding: 3px 6px !important;
  }

  .category-pill-track {
    gap: 4px !important;
    padding: 0 2px !important;
    flex-wrap: nowrap !important;
    overflow-x: auto !important;
  }

  .category-pill-btn {
    padding: 2px 7px !important;
    font-size: 0.7rem !important;
    border-radius: 10px !important;
  }

  .pill-badge {
    min-width: 14px !important;
    height: 14px !important;
    font-size: 0.6rem !important;
  }

  /* ================= 移动端资源行系统：统一同行操作按钮，杜绝脏灰背景 ================= */
  .detail-body {
    padding: 6px 8px 12px 8px !important;
  }

  .resource-list {
    display: flex !important;
    flex-direction: column !important;
    gap: 4px !important;
  }

  .resource-row-item {
    padding: 6px 9px !important;
    border-radius: 6px !important;
    background: rgba(var(--v-theme-surface), 0.4) !important;
    border: 1px solid rgba(var(--v-border-color), 0.08) !important;
    box-shadow: none !important;
  }

  :global(html[data-theme="glass"]) .resource-row-item,
  :global(html[data-theme="transparent"]) .resource-row-item {
    background: rgba(255, 255, 255, 0.04) !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
  }

  .resource-title-text {
    font-size: 0.82rem !important;
    line-height: 1.3 !important;
    font-weight: 600 !important;
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
  }

  .resource-actions-group {
    gap: 3.5px !important;
  }

  .res-btn-icon {
    width: 26px !important;
    height: 26px !important;
    min-width: 26px !important;
    max-width: 26px !important;
    border-radius: 5px !important;
    padding: 0 !important;
    background: rgba(var(--v-theme-surface-variant), 0.3) !important;
  }

  .res-btn-icon :deep(.v-icon) {
    font-size: 13px !important;
  }

  .resource-row-meta {
    gap: 4px !important;
    margin-top: 3px !important;
  }

  .resource-size-badge {
    font-size: 0.72rem !important;
  }

  .resource-seeders-badge {
    font-size: 0.68rem !important;
  }

  .quality-tag-pill {
    padding: 1px 4px !important;
    font-size: 0.62rem !important;
    height: 18px !important;
    min-height: 18px !important;
    line-height: 18px !important;
    border-radius: 3px !important;
  }
}
</style>
