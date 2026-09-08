<template>
  <v-dialog
    :model-value="modelValue"
    :fullscreen="isMobile"
    max-width="680"
    class="manual-resource-dialog"
    @update:model-value="close">
    <v-card rounded="lg" class="manual-resource-card">
      <v-card-title class="text-subtitle-1 d-flex align-center ga-2">
        <v-icon
          :icon="actionMode === 'upgrade' ? 'mdi-auto-fix' : 'mdi-link-variant-plus'"
          color="primary"
          size="small" />
        {{ actionMode === "upgrade" ? "媒体洗版" : "手动添加" }}
      </v-card-title>
      <v-card-text>
        <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-3">{{ error }}</v-alert>

        <v-btn-toggle v-model="actionMode" mandatory divided color="primary" density="compact" class="mb-4">
          <v-btn value="links" prepend-icon="mdi-link-variant">添加资源</v-btn>
          <v-btn v-if="enableCloudUpgrade" value="upgrade" prepend-icon="mdi-auto-fix">媒体洗版</v-btn>
        </v-btn-toggle>

        <template v-if="actionMode === 'links'">
          <v-text-field
            v-if="lockedInitialMedia"
            :model-value="lockedManualTargetLabel"
            :label="lockedSubscribe ? '订阅卡片' : '历史媒体'"
            variant="outlined"
            density="compact"
            readonly
            hide-details="auto"
            class="mb-3" />

          <v-autocomplete
            v-else
            v-model="selectedTarget"
            v-model:search="targetSearch"
            :items="targetCandidates"
            :item-title="targetTitle"
            return-object
            :no-filter="true"
            label="指定媒体"
            placeholder="搜索订阅或 TMDB 媒体"
            variant="outlined"
            density="compact"
            :loading="loadingOptions || searchingTmdb || loadingTmdbSeasons"
            :disabled="submitting"
            no-data-text="没有匹配订阅，可查询 TMDB"
            hide-details="auto"
            class="mb-3"
            @keyup.enter="searchTmdb">
            <template #append-inner>
              <v-btn
                icon="mdi-movie-search"
                variant="text"
                size="small"
                title="查询 TMDB"
                :loading="searchingTmdb"
                :disabled="submitting || !targetSearch.trim()"
                @click.stop="searchTmdb" />
            </template>
          </v-autocomplete>

          <v-select
            v-if="targetMediaType === 'tv'"
            v-model="seasons"
            :items="availableSeasons"
            :loading="loadingTmdbSeasons"
            multiple
            chips
            closable-chips
            :disabled="submitting || loadingTmdbSeasons || !availableSeasons.length"
            label="季"
            placeholder="留空默认全部季"
            variant="outlined"
            density="compact"
            hide-details="auto"
            class="mb-3"
            @update:model-value="updateSeasons" />

          <v-textarea
            :model-value="resourceLinks"
            :label="selectedCloudResource ? '网盘路径' : '资源链接'"
            placeholder="每行一个115分享、ED2K或Magnet链接"
            :hint="
              selectedCloudResource && !selectedCloudResourceAllowed
                ? '当前媒体类型未启用跨盘转存，请重新选择目标网盘路径。'
                : selectedCloudResource
                  ? '目标盘路径直接整理；其他网盘路径通过跨盘转存后整理。'
                  : '支持单个或多个资源包，单次最多50条；也可从右侧选择网盘路径。'
            "
            persistent-hint
            auto-grow
            rows="5"
            variant="outlined"
            density="compact"
            :class="{ 'cloud-resource-input': selectedCloudResource }"
            :readonly="Boolean(selectedCloudResource)"
            :disabled="submitting"
            @update:model-value="updateResourceInput">
            <template #append-inner>
              <div class="resource-path-actions">
                <v-btn
                  icon="mdi-folder-open"
                  variant="text"
                  size="small"
                  title="选择网盘路径"
                  :disabled="submitting || !selectableCloudDrives.length"
                  @click.stop="cloudDirectoryVisible = true" />
                <v-btn
                  v-if="selectedCloudResource"
                  icon="mdi-delete-outline"
                  variant="text"
                  size="small"
                  color="error"
                  title="删除网盘路径"
                  :disabled="submitting"
                  @click.stop="clearCloudResource" />
              </div>
            </template>
          </v-textarea>
          <v-checkbox
            v-model="manualUpgrade"
            label="将手动资源作为洗版候选"
            density="compact"
            hide-details
            class="mt-2"
            :disabled="submitting" />
          <v-checkbox
            v-model="skipHistory"
            label="不记录到历史列表"
            density="compact"
            hide-details
            :disabled="submitting" />
        </template>

        <template v-else>
          <v-alert type="info" variant="tonal" density="compact" class="mb-3">
            在所选媒体服务器的全部媒体库中直接搜索，仅显示实际路径位于插件媒体根路径内的条目；也可通过 TMDB
            快速精确匹配。
          </v-alert>
          <v-select
            v-model="mediaServer"
            :items="mediaServers"
            item-title="title"
            item-value="value"
            label="媒体服务器"
            placeholder="请选择 Emby 等媒体服务器"
            variant="outlined"
            density="compact"
            :loading="loadingMediaServers"
            :disabled="submitting"
            no-data-text="没有已启用的媒体服务器"
            hide-details="auto"
            class="mb-3" />
          <div v-if="lockedInitialMedia" class="manual-search-row mb-3">
            <v-text-field
              :model-value="lockedMediaLabel"
              label="历史媒体"
              variant="outlined"
              density="compact"
              readonly
              hide-details />
            <v-btn
              color="primary"
              variant="tonal"
              :loading="loadingMedia"
              :disabled="!mediaServer || submitting"
              @click="searchMediaContents(initialMedia)">
              刷新
            </v-btn>
          </div>
          <div v-else class="manual-search-row mb-3">
            <v-text-field
              v-model="mediaKeyword"
              label="搜索媒体库内容"
              placeholder="输入电影或电视剧名称"
              variant="outlined"
              density="compact"
              hide-details
              :disabled="!mediaServer || submitting"
              @keyup.enter="searchMediaContents" />
            <v-btn
              color="primary"
              variant="tonal"
              :loading="loadingMedia"
              :disabled="!mediaServer || !mediaKeyword.trim() || submitting"
              @click="searchMediaContents">
              搜索
            </v-btn>
            <v-btn
              color="secondary"
              variant="tonal"
              :loading="matchingUpgradeTmdb"
              :disabled="!mediaServer || !mediaKeyword.trim() || submitting"
              @click="matchUpgradeTmdb">
              TMDB
            </v-btn>
          </div>
          <v-autocomplete
            v-if="!lockedInitialMedia && upgradeTmdbCandidates.length"
            v-model="selectedUpgradeTmdb"
            :items="upgradeTmdbCandidates"
            :item-title="candidateTitle"
            return-object
            label="TMDB 快速匹配"
            placeholder="选择准确条目后自动匹配所选服务器中的媒体"
            variant="outlined"
            density="compact"
            :disabled="submitting"
            no-data-text="没有 TMDB 候选"
            hide-details="auto"
            class="mb-3" />
          <v-card variant="outlined" class="media-selection-card">
            <div class="media-selection-header">
              <span class="text-body-2 font-weight-medium">选择媒体内容</span>
              <v-spacer />
              <v-checkbox-btn
                v-if="mediaContents.length"
                :model-value="allMediaSelected"
                :indeterminate="someMediaSelected && !allMediaSelected"
                density="compact"
                color="primary"
                :disabled="submitting"
                aria-label="全选媒体内容"
                @update:model-value="toggleAllMediaItems" />
            </div>
            <v-divider />
            <div v-if="loadingMedia" class="media-selection-state">
              <v-progress-circular indeterminate color="primary" size="28" />
            </div>
            <v-list v-else-if="mediaContents.length" density="compact" class="media-selection-list">
              <v-list-item
                v-for="item in mediaContents"
                :key="mediaItemKey(item)"
                :disabled="submitting"
                @click="toggleMediaItem(item)">
                <template #prepend>
                  <v-checkbox-btn
                    :model-value="isMediaItemSelected(item)"
                    density="compact"
                    color="primary"
                    tabindex="-1"
                    @click.stop="toggleMediaItem(item)" />
                </template>
                <v-list-item-title>{{ mediaItemTitle(item) }}</v-list-item-title>
                <v-list-item-subtitle>{{ mediaItemSubtitle(item) }}</v-list-item-subtitle>
              </v-list-item>
            </v-list>
            <div v-else class="media-selection-state text-body-2 text-medium-emphasis">
              请先选择媒体服务器并搜索；不会跨服务器查询
            </div>
          </v-card>
          <div class="text-caption text-medium-emphasis mt-2">
            可勾选一个或多个电影、整季或单集；提交时会再次校验媒体服务器条目及实际路径。
          </div>
        </template>
      </v-card-text>
      <v-card-actions class="px-4 pb-3">
        <v-spacer />
        <v-btn variant="text" :disabled="submitting" @click="close(false)">取消</v-btn>
        <v-btn
          color="primary"
          :prepend-icon="actionMode === 'upgrade' ? 'mdi-auto-fix' : 'mdi-play'"
          :loading="submitting"
          :disabled="!canSubmit"
          @click="requestSubmit">
          {{ actionMode === "upgrade" ? "开始洗版" : "开始处理" }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog v-model="confirmVisible" max-width="440" persistent>
    <v-card rounded="lg">
      <v-card-title class="text-subtitle-1">确认媒体洗版</v-card-title>
      <v-card-text>
        确认对所选 {{ selectedMediaItems.length }} 个媒体内容发起洗版？
        <v-alert type="warning" variant="tonal" density="compact" class="mt-3">
          将按插件洗版设置比较候选版本；符合条件时会进入替换或共存流程。
        </v-alert>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" :disabled="submitting" @click="confirmVisible = false">取消</v-btn>
        <v-btn color="warning" :loading="submitting" @click="submit">确认洗版</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
  <CloudDirectoryDialog
    v-model="cloudDirectoryVisible"
    :api="api"
    :plugin-id="pluginId"
    :provider="selectedCloudResource?.provider || targetCloudDrive"
    :target-provider="targetCloudDrive"
    :providers="selectableCloudDrives"
    :initial-path="selectedCloudResource?.path || '/'"
    title="选择待整理的网盘路径"
    :allow-create="false"
    @select="selectCloudPath" />
</template>

<script setup>
import {computed, ref, watch} from "vue";
import {useDisplay} from "vuetify";
import CloudDirectoryDialog from "./CloudDirectoryDialog.vue";

const props = defineProps({
  modelValue: Boolean,
  api: { type: [Object, Function], required: true },
  pluginId: { type: String, default: "PanSearch" },
  initialMode: { type: String, default: "links" },
  initialMedia: { type: Object, default: null },
})
const emit = defineEmits(["update:modelValue", "started"])
const display = useDisplay()
const isMobile = computed(() => display.xs.value)
const subscribes = ref([])
const subscribeId = ref(null)
const selectedTarget = ref(null);
const targetSearch = ref("");
const actionMode = ref("links")
const resourceLinks = ref("")
const cloudDrives = ref([]);
const targetCloudDrive = ref("");
const crossTransferMediaTypes = ref([]);
const enableCloudUpgrade = ref(false);
const selectedCloudResource = ref(null);
const cloudDirectoryVisible = ref(false);
const manualUpgrade = ref(false)
const skipHistory = ref(false);
const tmdbCandidates = ref([])
const selectedMedia = ref(null)
const seasons = ref([]);
const availableSeasons = ref([]);
const parsedSeasons = ref([]);
const mediaServers = ref([])
const mediaServer = ref(null)
const mediaKeyword = ref("")
const mediaContents = ref([])
const selectedMediaItems = ref([])
const upgradeTmdbCandidates = ref([])
const selectedUpgradeTmdb = ref(null)
const loadingOptions = ref(false)
const loadingMediaServers = ref(false)
const loadingMedia = ref(false)
const searchingTmdb = ref(false)
const loadingTmdbSeasons = ref(false);
const matchingUpgradeTmdb = ref(false)
const submitting = ref(false)
const confirmVisible = ref(false)
const error = ref("")
let cloudMediaResolveRevision = 0;
let tmdbSeasonRevision = 0;
let resourceResolveTimer = null;
let resourceResolveRevision = 0;

const targetCandidates = computed(() => {
  const keyword = normalizeSearchText(targetSearch.value);
  const subscriptionItems = subscribes.value
    .filter((item) => !keyword || normalizeSearchText(`${item?.title || ""} ${item?.value || ""}`).includes(keyword))
    .map((item) => ({...item, target_kind: "subscribe", target_key: `subscribe:${item.value}`}))
  const tmdbItems = tmdbCandidates.value.map((item) => ({
    ...item,
    target_kind: "tmdb",
    target_key: `tmdb:${item.media_type}:${item.tmdb_id}`,
  }))
  return [...subscriptionItems, ...tmdbItems];
})

const targetMediaType = computed(() => String(selectedTarget.value?.media_type || ""));
const manualMediaType = targetMediaType;

const selectableCloudDrives = computed(() => {
  const mediaType = manualMediaType.value;
  if (!mediaType) return cloudDrives.value;
  return cloudDrives.value.filter((item) => item.mode !== "cross" || crossTransferMediaTypes.value.includes(mediaType));
})

const selectedCloudResourceAllowed = computed(
  () =>
    !selectedCloudResource.value ||
    selectableCloudDrives.value.some((item) => item.value === selectedCloudResource.value.provider),
)

const selectedMediaKeys = computed(() => new Set(selectedMediaItems.value.map(mediaItemKey)))

const allMediaSelected = computed(
  () =>
    mediaContents.value.length > 0 &&
    mediaContents.value.every((item) => selectedMediaKeys.value.has(mediaItemKey(item))),
)

const someMediaSelected = computed(() =>
  mediaContents.value.some((item) => selectedMediaKeys.value.has(mediaItemKey(item))),
)

const lockedInitialMedia = computed(() => props.initialMode === "upgrade" && Boolean(props.initialMedia?.title))

const lockedMediaLabel = computed(() => {
  if (!lockedInitialMedia.value) return ""
  const media = props.initialMedia || {}
  const title = `${media.title || "未知媒体"}${media.year ? ` (${media.year})` : ""}`
  const type = media.media_type === "movie" ? "电影" : media.media_type === "tv" ? "电视剧" : ""
  return [title, type].filter(Boolean).join(" · ")
})

const lockedSubscribe = computed(() => {
  if (!lockedInitialMedia.value || !subscribeId.value) return null
  return subscribes.value.find((item) => item.value === subscribeId.value) || null
})

const lockedManualTargetLabel = computed(() => lockedSubscribe.value?.title || lockedMediaLabel.value)

const canSubmit = computed(() => {
  if (submitting.value) return false
  if (actionMode.value === "upgrade") return selectedMediaItems.value.length > 0
  if (!resourceLinks.value.trim()) return false
  if (!selectedCloudResourceAllowed.value) return false;
  if (!selectedTarget.value?.tmdb_id) return false;
  return true;
})

function normalizeSeasons(values) {
  const normalized = [];
  for (const value of Array.isArray(values) ? values : [values]) {
    for (const token of String(value ?? "").split(/[,，\s]+/)) {
      const season = Number(token);
      if (Number.isInteger(season) && season > 0 && season <= 999 && !normalized.includes(season)) {
        normalized.push(season);
      }
    }
  }
  return normalized.sort((left, right) => left - right);
}

function updateSeasons(value) {
  seasons.value = normalizeSeasons(value);
}

function normalizeSearchText(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/g, "")
}

function unwrap(result) {
  return result?.data && typeof result.data === "object" && "success" in result.data ? result.data : result || {}
}

function candidateTitle(item) {
  if (!item) return ""
  const year = item.year ? ` (${item.year})` : ""
  return `${item.title || "未知媒体"}${year} · ${item.media_type_name || ""}`
}

function targetTitle(item) {
  return item?.target_kind === "subscribe" ? String(item.title || "指定订阅") : `TMDB · ${candidateTitle(item)}`;
}

function findMatchingSubscribe(media, season = null) {
  const candidates = subscribes.value.filter((item) => {
    if (String(item?.media_type || "") !== String(media?.media_type || "")) return false;
    if (Number(item?.tmdb_id || 0) !== Number(media?.tmdb_id || 0)) return false;
    return season === null || Number(item?.season || 0) === Number(season);
  })
  return candidates.length === 1 ? candidates[0] : null;
}

function applyRecognizedMedia(media, seasonValues = []) {
  const parsed = normalizeSeasons(seasonValues);
  const matched = findMatchingSubscribe(media, parsed.length === 1 ? parsed[0] : null);
  if (matched) {
    selectedTarget.value = {
      ...matched,
      target_kind: "subscribe",
      target_key: `subscribe:${matched.value}`,
    }
    subscribeId.value = matched.value;
    selectedMedia.value = matched;
    parsedSeasons.value = parsed;
    return;
  }
  const target = {
    ...media,
    target_kind: "tmdb",
    target_key: `tmdb:${media.media_type}:${media.tmdb_id}`,
  }
  selectedTarget.value = target;
  subscribeId.value = null;
  selectedMedia.value = target;
  parsedSeasons.value = parsed;
}

function mediaItemKey(item) {
  return [item?.server, item?.item_id, item?.kind, item?.season, item?.episode]
    .map((value) => String(value ?? ""))
    .join(":")
}

function mediaItemTitle(item) {
  const title = String(item?.title || "未知媒体")
  const season = Number(item?.season || 0)
  const episode = Number(item?.episode || 0)
  if (item?.kind === "season") return `${title} S${String(season).padStart(2, "0")} · 整季`
  if (item?.kind === "episode") {
    return `${title} S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`
  }
  return item?.year ? `${title} (${item.year})` : title
}

function mediaItemSubtitle(item) {
  const details = []
  if (item?.kind !== "movie" && item?.year) details.push(String(item.year))
  if (item?.path) details.push(String(item.path))
  return details.join(" · ")
}

function isMediaItemSelected(item) {
  return selectedMediaKeys.value.has(mediaItemKey(item))
}

function toggleMediaItem(item) {
  const key = mediaItemKey(item)
  selectedMediaItems.value = isMediaItemSelected(item)
    ? selectedMediaItems.value.filter((selected) => mediaItemKey(selected) !== key)
    : [...selectedMediaItems.value, item]
}

function toggleAllMediaItems(selected) {
  selectedMediaItems.value = selected ? [...mediaContents.value] : []
}

function cloudResourceLabel(resource) {
  const provider = cloudDrives.value.find((item) => item.value === resource?.provider);
  return `网盘路径：[${provider?.title || resource?.provider || "目标网盘"}] ${resource?.path || "/"}`;
}

function updateResourceInput(value) {
  const text = String(value || "");
  if (selectedCloudResource.value && text !== cloudResourceLabel(selectedCloudResource.value)) {
    selectedCloudResource.value = null;
  }
  resourceLinks.value = text;
  if (!selectedCloudResource.value) scheduleResourceTargetResolve(text);
}

function scheduleResourceTargetResolve(text) {
  if (resourceResolveTimer) window.clearTimeout(resourceResolveTimer);
  const revision = ++resourceResolveRevision;
  if (!/(?:https?:\/\/|ed2k:\/\/|magnet:\?)/i.test(text)) return;
  resourceResolveTimer = window.setTimeout(() => {
    resourceResolveTimer = null;
    void resolveResourceTarget(text, revision);
  }, 450)
}

async function resolveResourceTarget(text, revision) {
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/sync/manual/resolve`, {
        resource_links: text.split(/\r?\n/),
      }),
    )
    if (revision !== resourceResolveRevision) return;
    if (result.success === false) throw new Error(result.message || "资源识别失败");
    const data = result.data || {};
    targetSearch.value = String(data.title || targetSearch.value).trim();
    parsedSeasons.value = normalizeSeasons(data.seasons || []);
    const matchedId = Number(data.subscribe_id || 0);
    const matched = matchedId > 0 ? subscribes.value.find((item) => Number(item.value) === matchedId) : null;
    tmdbCandidates.value = Array.isArray(data.candidates) ? data.candidates : [];
    if (matched) {
      selectedTarget.value = {
        ...matched,
        seasons: normalizeSeasons(data.available_seasons || []),
        target_kind: "subscribe",
        target_key: `subscribe:${matched.value}`,
      }
    } else if (tmdbCandidates.value.length === 1) {
      applyRecognizedMedia(tmdbCandidates.value[0], parsedSeasons.value);
    } else {
      selectedTarget.value = null;
      selectedMedia.value = null;
    }
  } catch (e) {
    if (revision === resourceResolveRevision) error.value = e.message || "资源识别失败";
  }
}

function parseCloudPathMedia(path) {
  const segments = String(path || "")
    .split("/")
    .map((value) => value.trim())
    .filter(Boolean)
  const directoryName = segments[segments.length - 1] || "";
  const marker = /\{\s*tmdb\s*id\s*[-_:]?\s*(\d+)\s*\}/i.exec(directoryName);
  if (!marker) return null;
  let title = directoryName
    .slice(0, marker.index)
    .replace(/^\s*[A-Za-z]\s+(?=\p{Script=Han})/u, "")
    .replace(/[._\-\s]+$/g, "")
    .trim()
  if (!title) return null;
  const seasonMatch = /(?:^|[\s._()[\]-])S(?:eason)?\s*0*(\d{1,3})(?=$|[\s._()[\]E{-])/i.exec(directoryName);
  const seasonNumber = seasonMatch ? Math.max(1, Number(seasonMatch[1])) : null;
  return {
    tmdbId: Number(marker[1]),
    title,
    season: seasonNumber,
    preferTv: /(?:更新|连载|\.更\s*\d+)/.test(directoryName),
  }
}

async function requestTmdbCandidates(title, tmdbId = 0, mediaType = "") {
  const result = unwrap(
    await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
      title,
      tmdb_id: Number(tmdbId || 0) || null,
      media_type: mediaType || null,
    }),
  )
  if (result.success === false) throw new Error(result.message || "TMDB 搜索失败");
  return Array.isArray(result.data?.items) ? result.data.items : [];
}

async function loadTmdbSeasons(target) {
  const revision = ++tmdbSeasonRevision;
  availableSeasons.value = [];
  seasons.value = [];
  if (target?.media_type !== "tv" || !target?.tmdb_id) return;
  const applySeasons = (values) => {
    availableSeasons.value = normalizeSeasons(values);
    const parsed = normalizeSeasons(parsedSeasons.value);
    seasons.value = parsed.filter((season) => availableSeasons.value.includes(season));
  }
  if (Array.isArray(target.seasons) && target.seasons.length) {
    applySeasons(target.seasons);
    return;
  }
  loadingTmdbSeasons.value = true;
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
        tmdb_id: target.tmdb_id,
        title: target.title,
        original_title: target.original_title,
        year: target.year,
        media_type: "tv",
      }),
    )
    if (revision !== tmdbSeasonRevision) return;
    if (result.success === false) throw new Error(result.message || "TMDB 季列表查询失败");
    applySeasons(result.data?.seasons || result.data?.items?.[0]?.seasons || []);
  } catch (e) {
    if (revision === tmdbSeasonRevision) error.value = e.message || "TMDB 季列表查询失败";
  } finally {
    if (revision === tmdbSeasonRevision) loadingTmdbSeasons.value = false;
  }
}

async function resolveCloudPathMedia(path, revision) {
  const hint = parseCloudPathMedia(path);
  if (!hint) return;
  subscribeId.value = null;
  targetSearch.value = hint.title;
  selectedMedia.value = null;
  selectedTarget.value = null;
  parsedSeasons.value = hint.season ? [hint.season] : [];
  searchingTmdb.value = true;
  error.value = "";
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/sync/manual/resolve`, {
        cloud_path: path,
        cloud_provider: selectedCloudResource.value?.provider || null,
        title: hint.title,
        tmdb_id: hint.tmdbId,
        media_type: hint.preferTv ? "tv" : null,
      }),
    )
    if (revision !== cloudMediaResolveRevision) return;
    if (result.success === false) throw new Error(result.message || "网盘路径媒体识别失败");
    const data = result.data || {};
    targetSearch.value = String(data.title || hint.title).trim();
    parsedSeasons.value = normalizeSeasons(data.seasons || []);
    tmdbCandidates.value = Array.isArray(data.candidates) ? data.candidates : [];
    const matchedId = Number(data.subscribe_id || 0);
    const matchedSubscribe = matchedId > 0 ? subscribes.value.find((item) => Number(item.value) === matchedId) : null;
    if (matchedSubscribe) {
      selectedTarget.value = {
        ...matchedSubscribe,
        seasons: normalizeSeasons(data.available_seasons || []),
        target_kind: "subscribe",
        target_key: `subscribe:${matchedSubscribe.value}`,
      }
      return;
    }
    const exactMatches = tmdbCandidates.value.filter((item) => Number(item?.tmdb_id || 0) === hint.tmdbId);
    const matchedMedia =
      (hint.preferTv && exactMatches.find((item) => item.media_type === "tv")) || exactMatches[0] || null
    if (!matchedMedia) {
      throw new Error(`已识别 TMDB ID ${hint.tmdbId}，但未查询到对应媒体，请手动选择`);
    }
    applyRecognizedMedia(matchedMedia, parsedSeasons.value);
  } catch (e) {
    if (revision === cloudMediaResolveRevision) error.value = e.message || "网盘路径媒体识别失败";
  } finally {
    if (revision === cloudMediaResolveRevision) searchingTmdb.value = false;
  }
}

function selectCloudPath(path, provider) {
  if (resourceResolveTimer) window.clearTimeout(resourceResolveTimer);
  resourceResolveRevision += 1;
  selectedCloudResource.value = {
    provider: String(provider || targetCloudDrive.value || "").trim(),
    path: String(path || "/").trim() || "/",
  }
  resourceLinks.value = cloudResourceLabel(selectedCloudResource.value);
  cloudDirectoryVisible.value = false;
  cloudMediaResolveRevision += 1;
  searchingTmdb.value = false;
  void resolveCloudPathMedia(selectedCloudResource.value.path, cloudMediaResolveRevision);
}

function clearCloudResource() {
  cloudMediaResolveRevision += 1;
  searchingTmdb.value = false;
  selectedCloudResource.value = null;
  resourceLinks.value = "";
}

function matchInitialSubscribe() {
  subscribeId.value = null
  selectedTarget.value = null;
  if (!props.initialMedia) return
  const media = props.initialMedia
  const targetTmdbId = Number(media.tmdb_id || 0)
  const targetSeason = Number(media.season || 0)
  const targetType = String(media.media_type || "")
  const targetTitle = normalizeSearchText(media.title)
  const targetYear = String(media.year || "").trim()
  const matches = subscribes.value.filter((item) => {
    const itemType = String(item.media_type || "")
    if (targetType && itemType && itemType !== targetType) return false
    if (targetTmdbId && Number(item.tmdb_id || 0) === targetTmdbId) return true
    return (
      normalizeSearchText(item.name) === targetTitle && (!targetYear || !item.year || String(item.year) === targetYear)
    )
  })
  const seasonMatch = targetSeason > 0 ? matches.find((item) => Number(item.season || 0) === targetSeason) : null
  subscribeId.value = (seasonMatch || matches[0])?.value || null
  if (subscribeId.value) {
    const matched = matches.find((item) => item.value === subscribeId.value);
    selectedTarget.value = {
      ...matched,
      target_kind: "subscribe",
      target_key: `subscribe:${matched.value}`,
    }
    selectedMedia.value = matched;
  } else {
    const media = {
      ...media,
      media_type_name: media.media_type === "movie" ? "电影" : "电视剧",
    }
    selectedTarget.value = {
      ...media,
      target_kind: "tmdb",
      target_key: `tmdb:${media.media_type}:${media.tmdb_id}`,
    }
    selectedMedia.value = selectedTarget.value;
    seasons.value = media.season ? [Number(media.season)] : [];
    parsedSeasons.value = [...seasons.value];
  }
}

async function resolveInitialMediaFallback() {
  if (!lockedInitialMedia.value || subscribeId.value || selectedMedia.value?.tmdb_id || !props.initialMedia?.title)
    return
  const media = props.initialMedia
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
        title: media.title,
      }),
    )
    if (result.success === false) throw new Error(result.message || "TMDB 匹配失败")
    const candidates = Array.isArray(result.data?.items) ? result.data.items : []
    const targetTitle = normalizeSearchText(media.title)
    const targetYear = String(media.year || "").trim()
    const targetType = String(media.media_type || "")
    const matched =
      candidates.find(
        (item) =>
          (!targetType || item.media_type === targetType) &&
          (!targetYear || !item.year || String(item.year) === targetYear) &&
          normalizeSearchText(item.title) === targetTitle,
      ) ||
      candidates.find(
        (item) =>
          (!targetType || item.media_type === targetType) &&
          (!targetYear || !item.year || String(item.year) === targetYear),
      )
    if (!matched?.tmdb_id) throw new Error("未找到对应的 TMDB 媒体")
    const resolved = {
      ...matched,
      ...media,
      tmdb_id: matched.tmdb_id,
      media_type: media.media_type || matched.media_type,
    }
    applyRecognizedMedia(resolved, media.season ? [media.season] : []);
  } catch (e) {
    error.value = e.message || "历史媒体自动匹配失败"
  }
}

async function loadOptions() {
  loadingOptions.value = true
  error.value = ""
  try {
    const result = unwrap(await props.api.get(`plugin/${props.pluginId}/ui_options?scope=subscriptions`))
    if (result.success === false) throw new Error(result.message || "加载订阅失败")
    const data = result.data?.data || result.data || result
    subscribes.value = Array.isArray(data.subscribes) ? data.subscribes : []
    cloudDrives.value = Array.isArray(data.cloud_drives) ? data.cloud_drives : [];
    targetCloudDrive.value = String(data.target_cloud_drive || "");
    enableCloudUpgrade.value = Boolean(data.enable_cloud_upgrade);
    crossTransferMediaTypes.value = Array.isArray(data.cross_transfer_media_types)
      ? data.cross_transfer_media_types.map((value) => String(value))
      : []
  } catch (e) {
    error.value = e.message || "加载订阅失败"
  } finally {
    loadingOptions.value = false
  }
}

async function loadMediaServers() {
  if (loadingMediaServers.value || mediaServers.value.length) return
  loadingMediaServers.value = true
  error.value = ""
  try {
    const result = unwrap(await props.api.get(`plugin/${props.pluginId}/media/servers`))
    if (result.success === false) throw new Error(result.message || "加载媒体服务器失败")
    mediaServers.value = result.data?.servers || []
    if (mediaServers.value.length === 1) mediaServer.value = mediaServers.value[0].value
  } catch (e) {
    error.value = e.message || "加载媒体服务器失败"
  } finally {
    loadingMediaServers.value = false
  }
}

async function searchMediaContents(tmdb = null) {
  if (!mediaServer.value || (!tmdb?.tmdb_id && !mediaKeyword.value.trim()) || loadingMedia.value) return
  const selectedServer = mediaServer.value
  loadingMedia.value = true
  error.value = ""
  try {
    const params = { server: mediaServer.value }
    if (tmdb?.tmdb_id) {
      params.tmdb_id = String(tmdb.tmdb_id)
      params.media_type = tmdb.media_type || ""
    } else {
      params.keyword = mediaKeyword.value.trim()
    }
    const query = new URLSearchParams(params)
    const result = unwrap(await props.api.get(`plugin/${props.pluginId}/media/content?${query}`))
    if (mediaServer.value !== selectedServer) return
    if (result.success === false) throw new Error(result.message || "搜索媒体库失败")
    mediaServers.value = result.data?.servers || mediaServers.value
    mediaContents.value = result.data?.items || []
    selectedMediaItems.value = []
  } catch (e) {
    error.value = e.message || "搜索媒体库失败"
  } finally {
    loadingMedia.value = false
  }
}

async function matchUpgradeTmdb() {
  if (!mediaServer.value || !mediaKeyword.value.trim() || matchingUpgradeTmdb.value) return
  const selectedServer = mediaServer.value
  matchingUpgradeTmdb.value = true
  error.value = ""
  try {
    const result = unwrap(
      await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
        title: mediaKeyword.value.trim(),
      }),
    )
    if (mediaServer.value !== selectedServer) return
    if (result.success === false) throw new Error(result.message || "TMDB 匹配失败")
    upgradeTmdbCandidates.value = result.data?.items || []
    selectedUpgradeTmdb.value = upgradeTmdbCandidates.value.length === 1 ? upgradeTmdbCandidates.value[0] : null
  } catch (e) {
    error.value = e.message || "TMDB 匹配失败"
  } finally {
    matchingUpgradeTmdb.value = false
  }
}

async function searchTmdb() {
  if (!targetSearch.value.trim() || searchingTmdb.value) return;
  searchingTmdb.value = true
  error.value = ""
  try {
    tmdbCandidates.value = await requestTmdbCandidates(targetSearch.value.trim());
    if (tmdbCandidates.value.length === 1) applyRecognizedMedia(tmdbCandidates.value[0], parsedSeasons.value);
  } catch (e) {
    error.value = e.message || "TMDB 搜索失败"
  } finally {
    searchingTmdb.value = false
  }
}

function close(value) {
  if (submitting.value) return
  confirmVisible.value = false
  emit("update:modelValue", Boolean(value))
}

function requestSubmit() {
  if (!canSubmit.value) return
  if (actionMode.value === "upgrade") {
    confirmVisible.value = true
    return
  }
  submit()
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  error.value = ""
  try {
    let result
    if (actionMode.value === "upgrade") {
      result = unwrap(
        await props.api.post(`plugin/${props.pluginId}/history/upgrade`, {
          source: "media_server",
          items: selectedMediaItems.value.map((item) => ({
            server: item.server,
            item_id: item.item_id,
            kind: item.kind,
            season: item.season,
            episode: item.episode,
          })),
        }),
      )
    } else {
      const target = selectedTarget.value;
      const selectedSeasons = normalizeSeasons(seasons.value);
      const isSingleSubscriptionSeason = Boolean(
        target?.target_kind === "subscribe" &&
        (target.media_type !== "tv" ||
          (selectedSeasons.length === 1 && Number(target.season || 0) === selectedSeasons[0])),
      )
      const media = target
        ? {
          ...target,
          seasons: selectedSeasons,
          }
        : null
      result = unwrap(
        await props.api.post(`plugin/${props.pluginId}/sync/manual`, {
          subscribe_id: isSingleSubscriptionSeason ? target.value : null,
          media: isSingleSubscriptionSeason ? null : media,
          resource_links: selectedCloudResource.value ? [] : resourceLinks.value.split(/\r?\n/),
          cloud_path: selectedCloudResource.value?.path || null,
          cloud_provider: selectedCloudResource.value?.provider || null,
          manual_upgrade: manualUpgrade.value,
          skip_history: skipHistory.value,
        }),
      )
    }
    if (result.success === false) throw new Error(result.message || "提交失败")
    emit("started", result.message || "任务已启动")
    resourceLinks.value = ""
    selectedCloudResource.value = null;
    manualUpgrade.value = false
    skipHistory.value = false;
    selectedMediaItems.value = []
    confirmVisible.value = false
    emit("update:modelValue", false)
  } catch (e) {
    error.value = e.message || "提交失败"
  } finally {
    submitting.value = false
  }
}

watch(actionMode, (value) => {
  error.value = ""
  if (value === "upgrade") loadMediaServers()
})

watch(selectedTarget, (value) => {
  if (!value) {
    subscribeId.value = null;
    selectedMedia.value = null;
    availableSeasons.value = [];
    seasons.value = [];
    return;
  }
  subscribeId.value = value.target_kind === "subscribe" ? value.value : null;
  selectedMedia.value = value;
  void loadTmdbSeasons(value);
})

watch(mediaServer, () => {
  mediaContents.value = []
  selectedMediaItems.value = []
  upgradeTmdbCandidates.value = []
  selectedUpgradeTmdb.value = null
  if (props.modelValue && actionMode.value === "upgrade" && mediaServer.value && lockedInitialMedia.value) {
    searchMediaContents(props.initialMedia?.tmdb_id ? props.initialMedia : null)
  }
})

watch(mediaKeyword, () => {
  upgradeTmdbCandidates.value = []
  selectedUpgradeTmdb.value = null
})

watch(selectedUpgradeTmdb, (value) => {
  if (value?.tmdb_id) searchMediaContents(value)
})

watch(
  () => props.modelValue,
  async (visible) => {
    if (visible) {
      cloudMediaResolveRevision += 1;
      actionMode.value = "links";
      selectedCloudResource.value = null;
      skipHistory.value = false;
      cloudDirectoryVisible.value = false;
      mediaKeyword.value = String(props.initialMedia?.title || "").trim()
      targetSearch.value = "";
      tmdbCandidates.value = []
      selectedTarget.value = null;
      selectedMedia.value = null
      seasons.value = [];
      availableSeasons.value = [];
      parsedSeasons.value = [];
      mediaContents.value = []
      selectedMediaItems.value = []
      upgradeTmdbCandidates.value = []
      selectedUpgradeTmdb.value = null
      confirmVisible.value = false
      targetSearch.value = "";
      subscribeId.value = null
      await loadOptions()
      actionMode.value = props.initialMode === "upgrade" && enableCloudUpgrade.value ? "upgrade" : "links";
      matchInitialSubscribe()
      await resolveInitialMediaFallback()
      if (actionMode.value === "upgrade") {
        await loadMediaServers()
        if (mediaServer.value && lockedInitialMedia.value && !loadingMedia.value) {
          await searchMediaContents(props.initialMedia?.tmdb_id ? props.initialMedia : null)
        }
      }
    } else {
      searchingTmdb.value = false;
      error.value = ""
      confirmVisible.value = false
      cloudDirectoryVisible.value = false;
    }
  },
  { immediate: true },
)
</script>

<style scoped>
.media-selection-card {
  overflow: hidden;
}

.manual-resource-card {
  max-height: min(760px, calc(100dvh - 32px));
}

.manual-search-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.tmdb-target-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
}

.tmdb-target-row.has-season {
  grid-template-columns: minmax(0, 1fr) 72px;
}

.resource-path-actions {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cloud-resource-input :deep(.v-field) {
  background: rgba(var(--v-theme-primary), 0.06);
}

.cloud-resource-input :deep(textarea) {
  color: rgb(var(--v-theme-primary));
  font-weight: 500;
  cursor: default;
}

.media-selection-header {
  display: flex;
  min-height: 42px;
  align-items: center;
  padding: 4px 10px 4px 14px;
}

.media-selection-list {
  max-height: min(320px, 42vh);
  overflow-y: auto;
  overscroll-behavior: contain;
}

.media-selection-state {
  display: flex;
  min-height: 96px;
  align-items: center;
  justify-content: center;
  padding: 16px;
  text-align: center;
}

@media (max-width: 600px) {
  .manual-resource-card {
    width: 100%;
    height: 100dvh;
    max-height: 100dvh;
    border-radius: 0 !important;
  }

  .manual-resource-card > :deep(.v-card-text) {
    min-height: 0;
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 12px;
  }

  .manual-resource-card > :deep(.v-card-title) {
    padding: 12px;
  }

  .manual-resource-card > :deep(.v-card-actions) {
    flex: 0 0 auto;
    padding: 8px 12px 12px !important;
  }

  .manual-resource-card :deep(.v-btn-toggle) {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .manual-resource-card :deep(.v-btn-toggle .v-btn) {
    min-width: 0;
  }

  .manual-search-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .manual-search-row > :deep(.v-text-field) {
    grid-column: 1 / -1;
  }

  .manual-search-row > :deep(.v-btn) {
    width: 100%;
  }

  .tmdb-target-row.has-season {
    grid-template-columns: minmax(0, 1fr) 64px;
  }

  .media-selection-list {
    max-height: min(42vh, 360px);
  }

  .media-selection-list :deep(.v-list-item) {
    padding-inline: 8px;
  }

  .media-selection-list :deep(.v-list-item-subtitle) {
    white-space: normal;
    overflow-wrap: anywhere;
  }
}
</style>
