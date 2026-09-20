<template>
  <section class="media-grid-section">
    <div v-if="loading" class="state-container">
      <v-progress-circular indeterminate color="primary" size="44" width="3" />
      <span class="state-text">正在获取影视媒体数据...</span>
    </div>

    <div v-else-if="!items.length" class="state-container">
      <v-icon icon="mdi-movie-search-outline" size="64" color="medium-emphasis" />
      <p class="empty-title">暂未检索到符合条件的影视媒体</p>
      <v-btn v-if="searchMode" variant="tonal" color="primary" size="small" class="mt-2" @click="$emit('exit-search')">
        返回推荐榜单
      </v-btn>
    </div>

    <div v-else class="media-grid">
      <article
        v-for="item in items"
        :key="mediaItemKey(item)"
        class="media-card"
        data-glass-optical-mode="excluded"
        role="button"
        tabindex="0"
        :aria-label="`查看${item.title || '媒体'}资源详情`"
        @click="$emit('open', item)"
        @keydown.enter.prevent="$emit('open', item)"
        @keydown.space.prevent="$emit('open', item)">
        <div class="poster-wrapper">
          <v-img
            :src="item._currentPoster || item.poster_url"
            referrerpolicy="no-referrer"
            aspect-ratio="2/3"
            cover
            transition="fade-transition"
            class="media-poster"
            @load="item._imgLoaded = true"
            @error="handlePosterError(item)">
            <template #placeholder>
              <div class="poster-placeholder">
                <v-icon v-if="item._imgFailed" icon="mdi-image-off-outline" size="26" color="medium-emphasis" />
              </div>
            </template>
          </v-img>

          <!-- 类型徽章 -->
          <div :class="['type-badge', item.media_type]">
            {{ item.media_type === "movie" ? "电影" : "剧集" }}
            <span v-if="item.media_type === 'tv' && item.in_library && getLibraryEpisodesCount(item)">
              · {{ getLibraryEpisodesCount(item) }}集
            </span>
          </div>

          <!-- 入库/评分徽章 -->
          <template v-if="item.in_library">
            <div class="library-badge" :class="{ 'hide-on-card-hover': getRatingValue(item) > 0 }">
              <v-icon icon="mdi-check" size="11" class="library-badge-icon mr-0.5" />
              <span class="library-badge-text">已入库</span>
            </div>
            <div v-if="getRatingValue(item) > 0" class="rating-badge rating-badge--hover-swap">
              <v-icon icon="mdi-star" size="12" color="amber" class="mr-0.5" />
              {{ getRatingText(item) }}
            </div>
          </template>
          <div v-else-if="getRatingValue(item) > 0" class="rating-badge">
            <v-icon icon="mdi-star" size="12" color="amber" class="mr-0.5" />
            {{ getRatingText(item) }}
          </div>
          <div v-if="item.media_type === 'tv' && hasMissingEpisodes(item)" class="missing-badge">缺集</div>
          <div class="media-caption">
            <h3 class="media-title" :title="item.title">{{ item.title }}</h3>
            <span v-if="item.year" class="media-year">{{ item.year }}</span>
          </div>
          <div class="card-overlay">
            <v-btn
              color="primary"
              variant="flat"
              size="small"
              rounded="pill"
              prepend-icon="mdi-magnify"
              class="search-btn"
              @click.stop="$emit('open', item)">
              搜索资源
            </v-btn>
          </div>
        </div>
      </article>
    </div>
    <div v-if="allItems.length && !searchMode && hasMore" ref="sentinel" class="scroll-sentinel" aria-hidden="true" />
    <div v-if="allItems.length && !searchMode" class="pagination-footer">
      <div v-if="loadingMore" class="d-flex align-center ga-2">
        <v-progress-circular indeterminate size="20" width="2" color="primary" />
        <span>正在加载更多推荐影视...</span>
      </div>
      <div v-else-if="!hasMore" class="end-text">— 已加载全部推荐内容 —</div>
      <v-btn v-else variant="text" size="small" color="primary" @click="$emit('load-more')">点击加载更多</v-btn>
    </div>

    <div v-if="allItems.length && searchMode" class="pagination-footer">
      — 共检索到 {{ items.length }} 条相关影视结果 —
    </div>
  </section>
</template>

<script setup>
import {nextTick, onMounted, onUnmounted, ref, watch} from "vue";
import {
  getLibraryEpisodesCount,
  getRatingText,
  getRatingValue,
  handlePosterError,
  hasMissingEpisodes,
  mediaItemKey,
} from "../../composables/resourceUtils";

const props = defineProps({
  items: {type: Array, default: () => []},
  allItems: {type: Array, default: () => []},
  loading: Boolean,
  loadingMore: Boolean,
  hasMore: Boolean,
  searchMode: Boolean,
});

const emit = defineEmits(["open", "load-more", "exit-search", "sentinel"]);

const sentinel = ref(null);
let observer = null;
let scrollThrottleTimer = null;

function triggerLoadMore() {
  if (props.loading || props.loadingMore || !props.hasMore || props.searchMode) return;
  if (!props.allItems.length) return;
  emit("load-more");
}

function setupObserver(el) {
  if (observer) {
    observer.disconnect();
    observer = null;
  }
  if (!el || props.searchMode || !props.hasMore) return;
  observer = new IntersectionObserver(
    (entries) => {
      if (entries[0]?.isIntersecting) {
        triggerLoadMore();
      }
    },
    {root: null, rootMargin: "600px"},
  );
  observer.observe(el);
}

function handleScroll() {
  if (props.searchMode || !props.hasMore || props.loading || props.loadingMore) return;
  if (scrollThrottleTimer) return;
  scrollThrottleTimer = setTimeout(() => {
    scrollThrottleTimer = null;
    if (props.searchMode || !props.hasMore || props.loading || props.loadingMore) return;
    const scrollBottom = document.documentElement.scrollHeight - (window.scrollY + window.innerHeight);
    if (scrollBottom < 700) {
      triggerLoadMore();
    }
  }, 100);
}

watch(sentinel, (el) => {
  setupObserver(el);
  emit("sentinel", el);
});

watch(
  () => [props.searchMode, props.hasMore, props.allItems.length],
  ([searchMode, hasMore]) => {
    if (searchMode || !hasMore) {
      if (observer) {
        observer.disconnect();
        observer = null;
      }
    } else {
      nextTick(() => {
        setupObserver(sentinel.value);
        emit("sentinel", sentinel.value);
      });
    }
  },
);

onMounted(() => {
  window.addEventListener("scroll", handleScroll, {passive: true});
  nextTick(() => {
    setupObserver(sentinel.value);
    emit("sentinel", sentinel.value);
  });
});

onUnmounted(() => {
  window.removeEventListener("scroll", handleScroll);
  if (scrollThrottleTimer) clearTimeout(scrollThrottleTimer);
  if (observer) {
    observer.disconnect();
    observer = null;
  }
});
</script>

<style scoped>
.media-grid-section {
  position: relative;
  min-height: 120px;
  width: 100%;
}

.state-container {
  display: flex;
  min-height: 280px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  text-align: center;
}

.state-text,
.empty-title {
  font-size: 0.88rem;
  color: rgba(var(--v-theme-on-surface), 0.72);
  margin: 0;
}

/* 媒体卡片网格布局 */
.media-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(144px, 1fr));
  gap: 16px;
  align-items: start;
}

/* 媒体卡片 */
.media-card {
  position: relative;
  min-width: 0;
  overflow: hidden;
  border-radius: 10px;
  background: rgb(var(--v-theme-surface));
  border: 1px solid rgba(var(--v-border-color), 0.12);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
  cursor: pointer;
  transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1),
  box-shadow 0.22s ease,
  border-color 0.22s ease;
  user-select: none;
}

.media-card:hover,
.media-card:focus-visible {
  transform: translateY(-4px);
  box-shadow: 0 10px 24px rgba(0, 0, 0, 0.2);
  border-color: rgba(var(--v-theme-primary), 0.45);
  outline: none;
}

/* 毛玻璃与透明主题适配 */
:global(html[data-theme="glass"]) .media-card,
:global(html[data-theme="transparent"]) .media-card {
  background: rgb(var(--v-theme-surface)) !important;
  border-color: var(--glass-border, rgba(255, 255, 255, 0.16));
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
}

.poster-wrapper {
  position: relative;
  overflow: hidden;
  aspect-ratio: 2/3;
  width: 100%;
  background: transparent !important;
}

.media-poster {
  width: 100%;
  height: 100%;
  display: block;
  background-color: transparent !important;
}

/* 排除全局滤镜干扰 */
.poster-wrapper,
.media-poster,
.media-poster :deep(.v-img__img),
.media-poster :deep(img) {
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  filter: none !important;
}

.media-poster :deep(.v-img__img),
.media-poster :deep(img) {
  opacity: 1 !important;
  transition: opacity 0.35s ease-in-out,
  transform 0.35s cubic-bezier(0.25, 0.8, 0.25, 1) !important;
}

.media-card:hover .media-poster :deep(.v-img__img),
.media-card:focus-visible .media-poster :deep(.v-img__img) {
  transform: scale(1.05);
}

.poster-placeholder {
  display: flex;
  width: 100%;
  height: 100%;
  align-items: center;
  justify-content: center;
  background: linear-gradient(
    110deg,
    rgba(var(--v-theme-surface-variant), 0.22) 8%,
    rgba(var(--v-theme-surface-variant), 0.38) 18%,
    rgba(var(--v-theme-surface-variant), 0.22) 33%
  );
  background-size: 200% 100%;
  animation: shimmer-skeleton 1.5s infinite linear;
}

@keyframes shimmer-skeleton {
  0% {
    background-position: -200% 0;
  }
  100% {
    background-position: 200% 0;
  }
}

/* 徽章基础样式 */
.type-badge,
.rating-badge,
.library-badge,
.missing-badge {
  position: absolute;
  z-index: 5;
  display: inline-flex;
  align-items: center;
  padding: 2.5px 7px;
  border-radius: 6px;
  font-size: 0.7rem;
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: 0.2px;
  pointer-events: none;
}

/* 左上角：类型 */
.type-badge {
  top: 7px;
  left: 7px;
  background: rgba(15, 23, 42, 0.78);
  color: #f8fafc;
  border: 1px solid rgba(255, 255, 255, 0.14);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
}

/* 右上角：评分 */
.rating-badge {
  top: 7px;
  right: 7px;
  background: rgba(15, 23, 42, 0.78);
  color: #facc15;
  border: 1px solid rgba(255, 255, 255, 0.14);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
}

/* 右上角：已入库 */
.library-badge {
  top: 7px;
  right: 7px;
  background: #16a34a;
  color: #fff;
  box-shadow: 0 2px 6px rgba(22, 163, 74, 0.38);
  transition: opacity 0.22s cubic-bezier(0.4, 0, 0.2, 1),
  transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
}

/* 悬停时隐藏已入库徽章 */
.media-card:hover .library-badge.hide-on-card-hover,
.media-card:focus-visible .library-badge.hide-on-card-hover {
  opacity: 0;
  transform: scale(0.85);
  pointer-events: none;
}

/* 悬停时浮现评分徽章 */
.rating-badge--hover-swap {
  opacity: 0;
  transform: scale(0.85);
  transition: opacity 0.22s cubic-bezier(0.4, 0, 0.2, 1),
  transform 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  pointer-events: none;
}

.media-card:hover .rating-badge--hover-swap,
.media-card:focus-visible .rating-badge--hover-swap {
  opacity: 1;
  transform: scale(1);
}

/* 缺集徽章 */
.missing-badge {
  bottom: 34px;
  right: 7px;
  background: #b45309;
  color: #fff;
  box-shadow: 0 2px 6px rgba(180, 83, 9, 0.35);
}

/* 海报底部常驻渐变遮罩与标题年份 */
.media-caption {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 2;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 6px;
  padding: 30px 8px 8px 8px;
  background: linear-gradient(180deg, transparent 0%, rgba(15, 23, 42, 0.75) 60%, rgba(15, 23, 42, 0.95) 100%);
  pointer-events: none;
}

.media-title {
  min-width: 0;
  flex: 1;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 0.8rem;
  font-weight: 600;
  line-height: 1.25;
  color: #fff;
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.7);
}

.media-year {
  flex-shrink: 0;
  font-size: 0.7rem;
  color: rgba(255, 255, 255, 0.75);
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.7);
}

/* 悬停操作浮层 */
.card-overlay {
  position: absolute;
  inset: 0;
  z-index: 4;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.26);
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
  opacity: 0;
  transition: opacity 0.22s cubic-bezier(0.4, 0, 0.2, 1);
  pointer-events: none;
}

.media-card:hover .card-overlay,
.media-card:focus-visible .card-overlay {
  opacity: 1;
  pointer-events: auto;
}

.search-btn {
  transform: translateY(6px);
  transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1),
  box-shadow 0.22s ease !important;
  box-shadow: 0 4px 14px rgba(var(--v-theme-primary), 0.45) !important;
  font-weight: 600 !important;
  letter-spacing: 0.3px !important;
}

.media-card:hover .search-btn,
.media-card:focus-visible .search-btn {
  transform: translateY(0);
}

.scroll-sentinel {
  height: 24px;
  visibility: hidden;
}

.pagination-footer {
  display: flex;
  min-height: 52px;
  align-items: center;
  justify-content: center;
  color: rgba(var(--v-theme-on-surface), 0.65);
  font-size: 0.78rem;
}

.end-text {
  color: rgba(var(--v-theme-on-surface), 0.5);
  letter-spacing: 0.3px;
}

/* 响应式断点 */
@media (max-width: 960px) {
  .media-grid {
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 12px;
  }
}

@media (max-width: 600px) {
  .media-grid {
    grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
    gap: 8px;
  }

  .media-caption {
    padding: 24px 6px 6px 6px;
  }

  .media-title {
    font-size: 0.75rem;
  }

  .media-year {
    font-size: 0.65rem;
  }

  .type-badge,
  .rating-badge,
  .library-badge,
  .missing-badge {
    padding: 1.5px 5px;
    font-size: 0.64rem;
  }

  .library-badge {
    width: 18px !important;
    height: 18px !important;
    min-width: 18px !important;
    padding: 0 !important;
    border-radius: 50% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    background: #16a34a !important;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.4) !important;
  }

  .library-badge-text {
    display: none !important;
  }

  .library-badge-icon {
    margin: 0 !important;
    font-size: 12px !important;
  }
}
</style>
