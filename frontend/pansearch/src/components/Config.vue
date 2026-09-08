<template>
  <div class="cloud-subscribe-config">
    <v-card flat class="border rounded config-shell">
      <v-card-title class="config-header d-flex align-center ga-1 px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-cloud-cog-outline" color="primary" size="small" class="mr-1" />
        <span class="config-title text-subtitle-1">网盘订阅助手</span>
        <v-spacer />
        <v-btn
          v-if="showSwitch"
          class="config-header-action"
          variant="text"
          size="small"
          prepend-icon="mdi-arrow-left"
          title="返回详情"
          @click="emit('switch')">
          返回详情
        </v-btn>
        <v-btn
          class="config-header-action"
          variant="text"
          size="small"
          prepend-icon="mdi-close"
          title="关闭"
          @click="emit('close')">
          关闭
        </v-btn>
      </v-card-title>
      <v-card-text class="pa-0 config-body">
        <v-tabs v-model="activeTab" color="primary" density="compact" show-arrows class="config-tabs border-b">
          <v-tab v-for="section in sections" :key="section.value" :value="section.value">
            <v-icon :icon="section.icon" size="small" class="mr-2" />
            {{ section.title }}
          </v-tab>
        </v-tabs>
        <div class="config-content-scroll">
          <div class="config-window">
            <section
              v-for="section in sections"
              :key="section.value"
              v-show="activeTab === section.value"
              class="config-window-section">
              <ConfigSection
                :section="section"
                :config="config"
                :api="api"
                :refreshing-accounts="refreshingAccounts"
                :testing-source="testingSource"
                :testing-auto-subscribe="testingAutoSubscribe"
                :testing-proxy="testingProxy"
                :testing-auto-subscribe-proxy="testingAutoSubscribeProxy"
                :hdhive-oauth-action="hdhiveOauthAction"
                @scan="openQrCode"
                @browse-directory="openDirectoryPicker"
                @test-source="openSourceTest"
                @test-auto-subscribe="testAutoSubscribe"
                @test-proxy="testSearchProxy"
                @test-auto-subscribe-proxy="testAutoSubscribeProxy"
                @refresh-account="refreshAccount"
                @hdhive-oauth-start="startHdhiveOAuth"
                @hdhive-oauth-exchange="exchangeHdhiveOAuth"
                @checkin-result="handleCheckinResult"
                @copy-text="copyText" />
            </section>
          </div>
        </div>
      </v-card-text>
      <v-divider />
      <v-card-actions class="config-actions px-4 py-3">
        <v-progress-linear v-if="saving" class="save-progress" color="primary" indeterminate />
        <v-slide-y-transition>
          <div v-if="saving" class="save-state" aria-live="polite">
            <v-progress-circular indeterminate size="16" width="2" color="primary" />
            正在保存配置
          </div>
        </v-slide-y-transition>
        <v-spacer />
        <v-btn
          color="primary"
          class="save-config-button"
          variant="flat"
          elevation="2"
          prepend-icon="mdi-content-save-check-outline"
          :loading="saving"
          @click="save">
          保存配置
        </v-btn>
      </v-card-actions>
    </v-card>
    <QrCodeDialog v-show="qrVisible" v-model="qrVisible" :api="api" :provider="qrProvider" @success="handleQrSuccess" />
    <CloudDirectoryDialog
      v-show="directoryVisible"
      v-model="directoryVisible"
      :api="api"
      :provider="directoryProvider"
      :initial-path="directoryInitialPath"
      @select="selectDirectory" />
    <v-dialog v-model="sourceTestVisible" max-width="640" class="source-test-dialog">
      <v-card class="source-test-card" :class="{ 'source-test-card--results': tmdbSearched || testSubmitted }">
        <v-card-title class="source-test-header d-flex align-center ga-2">
          <v-icon icon="mdi-flask-outline" color="primary" />
          {{ sourceNames[sourceTest.source] || "搜索渠道" }}测试
          <v-spacer />
          <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="sourceTestVisible = false" />
        </v-card-title>
        <v-form class="source-test-form" @submit.prevent="searchTmdbCandidates">
          <v-card-text class="source-test-body">
            <v-row dense class="source-test-fields">
              <v-col cols="12" sm="8">
                <v-text-field
                  v-model="sourceTest.title"
                  label="媒体名称"
                  maxlength="100"
                  autofocus
                  clearable
                  hide-details />
              </v-col>
              <v-col cols="12" sm="4">
                <v-text-field
                  v-model="sourceTest.season"
                  label="电视剧季号"
                  type="number"
                  min="1"
                  max="999"
                  hide-details />
              </v-col>
            </v-row>
            <v-alert v-if="testError" type="error" variant="tonal" density="compact" class="source-test-error mt-3">
              {{ testError }}
              <span v-if="testElapsed != null">（耗时 {{ formatElapsed(testElapsed) }}）</span>
            </v-alert>
            <div v-if="tmdbSearched && !testSubmitted" class="source-test-tmdb mt-3">
              <div class="text-caption text-medium-emphasis mb-2">
                选择平台媒体条目后将一次读取完整媒体 ID，并立即测试
                {{ sourceNames[sourceTest.source] }}
              </div>
              <div v-if="testingSource" class="source-test-loading">
                <v-progress-circular indeterminate color="primary" size="42" width="4" />
                <div class="text-body-2 mt-3">正在读取 {{ sourceNames[sourceTest.source] }} 资源</div>
                <div class="text-caption text-medium-emphasis mt-1">仅执行只读搜索，不会解锁、转存或处理文件</div>
              </div>
              <div v-else class="source-test-tmdb-scroll">
                <v-list v-if="tmdbCandidates.length" density="compact" lines="two">
                  <v-list-item
                    v-for="item in tmdbCandidates"
                    :key="`${item.media_type}-${item.tmdb_id}`"
                    :title="item.title"
                    :subtitle="tmdbCandidateSubtitle(item)"
                    :disabled="Boolean(testingSource)"
                    rounded="lg"
                    class="source-test-tmdb-item mb-1"
                    @click="testSource(item)">
                    <template #prepend>
                      <v-avatar rounded="lg" size="48" color="surface-variant">
                        <v-img v-if="item.poster" :src="item.poster" cover />
                        <v-icon
                          v-else
                          :icon="item.media_type === 'movie' ? 'mdi-movie-open-outline' : 'mdi-television-classic'" />
                      </v-avatar>
                    </template>
                    <template #append>
                      <v-progress-circular
                        v-if="selectedTmdbId === item.tmdb_id"
                        indeterminate
                        color="primary"
                        size="20"
                        width="2" />
                      <v-chip v-else size="x-small" variant="tonal" color="primary">TMDB {{ item.tmdb_id }}</v-chip>
                    </template>
                  </v-list-item>
                </v-list>
                <v-alert v-else type="info" variant="tonal" density="compact">平台未找到匹配媒体条目</v-alert>
              </div>
            </div>
            <div v-if="testSubmitted" class="source-test-result">
              <div class="source-test-summary">
                <div class="source-test-summary__line">
                  <span>
                    本次获取
                    <strong>{{ testResult.count || 0 }}</strong>
                    个搜索结果
                  </span>
                  <span>
                    当前展示
                    <strong>{{ testResult.displayed_count ?? (testResult.items || []).length }}</strong>
                    个
                  </span>
                  <span v-if="testResult.elapsed_seconds != null">
                    耗时
                    <strong>{{ formatElapsed(testResult.elapsed_seconds) }}</strong>
                  </span>
                </div>
              </div>
              <div class="source-test-notice text-medium-emphasis mb-3">
                搜索测试固定最多展示
                {{ testResult.display_limit }}
                个候选，不受正式搜索候选上限配置影响
              </div>
              <v-tabs
                v-if="testResourceTypes.length > 1"
                v-model="selectedTestResourceType"
                color="primary"
                density="compact"
                show-arrows
                class="source-test-tabs source-test-resource-tabs">
                <v-tab
                  v-for="resourceType in testResourceTypes"
                  :key="resourceType.value"
                  :value="resourceType.value">
                  {{ resourceType.title }}
                  <v-badge inline :content="resourceType.count" color="primary" class="ml-2" />
                </v-tab>
              </v-tabs>
              <div class="source-test-result-scroll">
                <v-list v-if="filteredTestItems.length" density="compact" class="source-test-result-list">
                  <v-list-item
                    v-for="(item, index) in filteredTestItems"
                    :key="`${item.title}-${index}`"
                    class="source-test-result-item">
                    <div class="source-test-item-content">
                      <div class="source-test-item-title">{{ item.title }}</div>
                      <div class="source-test-item-meta">
                        <v-chip
                          size="x-small"
                          variant="tonal"
                          color="primary"
                          :href="item.source_url || undefined"
                          :target="item.source_url ? '_blank' : undefined"
                          :rel="item.source_url ? 'noopener noreferrer' : undefined"
                          :title="item.source_url ? '打开来源资源页' : undefined">
                          {{ item.source_name || testResult.source_name || "未知来源" }}
                          <v-icon v-if="item.source_url" icon="mdi-open-in-new" size="12" class="ml-1" />
                        </v-chip>
                        <v-chip size="x-small" variant="tonal">
                          {{ item.resource_type_name || item.resource_type || "未知类型" }}
                        </v-chip>
                        <v-chip
                          v-if="testItemStatus(item)"
                          size="x-small"
                          variant="tonal"
                          :color="testItemStatus(item).color">
                          {{ testItemStatus(item).label }}
                        </v-chip>
                        <span class="text-medium-emphasis">
                          {{ item.size || "大小未知" }}
                        </span>
                        <v-chip
                          v-for="tag in item.tags || []"
                          :key="`${item.title}-${tag}`"
                          size="x-small"
                          variant="outlined">
                          {{ tag }}
                        </v-chip>
                        <div class="source-test-item-actions">
                          <v-btn
                            v-if="canPreviewResource(item)"
                            icon="mdi-eye-outline"
                            size="x-small"
                            variant="text"
                            :title="'预览资源内容'"
                            :loading="previewingUrl === previewResourceKey(item)"
                            :disabled="Boolean(previewingUrl)"
                            @click="previewResource(item)" />
                          <v-btn
                            v-if="item.url"
                            icon="mdi-content-copy"
                            size="x-small"
                            variant="text"
                            title="复制资源链接"
                            @click="copyText(item.url, '资源链接')" />
                          <v-btn
                            v-if="item.need_unlock"
                            icon="mdi-lock-open-outline"
                            size="x-small"
                            variant="text"
                            color="warning"
                            :title="Number(item.unlock_points || 0) > 0 ? `确认消耗 ${Number(item.unlock_points || 0)} 积分解锁` : '获取免费资源链接'"
                            @click="confirmUnlock(item)" />
                        </div>
                      </div>
                    </div>
                  </v-list-item>
                </v-list>
                <v-alert v-else type="info" variant="tonal" density="compact">当前渠道未找到候选资源</v-alert>
              </div>
            </div>
          </v-card-text>
          <v-card-actions class="source-test-actions">
            <v-spacer />
            <v-btn
              type="submit"
              color="primary"
              variant="flat"
              prepend-icon="mdi-magnify"
              :loading="searchingTmdb"
              :disabled="Boolean(testingSource) || searchingTmdb || !String(sourceTest.title || '').trim()">
              搜索资源
            </v-btn>
          </v-card-actions>
        </v-form>
      </v-card>
    </v-dialog>
    <v-dialog v-model="autoSubscribeTestVisible" max-width="560" class="auto-subscribe-test-dialog">
      <v-card class="auto-subscribe-test-card">
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-flask-outline" color="primary" />
          <span>{{ autoSubscribeProviderNames[autoSubscribeTestProvider] || "榜单" }}测试</span>
          <v-spacer />
          <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="autoSubscribeTestVisible = false" />
        </v-card-title>
        <v-card-text class="auto-subscribe-test-body">
          <v-alert v-if="autoSubscribeTestError" type="error" variant="tonal" density="compact" class="mb-3">
            {{ autoSubscribeTestError }}
          </v-alert>
          <div v-if="autoSubscribeTestLoading" class="auto-subscribe-test-loading">
            <v-progress-circular indeterminate color="primary" size="36" />
            <span class="text-body-2">正在抓取榜单示例</span>
          </div>
          <template v-else-if="autoSubscribeTestResult">
            <div class="text-body-2 mb-2">{{ autoSubscribeTestMessage }}</div>
            <v-list v-if="autoSubscribeTestItems.length" density="compact" lines="two" class="auto-subscribe-test-list">
              <v-list-item v-for="(item, index) in autoSubscribeTestItems" :key="`${item.title}-${index}`">
                <template #prepend>
                  <v-avatar size="30" color="primary" variant="tonal">{{ index + 1 }}</v-avatar>
                </template>
                <v-list-item-title>{{ item.title }}</v-list-item-title>
                <v-list-item-subtitle>
                  <span v-if="item.year">{{ item.year }}</span>
                  <span v-if="item.media_type">· {{ item.media_type === "tv" ? "电视剧" : "电影" }}</span>
                  <span v-if="item.season != null">· 第 {{ item.season }} 季</span>
                </v-list-item-subtitle>
              </v-list-item>
            </v-list>
            <v-alert v-else type="info" variant="tonal" density="compact">榜单已连通，但没有示例数据。</v-alert>
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="autoSubscribeTestVisible = false">关闭</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
    <v-dialog v-model="previewVisible" max-width="760" scrollable class="resource-preview-dialog">
      <v-card class="source-preview-card">
        <v-card-title class="source-preview-header d-flex align-center justify-space-between py-3 px-4">
          <div class="d-flex align-center ga-2 min-w-0">
            <v-icon icon="mdi-folder-open-outline" color="primary" />
            <div class="min-w-0">
              <div class="text-subtitle-1 font-weight-bold">资源内容预览</div>
              <div
                class="text-caption text-medium-emphasis text-truncate"
                :title="previewMeta.display_name || previewMeta.share_url">
                {{ previewMeta.display_name || previewMeta.share_url || "文件树解析" }}
              </div>
            </div>
          </div>
          <div class="d-flex align-center ga-1">
            <v-btn
              v-if="previewMeta.share_url"
              icon="mdi-content-copy"
              size="small"
              variant="text"
              title="复制网盘链接"
              @click="copyText(previewMeta.share_url, '网盘链接')" />
            <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="previewVisible = false" />
          </div>
        </v-card-title>
        <v-card-text class="source-preview-body">
          <div
            v-if="
              previewMeta.display_name ||
              previewMeta.info_hash ||
              previewMeta.size ||
              previewMeta.provider_name ||
              previewMeta.share_url
            "
            class="source-preview-meta text-caption text-medium-emphasis mb-3">
            <span v-if="previewMeta.provider_name">
              网盘
              <strong>{{ previewMeta.provider_name }}</strong>
            </span>
            <span v-if="previewMeta.resource_type_name">
              类型
              <strong>{{ previewMeta.resource_type_name }}</strong>
            </span>
            <span v-if="previewMeta.size">
              总大小
              <strong>{{ formatPreviewSize(previewMeta.size) }}</strong>
            </span>
            <span>
              当前层
              <strong class="text-primary">{{ previewItems.length }}</strong>
              项
            </span>
          </div>
          <div v-if="previewBreadcrumbs.length > 1" class="source-preview-breadcrumbs mb-3">
            <template v-for="(breadcrumb, index) in previewBreadcrumbs" :key="`${breadcrumb.id}-${index}`">
              <v-icon v-if="index" icon="mdi-chevron-right" size="small" />
              <v-btn
                size="small"
                variant="text"
                :disabled="previewLoading || index === previewBreadcrumbs.length - 1"
                @click="openPreviewBreadcrumb(index)">
                {{ breadcrumb.name }}
              </v-btn>
            </template>
          </div>
          <div v-if="previewLoading" class="source-preview-loading">
            <v-progress-circular indeterminate color="primary" size="44" width="4" />
          </div>
          <v-alert v-if="previewError" type="error" variant="tonal" density="compact" class="my-3">
            {{ previewError }}
          </v-alert>
          <div v-else-if="!previewLoading && previewItems.length" class="source-preview-list-scroll">
            <v-list density="compact" lines="one" class="source-preview-list">
              <template v-for="(file, index) in previewItems" :key="`${file.name}-${index}`">
                <v-list-item
                  class="source-preview-file"
                  :class="{ 'source-preview-file--directory': file.can_enter }"
                  @click="file.can_enter && openPreviewFolder(file)">
                  <template #prepend>
                    <v-icon :icon="previewFileIcon(file)" />
                  </template>
                  <v-list-item-title class="source-preview-file-name" :title="file.name">
                    <span v-if="file.is_dir" class="source-preview-file-stem">{{ file.name }}</span>
                    <template v-else>
                      <span class="source-preview-file-stem">{{ previewFileStem(file.name) }}</span>
                      <span class="source-preview-file-extension">{{ previewFileExtension(file.name) }}</span>
                    </template>
                  </v-list-item-title>
                  <template #append>
                    <span
                      v-if="formatPreviewSize(file.size)"
                      class="source-preview-file-size text-caption text-medium-emphasis">
                      {{ formatPreviewSize(file.size) }}
                    </span>
                    <v-icon v-if="file.can_enter" icon="mdi-chevron-right" size="small" class="ml-2" />
                  </template>
                </v-list-item>
                <v-divider v-if="index < previewItems.length - 1" />
              </template>
            </v-list>
          </div>
          <v-alert v-else-if="!previewLoading && !previewError" type="info" variant="tonal" density="compact">
            当前目录为空
          </v-alert>
        </v-card-text>
      </v-card>
    </v-dialog>
    <v-dialog v-model="unlockVisible" max-width="440" :persistent="unlocking">
      <v-card>
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-lock-open-outline" color="warning" />
          解锁资源
        </v-card-title>
        <v-card-text>
          <p class="mb-3">
            <template v-if="Number(unlockItem?.unlock_points || 0) > 0">
              确认消耗
              {{ Number(unlockItem?.unlock_points || 0) }} 积分解锁此资源？
            </template>
            <template v-else>此资源免费，确认获取资源链接？</template>
          </p>
          <div class="text-body-2 text-medium-emphasis text-truncate" :title="unlockItem?.title || ''">
            {{ unlockItem?.title || "未命名资源" }}
          </div>
          <v-alert v-if="unlockError" type="error" variant="tonal" density="compact" class="mt-4">
            {{ unlockError }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="unlocking" @click="unlockVisible = false">取消</v-btn>
          <v-btn
            color="warning"
            variant="flat"
            prepend-icon="mdi-lock-open-outline"
            :loading="unlocking"
            @click="unlockResource">
            确认解锁
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
    <v-snackbar v-model="messageVisible" :color="messageType" location="top" timeout="3500" variant="elevated">
      {{ message }}
      <template #actions>
        <v-btn icon="mdi-close" size="small" variant="text" @click="messageVisible = false" />
      </template>
    </v-snackbar>
  </div>
</template>

<script setup>
import {computed, defineAsyncComponent, onBeforeUnmount, onMounted, reactive, ref, watch} from "vue";
import ConfigSection from "./config/ConfigSection.vue";
import {createConfigSections} from "../config/fields.js";
import {createResourceTypeItems} from "../config/fields/helpers.js";

const QrCodeDialog = defineAsyncComponent(() => import("./dialogs/QrCodeDialog.vue"))
const CloudDirectoryDialog = defineAsyncComponent(() => import("./dialogs/CloudDirectoryDialog.vue"))
const props = defineProps({
  api: { type: [Object, Function], required: true },
  initialConfig: { type: Object, default: () => ({}) },
  showSwitch: { type: Boolean, default: true },
})
const emit = defineEmits(["save", "close", "switch", "layout"])
const api = props.api
const config = reactive(JSON.parse(JSON.stringify(props.initialConfig || {})))

function normalizeAutoSubscribeYears(target) {
  const currentYear = new Date().getFullYear();
  if (!String(target.auto_subscribe_username || "").trim()) target.auto_subscribe_username = "网盘订阅助手"
  ;
  [
    "auto_subscribe_douban_min_year",
    "auto_subscribe_maoyan_min_year",
    "auto_subscribe_netflix_min_year",
    "auto_subscribe_mikan_year",
    "auto_subscribe_mikan_min_year",
  ].forEach((key) => {
    const value = Number(target[key]);
    if (!Number.isFinite(value) || value === 0) target[key] = currentYear;
  })
  if (typeof target.auto_subscribe_douban_rss_urls === "string") {
    target.auto_subscribe_douban_rss_urls = target.auto_subscribe_douban_rss_urls
      .split(/[\n,，]+/)
      .map((value) => value.trim())
      .filter(Boolean)
  }
  if (!Array.isArray(target.auto_subscribe_mikan_base_urls)) {
    const value = String(target.auto_subscribe_mikan_base_urls || "").trim();
    target.auto_subscribe_mikan_base_urls = value ? [value] : ["https://mikanani.me", "https://mikanime.tv"];
  }
}

normalizeAutoSubscribeYears(config);
if (!Array.isArray(config.online_docs) || !config.online_docs.length) {
  const legacyUrls = Array.isArray(config.online_docs_urls)
    ? config.online_docs_urls
    : String(config.online_docs_urls || "").split(/[,，\n]+/)
  const legacyTypes = Array.isArray(config.online_docs_resource_types) ? config.online_docs_resource_types : []
  config.online_docs = legacyUrls
    .map((url) => String(url || "").trim())
    .filter(Boolean)
    .map((url) => ({ url, resource_types: [...legacyTypes] }))
}
if (!config.online_docs.length) {
  config.online_docs.push({ url: "", resource_types: [] })
}
config.online_docs_urls = []
config.online_docs_resource_types = []
const activeTab = ref("basic")
const optionScopeByTab = Object.freeze({
  basic: "base",
  transfer: "subscriptions",
  upgrade: "subscriptions",
  drive: "drive",
  search: "search",
  checkin: "base",
  notify: "notify",
})
const loadedOptionScopes = new Set()
const optionScopeRequests = new Map()
const qrVisible = ref(false),
  qrProvider = ref("115"),
  directoryVisible = ref(false),
  directoryField = ref(""),
  directoryInitialPath = ref("/"),
  directoryProvider = ref("115"),
  saving = ref(false),
  refreshingAccounts = ref([]),
  testingSource = ref(""),
  testingAutoSubscribe = ref(""),
  autoSubscribeTestVisible = ref(false),
  autoSubscribeTestProvider = ref(""),
  autoSubscribeTestLoading = ref(false),
  autoSubscribeTestResult = ref(null),
  autoSubscribeTestError = ref(""),
  testingProxy = ref(false),
  testingAutoSubscribeProxy = ref(false),
  searchingTmdb = ref(false),
  tmdbSearched = ref(false),
  tmdbCandidates = ref([]),
  selectedTmdbId = ref(0),
  testResult = ref({}),
  selectedTestResourceType = ref(""),
  sourceTestVisible = ref(false),
  testSubmitted = ref(false),
  testError = ref(""),
  testElapsed = ref(null),
  previewVisible = ref(false),
  previewingUrl = ref(""),
  previewLoading = ref(false),
  previewError = ref(""),
  previewItems = ref([]),
  previewMeta = ref({}),
  previewBreadcrumbs = ref([]),
  previewResourceType = ref(""),
  previewShareUrl = ref(""),
  previewSource = ref(""),
  previewJuyingResourceId = ref(""),
  previewProviderData = ref({}),
  previewPendingResource = ref({}),
  previewTargetSeason = ref(null),
  previewTargetEpisodes = ref([]),
  unlockVisible = ref(false),
  unlockItem = ref(null),
  unlocking = ref(false),
  unlockError = ref(""),
  message = ref(""),
  messageType = ref("success"),
  messageVisible = ref(false)
let previewRequestId = 0
const options = reactive({
  subscribes: [],
  mediaservers: [],
  mediaLibraryWebhookUrls: {},
  notificationTypes: [],
  cloudDrives: [],
  account: {},
  accounts: {},
  searchAccounts: {},
  pansou: {},
  rsshubInstances: [],
  rsshubLoading: false,
})
const sections = computed(() => createConfigSections(options, config))
const testResourceTypes = computed(() => {
  const declared = Array.isArray(testResult.value?.resource_types)
    ? testResult.value.resource_types
    : [];
  if (declared.length) return declared;
  const items = Array.isArray(testResult.value?.items) ? testResult.value.items : [];
  const counts = new Map();
  items.forEach((item) => {
    const value = String(item?.resource_type || "unknown").toLowerCase();
    const title = item?.resource_type_name || value;
    const current = counts.get(value) || {value, title, count: 0};
    current.count += 1;
    counts.set(value, current);
  });
  return [...counts.values()];
});
watch(testResourceTypes, (types) => {
  if (!types.some((item) => item.value === selectedTestResourceType.value)) {
    selectedTestResourceType.value = types[0]?.value || "";
  }
}, {immediate: true});
const filteredTestItems = computed(() => {
  const items = Array.isArray(testResult.value?.items) ? testResult.value.items : []
  let allItems = items;
  const merged = testResult.value?.merged_by_type;
  if (!allItems.length && merged && typeof merged === "object") {
    const resourceTypeNames = {
      "115": "115网盘",
      "123": "123云盘",
      quark: "夸克网盘",
      aliyun: "阿里云盘",
      alipan: "阿里云盘",
      guangya: "光鸭云盘",
      tianyi: "天翼云盘",
      magnet: "磁力链接",
      ed2k: "电驴链接",
    };
    allItems = Object.entries(merged).flatMap(([resourceType, rows]) => {
      if (!Array.isArray(rows)) return [];
      return rows.map((row) => {
        const value = row && typeof row === "object" ? row : {};
        const title = String(value.title || value.note || "未命名资源");
        return {
          ...value,
          title,
          url: value.url || value.share_url || "",
          resource_type: value.resource_type || resourceType,
          resource_type_name: value.resource_type_name || resourceTypeNames[resourceType] || resourceType,
          size: value.size || value.size_human || 0,
          tags: Array.isArray(value.tags) ? value.tags : [],
        };
      });
    });
  }
  if (!selectedTestResourceType.value) return allItems;
  return allItems.filter((item) => String(item?.resource_type || "unknown").toLowerCase() === selectedTestResourceType.value);
})
const sourceNames = {
  hdhive: "HDHive",
  pansou: "PanSou",
  dian115: "Dian115",
  juying: "聚影",
  seedhub: "SeedHub",
  pinglian: "盘链",
  online_docs: "在线文档",
  piratebay: "海盗湾",
  uindex: "UIndex",
}
const autoSubscribeProviderNames = {
  douban: "豆瓣榜单",
  maoyan: "猫眼榜单",
  netflix: "Netflix 榜单",
  mikan: "Mikan 新番",
  tmdb: "TMDB 榜单",
  bangumi: "Bangumi 榜单",
  anilist: "AniList 榜单",
}
const autoSubscribeTestItems = computed(() => {
  const items = autoSubscribeTestResult.value?.data?.items || autoSubscribeTestResult.value?.items || [];
  return Array.isArray(items) ? items : [];
})
const autoSubscribeTestMessage = computed(() => {
  return autoSubscribeTestResult.value?.message || "测试完成";
})
const sourceTestConfigKeys = {
  hdhive: [
    "hdhive_base_url",
    "hdhive_query_mode",
    "hdhive_api_key",
    "hdhive_client_id",
    "hdhive_redirect_uri",
    "hdhive_response_mode",
    "hdhive_auth_code",
    "hdhive_access_token",
    "hdhive_refresh_token",
    "hdhive_token_expires_at",
    "hdhive_username",
    "hdhive_password",
    "hdhive_candidate_limit",
    "hdhive_request_interval",
    "hdhive_unlocks_per_minute",
    "hdhive_torrentclaw_enabled",
    "hdhive_torrentclaw_subtitle_languages",
  ],
  pansou: [
    "pansou_url",
    "pansou_username",
    "pansou_password",
    "pansou_auth_enabled",
    "pansou_channels",
    "pansou_plugins",
    "pansou_filter_include",
    "pansou_filter_exclude",
    "pansou_concurrency",
    "pansou_result_limit",
    "pansou_timeout",
  ],
  dian115: [
    "dian115_email",
    "dian115_password",
    "dian115_candidate_limit",
    "dian115_request_interval",
    "dian115_unlocks_per_minute",
  ],
  juying: ["juying_username", "juying_password", "juying_result_limit", "juying_request_interval"],
  seedhub: ["seedhub_result_limit", "seedhub_request_interval", "seedhub_timeout"],
  pinglian: [
    "pinglian_username",
    "pinglian_password",
    "pinglian_result_limit",
    "pinglian_request_interval",
    "pinglian_timeout",
  ],
  online_docs: ["online_docs"],
  piratebay: ["piratebay_base_url", "piratebay_result_limit", "piratebay_request_interval", "piratebay_timeout"],
  uindex: ["uindex_base_url", "uindex_result_limit", "uindex_request_interval", "uindex_timeout"],
}
const sourceTest = reactive({
  source: "",
  title: "",
  season: 1,
})

function unwrapResponse(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) {
    return raw.data
  }
  return raw || {}
}

function applyOptions(data) {
  Object.entries(data.defaults || {}).forEach(([key, value]) => {
    if (!(key in config)) config[key] = value
  })
  if ("subscribes" in data) {
    options.subscribes = Array.isArray(data.subscribes) ? data.subscribes : []
  }
  if ("mediaservers" in data) {
    options.mediaservers = Array.isArray(data.mediaservers) ? data.mediaservers : []
  }
  if ("media_library_webhook_urls" in data) {
    options.mediaLibraryWebhookUrls =
      data.media_library_webhook_urls && typeof data.media_library_webhook_urls === "object"
        ? data.media_library_webhook_urls
        : {}
  }
  if ("notification_types" in data) {
    options.notificationTypes = Array.isArray(data.notification_types) ? data.notification_types : []
  }
  if ("cloud_drives" in data) {
    options.cloudDrives = Array.isArray(data.cloud_drives) ? data.cloud_drives : []
  }
  if ("account" in data) {
    options.account = data.account && typeof data.account === "object" ? data.account : {}
  }
  if ("accounts" in data) {
    options.accounts = data.accounts && typeof data.accounts === "object" ? data.accounts : {}
  }
  if ("search_accounts" in data) {
    options.searchAccounts =
      data.search_accounts && typeof data.search_accounts === "object" ? data.search_accounts : {}
  }
  if ("pansou" in data) {
    options.pansou = data.pansou && typeof data.pansou === "object" ? data.pansou : {}
  }
  const configuredSources = Array.isArray(config.search_source_order)
    ? config.search_source_order.filter(Boolean)
    : String(config.search_source_order || "")
        .split(/[,，\n]+/)
        .map((value) => value.trim())
        .filter(Boolean)
  config.search_source_order = configuredSources
  ;["pansou_channels", "pansou_plugins", "pansou_filter_include", "pansou_filter_exclude"].forEach((key) => {
    if (Array.isArray(config[key])) return
    config[key] = String(config[key] || "")
      .split(/[,，\n]+/)
      .map((value) => value.trim())
      .filter(Boolean)
  })
  normalizeAutoSubscribeYears(config);
}

function notify(text, type = "success") {
  message.value = text
  messageType.value = type
  messageVisible.value = true
}

async function testSearchProxy() {
  if (testingProxy.value) return
  const proxy = String(config.search_proxy || "").trim()
  if (!proxy) {
    notify("请先填写搜索渠道代理地址", "warning")
    return
  }
  testingProxy.value = true
  try {
    const response = unwrapResponse(
      await api.post("plugin/PanSearch/search/proxy/test", {
        proxy,
        username: String(config.search_proxy_username || "").trim(),
        password: String(config.search_proxy_password || ""),
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "代理测试失败")
    }
    const data = response.data?.data || response.data || {}
    const details = [
      Number.isFinite(Number(data.latency_ms)) ? `${Number(data.latency_ms)} ms` : "",
      data.ip ? `IP ${data.ip}` : "",
      data.loc ? `地区 ${data.loc}` : "",
      data.colo ? `节点 ${data.colo}` : "",
    ].filter(Boolean)
    notify(`代理连接成功${details.length ? `：${details.join(" · ")}` : ""}`)
  } catch (error) {
    notify(error?.response?.data?.message || error.message || String(error), "error")
  } finally {
    testingProxy.value = false
  }
}

async function testAutoSubscribeProxy() {
  if (testingAutoSubscribeProxy.value) return;
  const proxy = String(config.auto_subscribe_proxy || "").trim();
  if (!proxy) {
    notify("请先填写榜单代理地址", "warning");
    return;
  }
  testingAutoSubscribeProxy.value = true;
  try {
    const response = unwrapResponse(
      await api.post("plugin/PanSearch/auto_subscribe/proxy/test", {
        proxy,
        username: String(config.auto_subscribe_proxy_username || "").trim(),
        password: String(config.auto_subscribe_proxy_password || ""),
      }),
    )
    if (response.success === false) throw new Error(response.message || "代理测试失败");
    notify(response.message || "榜单代理连接成功");
  } catch (error) {
    notify(error?.response?.data?.message || error.message || String(error), "error");
  } finally {
    testingAutoSubscribeProxy.value = false;
  }
}

async function copyText(value, label = "Webhook URL") {
  const text = String(value || "")
  if (!text) return
  try {
    let copied = false
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text)
        copied = true
      } catch (_) {
        // 局域网 HTTP 页面可能暴露 API 但拒绝调用，继续使用兼容方式。
      }
    }
    if (!copied) {
      const input = document.createElement("textarea")
      input.value = text
      input.setAttribute("readonly", "")
      input.style.position = "fixed"
      input.style.opacity = "0"
      document.body.appendChild(input)
      input.select()
      const copied = document.execCommand("copy")
      document.body.removeChild(input)
      if (!copied) throw new Error("浏览器拒绝访问剪贴板")
    }
    notify(`${label}已复制`)
  } catch (error) {
    notify(`复制失败：${error.message || error}`, "error")
  }
}

function formatPreviewSize(value) {
  const size = Number(value || 0)
  if (!Number.isFinite(size) || size <= 0) return ""
  const units = ["B", "KB", "MB", "GB", "TB"]
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1)
  return `${(size / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`
}

function formatElapsed(value) {
  const seconds = Number(value)
  return Number.isFinite(seconds) ? `${seconds.toFixed(2)} 秒` : "未知"
}

function previewFileIcon(file) {
  if (file?.is_dir) return "mdi-folder-outline"
  const name = String(file?.name || "").toLowerCase()
  if (/\.(mkv|mp4|avi|ts|m2ts|mov|wmv|webm)$/.test(name)) return "mdi-filmstrip"
  if (/\.(srt|ass|ssa|sup|vtt)$/.test(name)) return "mdi-subtitles-outline"
  if (/\.(zip|rar|7z|tar|gz)$/.test(name)) return "mdi-archive-outline"
  return "mdi-file-outline"
}

function previewFileExtension(value) {
  const name = String(value || "未命名文件")
  const extensionMatch = name.match(/(\.[^./\\\s]{1,12})$/)
  return extensionMatch ? extensionMatch[1] : ""
}

function previewFileStem(value) {
  const name = String(value || "未命名文件")
  const extension = previewFileExtension(name)
  const slash = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"))
  const basename = slash >= 0 ? name.slice(slash + 1) : name
  return extension ? basename.slice(0, -extension.length) : basename
}

function canPreviewResource(item) {
  const source = String(item?.source || "").toLowerCase()
  return Boolean(
    item?.can_preview &&
    (item?.url ||
      (source === "juying" && item?.provider_data?.resource_id) ||
      (source === "hdhive" && item?.resource_ref) ||
      (item?.pending_resolution && ["seedhub", "pinglian"].includes(source))),
  )
}

function previewResourceKey(item) {
  const source = String(item?.source || "").toLowerCase()
  const providerData = item?.provider_data || {};
  const resourceId = String(providerData.resource_id || "");
  if (source === "juying" && resourceId) return `${source}:${resourceId}`
  return String(
    item?.url ||
      `${source}:${item?.resource_type || ""}:${
        item?.resource_ref || providerData.seed_id || providerData.path || providerData.resource_id || item?.id || ""
      }`,
  )
}

async function requestResourceUrl(item) {
  const response = unwrapResponse(
    await api.post("plugin/PanSearch/search/unlock", {
      source: item.source || sourceTest.source,
      item,
      config: sourceTestConfig(item.source || sourceTest.source),
    }),
  )
  if (response.success === false) throw new Error(response.message || "资源链接获取失败")
  const data = response.data?.data || response.data || {}
  if (!data.url) throw new Error(response.message || "资源链接获取失败")
  applySearchAccountPoints(item.source, data.deducted_points);
  item.url = data.url
  item.need_access = false
  item.need_unlock = false
  item.is_unlocked = true
  return response.message || "资源链接已获取"
}

function applySearchAccountPoints(source, deductedPoints) {
  const normalizedSource = String(source || "")
    .trim()
    .toLowerCase();
  const points = Number(deductedPoints);
  if (!normalizedSource || !Number.isFinite(points) || points <= 0) return;
  const account = options.searchAccounts?.[normalizedSource];
  const available = Number(account?.points?.available);
  if (!account || !Number.isFinite(available)) return;
  options.searchAccounts = {
    ...options.searchAccounts,
    [normalizedSource]: {
      ...account,
      points: {
        ...(account.points || {}),
        available: Math.max(0, available - points),
      },
    },
  }
}

const previewHdhiveResourceRef = ref("");
const previewHdhiveUnlocked = ref(false);

async function previewResource(item) {
  if (!canPreviewResource(item)) return
  if (previewingUrl.value) return;
  const requestId = ++previewRequestId
  previewingUrl.value = previewResourceKey(item)
  const shareUrl = String(item.url || "")
  previewVisible.value = true
  previewLoading.value = false
  previewError.value = ""
  previewItems.value = []
  previewResourceType.value = String(item.resource_type || "").toLowerCase()
  previewShareUrl.value = shareUrl
  previewSource.value = String(item.source || "").toLowerCase()
  previewJuyingResourceId.value = String(item.provider_data?.resource_id || "");
  previewHdhiveResourceRef.value = String(item.resource_ref || "");
  previewProviderData.value = {...(item.provider_data || {})};
  previewPendingResource.value = {
    pending_resolution: Boolean(item.pending_resolution),
    provider_data: {...(item.provider_data || {})},
  }
  previewHdhiveUnlocked.value = Boolean(item.is_unlocked)
  previewTargetSeason.value = item.target_season ?? null
  previewTargetEpisodes.value = Array.isArray(item.target_episodes) ? [...item.target_episodes] : []
  previewMeta.value = {
    provider_name: "",
    resource_type_name: String(item.resource_type_name || item.resource_type || ""),
    share_url: previewShareUrl.value,
  }
  const breadcrumbs = [{ id: "", name: "根目录" }]
  previewBreadcrumbs.value = breadcrumbs
  await loadPreviewDirectory("", breadcrumbs, requestId)
  if (requestId === previewRequestId && previewShareUrl.value) {
    item.url = previewShareUrl.value
    item.need_access = false
    item.need_unlock = false
    item.is_unlocked = true
    item.pending_resolution = false
  }
  if (requestId === previewRequestId) previewingUrl.value = ""
}

async function loadPreviewDirectory(parentId, breadcrumbs, requestId = ++previewRequestId) {
  const pendingJuying = previewSource.value === "juying" && previewJuyingResourceId.value
  const pendingHdhive = previewSource.value === "hdhive" && previewHdhiveResourceRef.value && !previewShareUrl.value;
  const pendingSourceResource =
    ["seedhub", "pinglian"].includes(previewSource.value) &&
    previewPendingResource.value.pending_resolution &&
    !previewShareUrl.value
  if (!previewShareUrl.value && !pendingJuying && !pendingHdhive && !pendingSourceResource) return
  const resourceType = previewResourceType.value
  const shareUrl = previewShareUrl.value
  previewLoading.value = true
  previewError.value = ""
  try {
    const response = unwrapResponse(
      await api.post("plugin/PanSearch/search/preview", {
        resource_type: resourceType,
        url: shareUrl,
        parent_id: parentId || "",
        source: previewSource.value,
        resource_ref: previewHdhiveResourceRef.value,
        provider_data: previewProviderData.value,
        is_unlocked: previewHdhiveUnlocked.value,
        target_season: previewTargetSeason.value,
        target_episodes: previewTargetEpisodes.value,
        ...previewPendingResource.value,
        config:
          pendingJuying || pendingHdhive || pendingSourceResource ? sourceTestConfig(previewSource.value) : undefined,
      }),
    )
    if (requestId !== previewRequestId || !previewVisible.value) return
    if (response.success === false) throw new Error(response.message || "资源预览失败")
    const data = response.data?.data || response.data || {}
    if (data.resource_type) {
      previewResourceType.value = String(data.resource_type).toLowerCase()
    }
    if (data.share_url) {
      previewShareUrl.value = String(data.share_url)
      previewPendingResource.value.pending_resolution = false
    }
    previewItems.value = Array.isArray(data.items) ? data.items : []
    previewMeta.value = {
      provider_name: String(data.provider_name || ""),
      resource_type_name: String(data.resource_type_name || ""),
      display_name: String(data.display_name || ""),
      info_hash: String(data.info_hash || ""),
      size: Number(data.size || 0),
      share_url: String(data.share_url || shareUrl),
    }
    previewBreadcrumbs.value = breadcrumbs
  } catch (error) {
    if (requestId !== previewRequestId || !previewVisible.value) return
    previewItems.value = []
    previewError.value = error?.response?.data?.message || error.message || String(error)
  } finally {
    if (requestId === previewRequestId) previewLoading.value = false
  }
}

function openPreviewFolder(file) {
  if (!file?.can_enter || previewLoading.value) return
  loadPreviewDirectory(String(file.id || ""), [
    ...previewBreadcrumbs.value,
    { id: String(file.id || ""), name: String(file.name || "未命名目录") },
  ])
}

function openPreviewBreadcrumb(index) {
  const breadcrumb = previewBreadcrumbs.value[index]
  if (!breadcrumb || previewLoading.value) return
  loadPreviewDirectory(String(breadcrumb.id || ""), previewBreadcrumbs.value.slice(0, index + 1))
}

function confirmUnlock(item) {
  unlockItem.value = item || null
  unlockError.value = ""
  unlockVisible.value = Boolean(item)
}

async function unlockResource() {
  const item = unlockItem.value
  if (!item || unlocking.value) return
  unlocking.value = true
  unlockError.value = ""
  let shouldPreview = false
  try {
    const message = await requestResourceUrl(item)
    unlockVisible.value = false
    shouldPreview = canPreviewResource(item)
    notify(shouldPreview ? `${message}，正在打开预览` : `${message}，现在可以复制`)
  } catch (error) {
    unlockError.value = error?.response?.data?.message || error.message || String(error)
  } finally {
    unlocking.value = false
  }
  if (shouldPreview) await previewResource(item)
}

function isPointUnlockResource(item) {
  const source = String(item?.source || "").toLowerCase();
  return ["hdhive", "dian115"].includes(source);
}

function testItemStatus(item) {
  if (item?.need_unlock && Number(item.unlock_points || 0) > 0) {
    return {
      label: `${Number(item.unlock_points || 0)} 积分`,
      color: "warning",
    }
  }
  if (isPointUnlockResource(item) && Number(item?.unlock_points || 0) === 0) {
    return {label: "免费", color: "success"};
  }
  if (item?.need_access) return { label: "待获取", color: "info" }
  return null
}

function tmdbCandidateSubtitle(item) {
  const values = [item.media_type_name, item.year]
  if (item.original_title && item.original_title !== item.title) {
    values.push(item.original_title)
  }
  if (Number(item.vote_average) > 0) {
    values.push(`评分 ${Number(item.vote_average).toFixed(1)}`)
  }
  return values.filter(Boolean).join(" · ")
}

function sourceTestConfig(source) {
  const keys = [
    "resource_type_order",
    "search_proxy",
    "search_proxy_username",
    "search_proxy_password",
    ...(sourceTestConfigKeys[source] || []),
  ]
  return Object.fromEntries(
    keys
      .filter((key) => key in config && config[key] !== undefined)
      .map((key) => [key, JSON.parse(JSON.stringify(config[key]))]),
  )
}

function openQrCode(provider) {
  qrProvider.value = String(provider || "115")
  qrVisible.value = true
}

async function handleQrSuccess(payload) {
  const provider = String(payload?.provider || qrProvider.value || "")
    .trim()
    .toLowerCase()
  const credentials = payload?.credentials
  if (credentials && typeof credentials === "object") {
    Object.assign(config, credentials)
  }
  try {
    if (provider) {
      await refreshAccount(`drive:${provider}`, { silent: true })
    }
  } catch (error) {
    notify(`账号信息刷新失败：${error.message || error}`, "warning")
  }
}

function openDirectoryPicker(fieldKey, provider) {
  directoryField.value = fieldKey
  directoryProvider.value = String(provider || config.cloud_drive || "115")
  directoryInitialPath.value = String(config[fieldKey] || "/").trim() || "/"
  directoryVisible.value = true
}

function selectDirectory(path) {
  if (directoryField.value) config[directoryField.value] = path || "/"
  directoryVisible.value = false
}

function optionScopeForTab(tab = activeTab.value) {
  return optionScopeByTab[String(tab || "basic")] || "base"
}

async function loadOptions(scope = "base", { force = false } = {}) {
  const normalizedScope = String(scope || "base")
    .trim()
    .toLowerCase()
  if (!force && loadedOptionScopes.has(normalizedScope)) return
  if (optionScopeRequests.has(normalizedScope)) {
    return optionScopeRequests.get(normalizedScope)
  }
  const request = (async () => {
    if (normalizedScope === "base") options.rsshubLoading = true;
    const query = new URLSearchParams({ scope: normalizedScope })
    const response = unwrapResponse(await api.get(`plugin/PanSearch/ui_options?${query}`))
    if (response.success === false) {
      throw new Error(response.message || "加载配置选项失败")
    }
    applyOptions(response.data?.data || response.data || response)
    loadedOptionScopes.add(normalizedScope)
    if (normalizedScope === "base") options.rsshubLoading = false;
  })()
  optionScopeRequests.set(normalizedScope, request)
  try {
    return await request
  } finally {
    if (normalizedScope === "base") options.rsshubLoading = false;
    optionScopeRequests.delete(normalizedScope)
  }
}

async function reloadVisibleOptionScopes() {
  loadedOptionScopes.clear()
  const scopes = [...new Set(["base", optionScopeForTab()])]
  await Promise.all(scopes.map((scope) => loadOptions(scope, { force: true })))
}

async function refreshAccount(accountKey, { silent = false } = {}) {
  const normalizedKey = String(accountKey || "")
    .trim()
    .toLowerCase()
  if (!normalizedKey || refreshingAccounts.value.includes(normalizedKey)) return
  refreshingAccounts.value = [...refreshingAccounts.value, normalizedKey]
  try {
    const response = unwrapResponse(
      await api.post("plugin/PanSearch/account/refresh", {
        key: normalizedKey,
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "账户信息刷新失败")
    }
    const data = response.data?.data || response.data || {}
    const [category, source] = String(data.key || normalizedKey).split(":", 2)
    const account = data.account && typeof data.account === "object" ? data.account : {}
    if (category === "drive") {
      options.accounts = { ...options.accounts, [source]: account }
      if (String(config.cloud_drive || "") === source) {
        options.account = account
      }
    } else if (category === "search") {
      options.searchAccounts = { ...options.searchAccounts, [source]: account }
    }
    if (!silent) {
      notify(
        response.message || (data.limited ? "刷新过于频繁，已显示最近一次账户信息" : "账户信息已刷新"),
        data.limited ? "warning" : "success",
      )
    }
  } catch (error) {
    if (!silent) {
      notify(`账户信息刷新失败：${error.message || error}`, "error")
    }
  } finally {
    refreshingAccounts.value = refreshingAccounts.value.filter((key) => key !== normalizedKey)
  }
}

async function save() {
  if (saving.value) return
  saving.value = true
  messageVisible.value = false
  try {
    const payload = JSON.parse(JSON.stringify(config))
    delete payload.hdhive_oauth_callback
    const response = unwrapResponse(await api.post("plugin/PanSearch/config/save", payload))
    if (response.success === false) {
      throw new Error(response.message || "保存配置失败")
    }
    const savedConfig = response.data?.data || response.data
    if (savedConfig && typeof savedConfig === "object") {
      Object.assign(config, JSON.parse(JSON.stringify(savedConfig)))
    }
    await reloadVisibleOptionScopes()
    notify(response.message || "配置已保存")
  } catch (e) {
    notify(`保存配置失败：${e.message || e}`, "error")
  } finally {
    saving.value = false
  }
}

async function handleCheckinResult(result) {
  const providerName = result?.providerName || "签到服务"
  if (result?.success && result?.providerKey) {
    await refreshAccount(`search:${result.providerKey}`, { silent: true })
  }
  notify(
    result?.message || (result?.success ? `${providerName} 签到完成` : `${providerName} 签到失败`),
    result?.success ? "success" : "error",
  )
}

let hdhiveOauthWindow = null;
const hdhiveOauthAction = ref("");

function hdhiveOAuthPayload() {
  return {
    client_id: String(config.hdhive_client_id || "").trim(),
    app_secret: String(config.hdhive_api_key || "").trim(),
    redirect_uri: String(config.hdhive_redirect_uri || "").trim(),
    response_mode: String(config.hdhive_response_mode || "redirect").trim(),
    scope: "query unlock write",
  }
}

async function startHdhiveOAuth() {
  if (hdhiveOauthAction.value) return
  const oauthPopup = window.open("about:blank", "hdhive-openapi-oauth", "width=560,height=760,noopener=no")
  hdhiveOauthAction.value = "start"
  try {
    const response = unwrapResponse(await api.post("plugin/PanSearch/hdhive/oauth/start", hdhiveOAuthPayload()))
    if (response.success === false) {
      throw new Error(response.message || "生成 HDHive 授权链接失败")
    }
    const data = response.data?.data || response.data || {}
    if (!data.authorize_url) throw new Error("HDHive 授权链接为空")
    if (oauthPopup) {
      oauthPopup.location.href = data.authorize_url
      hdhiveOauthWindow = oauthPopup
    } else {
      window.open(data.authorize_url, "_blank", "noopener,noreferrer")
    }
    notify(response.message || "HDHive 授权页已打开")
  } catch (error) {
    if (oauthPopup && !oauthPopup.closed) oauthPopup.close()
    notify(`发起 HDHive 授权失败：${error.message || error}`, "error")
  } finally {
    hdhiveOauthAction.value = ""
  }
}

async function exchangeHdhiveOAuth(callbackData = {}) {
  if (hdhiveOauthAction.value) return
  hdhiveOauthAction.value = "exchange"
  try {
    const response = unwrapResponse(
      await api.post("plugin/PanSearch/hdhive/oauth/exchange", {
        ...hdhiveOAuthPayload(),
        callback: String(config.hdhive_oauth_callback || "").trim(),
        code: String(callbackData.code || "").trim(),
        state: String(callbackData.state || "").trim(),
      }),
    )
    if (response.success === false) {
      throw new Error(response.message || "HDHive 用户授权失败")
    }
    const data = response.data?.data || response.data || {}
    config.hdhive_access_token = data.access_token || ""
    config.hdhive_refresh_token = data.refresh_token || ""
    config.hdhive_token_expires_at = Number(data.token_expires_at || 0)
    config.hdhive_auth_code = ""
    config.hdhive_oauth_callback = ""
    if (hdhiveOauthWindow && !hdhiveOauthWindow.closed) hdhiveOauthWindow.close()
    hdhiveOauthWindow = null
    notify(data.warning || "HDHive OpenAPI 授权成功，请保存配置使 Token 生效", data.warning ? "warning" : "success")
  } catch (error) {
    notify(`完成 HDHive 授权失败：${error.message || error}`, "error")
  } finally {
    hdhiveOauthAction.value = ""
  }
}

function handleHdhiveOAuthMessage(event) {
  if (event.origin !== "https://re0.me") return;
  const payload = event.data
  if (
    !payload ||
    payload.source !== "hdhive-openapi" ||
    payload.type !== "authorization_response" ||
    String(payload.client_id || "") !== String(config.hdhive_client_id || "")
  )
    return
  exchangeHdhiveOAuth(payload)
}

function openSourceTest(source) {
  if (testingSource.value || !sourceNames[source]) return
  sourceTest.source = source
  tmdbCandidates.value = []
  tmdbSearched.value = false
  selectedTmdbId.value = 0
  testResult.value = {}
  selectedTestResourceType.value = "";
  testSubmitted.value = false
  testError.value = ""
  testElapsed.value = null
  sourceTestVisible.value = true
}

async function searchTmdbCandidates() {
  if (searchingTmdb.value || testingSource.value) return
  const title = String(sourceTest.title || "").trim()
  if (!title) {
    testError.value = "请输入媒体名称"
    return
  }
  searchingTmdb.value = true
  tmdbSearched.value = false
  tmdbCandidates.value = []
  selectedTmdbId.value = 0
  testResult.value = {}
  selectedTestResourceType.value = "";
  testSubmitted.value = false
  testError.value = ""
  try {
    const response = unwrapResponse(await api.post("plugin/PanSearch/search/tmdb", { title }))
    if (response.success === false) {
      throw new Error(response.message || "TMDB 查询失败")
    }
    const data = response.data?.data || response.data || {}
    tmdbCandidates.value = Array.isArray(data.items) ? data.items : []
    tmdbSearched.value = true
  } catch (e) {
    testError.value = e?.response?.data?.message || e.message || String(e)
  } finally {
    searchingTmdb.value = false
  }
}

async function testSource(candidate) {
  if (testingSource.value || !candidate?.tmdb_id) return
  testingSource.value = sourceTest.source
  selectedTmdbId.value = Number(candidate.tmdb_id || 0)
  testError.value = ""
  testElapsed.value = null
  testSubmitted.value = false
  messageVisible.value = false
  try {
    const response = unwrapResponse(
      await api.post("plugin/PanSearch/search/test", {
        source: sourceTest.source,
        title: candidate.title,
        original_title: candidate.original_title || "",
        year: candidate.year || null,
        tmdb_id: candidate.tmdb_id,
        imdb_id: candidate.imdb_id || null,
        tvdb_id: candidate.tvdb_id || null,
        douban_id: candidate.douban_id || null,
        bangumi_id: candidate.bangumi_id || null,
        anilist_id: candidate.anilist_id || null,
        media_type: candidate.media_type,
        season: candidate.media_type === "tv" ? sourceTest.season : null,
        config: sourceTestConfig(sourceTest.source),
      }),
    )
    if (response.success === false) {
      testElapsed.value = response.data?.elapsed_seconds ?? null
      throw new Error(response.message || "搜索渠道测试失败")
    }
    testResult.value = response.data?.data || response.data || {}
    testElapsed.value = testResult.value.elapsed_seconds ?? null
    testSubmitted.value = true
    notify(response.message || "搜索渠道测试完成")
  } catch (e) {
    const status = Number(e?.response?.status || 0)
    const errorData = e?.response?.data?.data || e?.response?.data || {}
    testElapsed.value = errorData.elapsed_seconds ?? testElapsed.value
    testError.value =
      status === 502
        ? `${sourceNames[sourceTest.source] || "搜索渠道"} 测试请求被网关中断（HTTP 502），请检查渠道服务状态及反向代理超时`
        : e?.response?.data?.message || e.message || String(e)
  } finally {
    testingSource.value = ""
    selectedTmdbId.value = 0
  }
}

async function testAutoSubscribe(provider) {
  if (testingAutoSubscribe.value) return;
  testingAutoSubscribe.value = provider;
  autoSubscribeTestProvider.value = provider;
  autoSubscribeTestVisible.value = true;
  autoSubscribeTestLoading.value = true;
  autoSubscribeTestResult.value = null;
  autoSubscribeTestError.value = "";
  try {
    const response = unwrapResponse(
      await api.post("plugin/PanSearch/auto_subscribe/test", {
        provider_id: provider,
        config: JSON.parse(JSON.stringify(config)),
      }),
    )
    autoSubscribeTestResult.value = response;
    if (response.success === false) autoSubscribeTestError.value = response.message || "榜单测试失败";
  } catch (error) {
    autoSubscribeTestError.value = error?.response?.data?.message || error.message || String(error);
  } finally {
    autoSubscribeTestLoading.value = false;
    testingAutoSubscribe.value = "";
  }
}

onMounted(async () => {
  window.addEventListener("message", handleHdhiveOAuthMessage)
  emit("layout", { maxWidth: "62rem" })
  try {
    await loadOptions("base")
  } catch (e) {
    notify(`加载配置选项失败：${e.message || e}`, "warning")
  }
})

watch(activeTab, (tab) => {
  loadOptions(optionScopeForTab(tab)).catch((error) => {
    notify(`加载配置选项失败：${error.message || error}`, "warning")
  })
})

onBeforeUnmount(() => {
  previewRequestId += 1
  window.removeEventListener("message", handleHdhiveOAuthMessage)
  if (hdhiveOauthWindow && !hdhiveOauthWindow.closed) hdhiveOauthWindow.close()
})

watch(previewVisible, (visible) => {
  if (visible) return
  previewRequestId += 1
  previewingUrl.value = ""
  previewLoading.value = false
  previewError.value = ""
  previewItems.value = []
  previewMeta.value = {}
  previewBreadcrumbs.value = []
  previewResourceType.value = ""
  previewShareUrl.value = ""
  previewSource.value = ""
  previewJuyingResourceId.value = ""
  previewHdhiveResourceRef.value = "";
  previewHdhiveUnlocked.value = false
  previewProviderData.value = {};
  previewPendingResource.value = {};
  previewTargetSeason.value = null
  previewTargetEpisodes.value = []
})

watch(
  () => props.initialConfig,
  (value) => {
    Object.assign(config, JSON.parse(JSON.stringify(value || {})));
    normalizeAutoSubscribeYears(config);
  },
  { deep: true },
)

watch(
  [() => config.cloud_drive, () => config.cross_transfer_enabled, () => options.cloudDrives],
  ([provider, crossTransfer, drives], [previousProvider, previousCrossTransfer, previousDrives]) => {
    if (provider === previousProvider && crossTransfer === previousCrossTransfer && drives === previousDrives) return
    const supported = new Set(createResourceTypeItems(options.cloudDrives, config).map((item) => item.value))
    config.resource_type_order = (config.resource_type_order || []).filter((value) => supported.has(value))
  },
)
</script>

<style scoped>
.cloud-subscribe-config {
  display: flex;
  width: min(62rem, calc(100vw - 32px));
  max-width: min(62rem, 100%);
  min-width: 0;
  height: min(800px, calc(100dvh - 64px));
  max-height: min(800px, calc(100dvh - 64px));
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

:global(.v-overlay__content:has(.cloud-subscribe-config)) {
  overflow: hidden !important;
}

:global(.v-overlay__content:has(.cloud-subscribe-config) > *) {
  min-height: 0;
  max-height: 100%;
  overflow: hidden !important;
}

.config-shell {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: auto;
  max-height: 100%;
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.config-header {
  min-width: 0;
  flex-wrap: nowrap;
}

.config-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.config-body {
  width: 100%;
  max-width: 100%;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cloud-subscribe-config :deep(.v-field),
.cloud-subscribe-config :deep(.v-selection-control) {
  font-size: 0.875rem;
}

.cloud-subscribe-config :deep(.v-field) {
  --v-input-control-height: 38px;
}

.config-tabs :deep(.v-tab) {
  min-width: 132px;
  text-transform: none;
}

.config-tabs {
  flex: 0 0 auto;
}

.config-content-scroll {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  overflow: hidden;
}

.config-actions {
  position: relative;
  min-height: 68px;
}

.save-progress {
  position: absolute;
  inset: 0 0 auto;
}

.save-state {
  display: flex;
  align-items: center;
  gap: 8px;
  color: rgb(var(--v-theme-primary));
  font-size: 0.875rem;
}

.save-config-button {
  min-width: 132px;
  height: 42px;
  font-weight: 600;
  letter-spacing: 0;
  box-shadow: 0 3px 8px rgba(var(--v-theme-primary), 0.24) !important;
}

.source-test-card {
  max-height: min(600px, calc(100dvh - 48px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-card--results {
  height: min(600px, calc(100dvh - 48px));
}

.auto-subscribe-test-card {
  max-height: min(620px, calc(100dvh - 48px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.auto-subscribe-test-body {
  min-height: 0;
  overflow: hidden;
}

.auto-subscribe-test-loading {
  min-height: 180px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 12px;
}

.auto-subscribe-test-list {
  max-height: min(420px, 52dvh);
  overflow-y: auto;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
}

.source-test-header,
.source-test-actions {
  flex: 0 0 auto;
}

.source-test-form {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-body {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-fields {
  flex: 0 0 auto;
}

.source-test-error {
  flex: 0 0 auto;
}

.source-test-tmdb {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
}

.source-test-tmdb-scroll {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.source-test-loading {
  min-height: 220px;
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  text-align: center;
}

.source-test-tmdb-item {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.source-test-result {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  margin-top: 8px;
}

.source-test-summary {
  flex: 0 0 auto;
  padding: 10px 12px;
  margin-bottom: 8px;
  border-left: 3px solid rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.07);
}

.source-test-summary__line {
  display: flex;
  align-items: baseline;
  flex-wrap: nowrap;
  gap: 14px;
  overflow-x: auto;
  white-space: nowrap;
  color: rgba(var(--v-theme-on-surface), 0.86);
  font-size: 0.8125rem;
  line-height: 1.45;
  scrollbar-width: none;
}

.source-test-summary__line::-webkit-scrollbar {
  display: none;
}

.source-test-summary__line strong {
  color: rgb(var(--v-theme-primary));
  font-size: 0.9375rem;
}

.source-test-notice {
  font-size: 0.81rem;
  line-height: 1.4;
  margin-bottom: 2px !important;
}

.source-test-result-scroll {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.source-test-tabs {
  flex: 0 0 auto;
  min-height: 38px;
  height: 38px;
  margin-top: 0;
  margin-bottom: 4px;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.source-test-resource-tabs :deep(.v-slide-group__content) {
  min-height: 38px;
  height: 38px;
  align-items: flex-end;
}

.source-test-resource-tabs :deep(.v-tab) {
  height: 38px;
  min-height: 38px;
  min-width: 0;
  padding-inline: 12px;
}

.source-test-tabs :deep(.v-btn__content) {
  text-transform: none;
}

.source-test-result-list {
  padding: 0;
}

.source-test-result-item {
  min-height: 0 !important;
  height: auto !important;
  padding-top: 6px;
  padding-bottom: 6px;
}

.source-test-result-item :deep(.v-list-item__content) {
  align-self: stretch;
  min-width: 0;
}

.source-test-item-content {
  width: 100%;
  min-width: 0;
  position: relative;
  padding-right: 82px;
}

.source-test-item-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.45;
}

.source-test-item-meta {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: nowrap;
  gap: 6px;
  overflow: hidden;
  padding-top: 4px;
  padding-bottom: 2px;
  line-height: 1.4;
}

.source-test-item-meta > :deep(.v-chip),
.source-test-item-meta > span {
  flex: 0 0 auto;
}

.source-test-item-actions {
  position: absolute;
  top: 22px;
  right: 0;
  display: flex;
  align-items: center;
  gap: 2px;
}

.source-preview-card {
  max-height: min(78vh, 720px);
  border-radius: 10px;
}

.source-preview-header {
  min-height: 58px;
  background: rgb(var(--v-theme-surface));
}

.source-preview-body {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.source-preview-loading {
  min-height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.source-preview-list-scroll {
  min-height: 0;
  max-height: min(58vh, 520px);
  overflow-y: auto;
}

.source-preview-list {
  font-size: 0.86rem;
}

.source-preview-list :deep(.v-list-item-title) {
  font-size: 0.86rem;
  line-height: 1.35rem;
}

.source-preview-meta {
  display: flex;
  gap: 8px 18px;
  padding: 9px 12px;
  border: 1px solid rgba(var(--v-border-color), 0.12);
  border-radius: 6px;
  background: rgba(var(--v-theme-surface-variant), 0.22);
  overflow-wrap: anywhere;
  line-height: 1.55;
}

.source-preview-meta > span {
  margin-right: 0;
  white-space: nowrap;
}

.source-preview-breadcrumbs {
  display: flex;
  min-height: 32px;
  align-items: center;
  overflow-x: auto;
  padding-bottom: 6px;
  white-space: nowrap;
}

.source-preview-file--directory {
  cursor: pointer;
}

.source-preview-file--directory:hover {
  background: rgba(var(--v-theme-primary), 0.06);
}

.source-preview-file :deep(.v-list-item__prepend) {
  width: 36px;
  min-width: 36px;
}

.source-preview-file :deep(.v-list-item__content) {
  min-width: 0;
  overflow: hidden;
}

.source-preview-file-name {
  display: flex;
  flex-direction: row;
  justify-content: flex-start;
  min-width: 0;
  overflow: hidden;
  direction: ltr;
  text-align: left;
  white-space: nowrap;
}

.source-preview-file-stem {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  direction: ltr;
  text-align: left;
}

.source-preview-file-extension {
  flex: 0 0 auto;
}

.source-preview-file-size {
  width: 76px;
  flex: 0 0 76px;
  text-align: right;
  white-space: nowrap;
}

.config-window {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  flex: 1 1 0;
  display: flex;
  overflow: hidden;
}

.config-window-section {
  box-sizing: border-box;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  flex: 1 1 0;
  overflow: hidden;
}

@media (min-width: 601px) {
  .config-window-section {
    padding: 12px 14px 20px;
  }
}

@media (max-width: 600px) {
  :global(.v-overlay__content:has(.cloud-subscribe-config)) {
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100dvh !important;
    max-height: 100dvh !important;
    margin: 0 !important;
  }

  .cloud-subscribe-config {
    width: 100%;
    max-width: 100%;
    height: 100%;
    max-height: 100%;
  }

  .config-shell {
    width: 100%;
    height: 100%;
    max-height: 100%;
    min-height: 0;
    border-radius: 0 !important;
  }

  .config-body,
  .config-content-scroll {
    height: 0;
  }

  .config-content-scroll {
    overflow-y: auto;
    overscroll-behavior-y: contain;
    scroll-behavior: auto;
  }

  .config-header {
    padding-left: 10px !important;
    padding-right: 10px !important;
  }

  .config-header-action {
    flex: 0 0 34px;
    min-width: 34px !important;
    width: 34px;
    padding: 0 !important;
  }

  .config-header-action :deep(.v-btn__content) {
    display: none;
  }

  .config-header-action :deep(.v-btn__prepend) {
    margin: 0;
  }

  .config-actions {
    min-height: 64px;
    padding-inline: 12px !important;
  }

  .save-state {
    font-size: 0.8125rem;
  }

  .config-tabs :deep(.v-tab) {
    min-width: 112px;
  }

  .config-window-section {
    padding: 10px 12px 20px;
  }
}
</style>
