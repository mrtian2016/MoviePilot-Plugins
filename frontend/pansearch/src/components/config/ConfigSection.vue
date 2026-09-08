<template>
  <div class="config-tab-content">
    <v-tabs
      v-if="section.subtabs?.length"
      v-model="activeSubtab"
      color="primary"
      density="compact"
      show-arrows
      class="section-tabs mb-4">
      <v-tab v-for="tab in section.subtabs" :key="tab.value" :value="tab.value">
        <v-icon :icon="tab.icon" size="small" class="mr-2" />
        {{ tab.title }}
      </v-tab>
    </v-tabs>
    <div class="config-section-scroll">
      <template v-for="(group, index) in visibleGroups" :key="group.title">
        <section class="config-group">
          <div v-if="!group.hideHeading" class="group-heading">
            <v-icon :icon="group.icon" color="primary" size="small" />
            <div>
              <div class="text-subtitle-2 font-weight-medium">
                {{ group.title }}
              </div>
              <div v-if="group.hint" class="text-caption text-medium-emphasis">
                {{ group.hint }}
              </div>
            </div>
          </div>

          <v-row dense>
            <template v-for="field in group.fields" :key="field.key">
              <v-col v-if="!field.show || field.show(config)" cols="12" :md="field.cols || 6" class="config-field-col">
                <AccountInfo
                  v-if="field.type === 'account'"
                  :account="field.data"
                  :compact="Boolean(field.compact)"
                  :loading="refreshingAccounts.includes(field.accountKey)"
                  :refreshable="Boolean(field.accountKey)"
                  @refresh="emit('refresh-account', field.accountKey)" />
                <v-alert v-else-if="field.type === 'info'" type="info" variant="tonal" density="compact">
                  <div class="text-body-2 font-weight-medium mb-1">
                    {{ field.label }}
                  </div>
                  <div v-for="line in field.lines || []" :key="line" class="text-caption mb-1">• {{ line }}</div>
                </v-alert>
                <div v-else-if="field.type === 'media-library-webhook'" class="media-library-webhook">
                  <v-alert
                    v-if="!(field.items || []).length"
                    type="warning"
                    variant="tonal"
                    density="compact"
                    text="尚未配置 Emby 媒体服务器，暂时无法生成通知地址。" />
                  <v-text-field
                    v-for="item in field.items || []"
                    v-else
                    :key="item.value"
                    :model-value="mediaLibraryWebhookUrl(field, item.value)"
                    :label="`${item.title || item.value} Webhook URL`"
                    readonly
                    density="compact"
                    variant="outlined"
                    hide-details="auto">
                    <template #append-inner>
                      <div class="media-library-webhook__actions">
                        <v-btn
                          icon="mdi-content-copy"
                          variant="text"
                          color="primary"
                          size="small"
                          title="复制 Webhook URL"
                          @click="emit('copy-text', mediaLibraryWebhookUrl(field, item.value))" />
                      </div>
                    </template>
                  </v-text-field>
                </div>
                <v-btn
                  v-else-if="field.type === 'test-source'"
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-flask-outline"
                  :loading="testingSource === field.source"
                  :disabled="
                    !isTestSourceConfigured(field.source) || (Boolean(testingSource) && testingSource !== field.source)
                  "
                  :title="testSourceTitle(field.source)"
                  @click="emit('test-source', field.source)">
                  {{ field.label }}
                </v-btn>
                <v-btn
                  v-else-if="field.type === 'test-auto-subscribe'"
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-flask-outline"
                  :loading="testingAutoSubscribe === field.provider"
                  :disabled="
                    !isAutoSubscribeConfigured(field.provider) ||
                    (Boolean(testingAutoSubscribe) && testingAutoSubscribe !== field.provider)
                  "
                  :title="autoSubscribeTestTitle(field.provider)"
                  @click="emit('test-auto-subscribe', field.provider)">
                  {{ field.label }}
                </v-btn>
                <div v-else-if="field.type === 'hdhive-oauth'" class="hdhive-oauth-panel">
                  <div class="d-flex align-center flex-wrap ga-2 mb-2">
                    <v-chip size="small" variant="tonal" :color="config.hdhive_access_token ? 'success' : 'warning'">
                      {{ config.hdhive_access_token ? "已获取用户 Token" : "尚未完成用户授权" }}
                    </v-chip>
                    <span class="text-caption text-medium-emphasis">授权范围：query unlock write</span>
                  </div>
                  <v-text-field
                    v-if="config.hdhive_response_mode !== 'postmessage'"
                    v-model="config.hdhive_oauth_callback"
                    label="授权完成后的完整回调 URL"
                    hint="请粘贴同时包含 code 和 state 的完整地址；插件会校验 state 后在服务端换取 Token。"
                    persistent-hint
                    clearable
                    density="compact"
                    variant="outlined"
                    hide-details="auto"
                    class="mb-2" />
                  <div class="d-flex flex-wrap ga-2">
                    <v-btn
                      color="primary"
                      variant="tonal"
                      prepend-icon="mdi-open-in-new"
                      :loading="hdhiveOauthAction === 'start'"
                      :disabled="Boolean(hdhiveOauthAction) && hdhiveOauthAction !== 'start'"
                      @click="emit('hdhive-oauth-start')">
                      打开 HDHive 授权页
                    </v-btn>
                    <v-btn
                      v-if="config.hdhive_response_mode !== 'postmessage'"
                      color="success"
                      variant="tonal"
                      prepend-icon="mdi-shield-check-outline"
                      :loading="hdhiveOauthAction === 'exchange'"
                      :disabled="Boolean(hdhiveOauthAction) || !String(config.hdhive_oauth_callback || '').trim()"
                      @click="emit('hdhive-oauth-exchange')">
                      校验回调并完成授权
                    </v-btn>
                  </div>
                </div>
                <CheckinTimeline
                  v-else-if="field.type === 'checkin-timeline'"
                  :api="api"
                  :providers="field.providers"
                  :config="config"
                  @result="emit('checkin-result', $event)" />
                <RegionMediaMapField
                  v-else-if="field.type === 'region-media-map'"
                  v-model="config[field.key]"
                  :field="field" />
                <VCronField
                  v-else-if="field.type === 'cron'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact" />
                <v-switch
                  v-else-if="field.type === 'switch'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  color="success"
                  density="compact"
                  hide-details="auto"
                  class="config-switch" />
                <div v-else-if="field.type === 'online-documents'" class="online-documents">
                  <div v-if="field.hint" class="text-caption text-medium-emphasis mb-3">{{ field.hint }}</div>
                  <div
                    v-for="(document, documentIndex) in onlineDocuments(field.key)"
                    :key="documentIndex"
                    class="online-document-row">
                    <v-text-field
                      v-model="document.url"
                      label="文档地址"
                      placeholder="https://docs.qq.com/..."
                      density="compact"
                      variant="outlined"
                      hide-details="auto" />
                    <v-select
                      v-model="document.resource_types"
                      label="资源类型"
                      :items="field.items || []"
                      multiple
                      chips
                      closable-chips
                      density="compact"
                      variant="outlined"
                      hide-details="auto" />
                    <v-btn
                      icon="mdi-plus"
                      variant="text"
                      color="primary"
                      title="在下方添加文档"
                      @click="addOnlineDocument(field.key, documentIndex)" />
                    <v-btn
                      icon="mdi-delete-outline"
                      variant="text"
                      color="error"
                      title="删除此文档"
                      @click="removeOnlineDocument(field.key, documentIndex)" />
                  </div>
                </div>
                <v-autocomplete
                  v-else-if="field.type === 'select' && field.searchable"
                  v-model="config[field.key]"
                  v-model:search="selectSearch[field.key]"
                  :label="field.label"
                  :items="filteredSelectItems(field)"
                  :multiple="field.multiple"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  :no-filter="true"
                  no-data-text="没有匹配的订阅"
                  chips
                  closable-chips
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-select
                  v-else-if="field.type === 'select'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :items="field.items"
                  :multiple="field.multiple"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  chips
                  closable-chips
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-textarea
                  v-else-if="field.type === 'textarea'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  auto-grow
                  rows="2"
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-combobox
                  v-else-if="field.type === 'combobox'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :items="field.items || []"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :loading="Boolean(field.loading)"
                  :multiple="field.multiple !== false"
                  :return-object="Boolean(field.returnObject)"
                  chips
                  closable-chips
                  clearable
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-text-field
                  v-else-if="field.type === 'cloud-directory'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template #append-inner>
                    <v-btn
                      icon="mdi-folder-search-outline"
                      variant="text"
                      color="primary"
                      size="small"
                      title="浏览网盘目录"
                      @click="emit('browse-directory', field.key, field.driveProvider)" />
                  </template>
                </v-text-field>
                <v-text-field
                  v-else-if="field.type === 'proxy'"
                  v-model="config[field.key]"
                  :name="field.key"
                  autocomplete="off"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  clearable
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template #append-inner>
                    <v-btn
                      icon="mdi-lan-connect"
                      variant="text"
                      color="primary"
                      size="small"
                      title="测试代理连通性、延迟和出口"
                      :loading="testingProxy"
                      :disabled="testingProxy || !hasText(config[field.key])"
                      @click="emit('test-proxy')" />
                  </template>
                </v-text-field>
                <v-text-field
                  v-else-if="field.type === 'auto-subscribe-proxy'"
                  v-model="config[field.key]"
                  :name="field.key"
                  autocomplete="off"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  clearable
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template #append-inner>
                    <v-btn
                      icon="mdi-lan-connect"
                      variant="text"
                      color="primary"
                      size="small"
                      title="测试榜单代理连通性和延迟"
                      :loading="testingAutoSubscribeProxy"
                      :disabled="testingAutoSubscribeProxy || !hasText(config[field.key])"
                      @click="emit('test-auto-subscribe-proxy')" />
                  </template>
                </v-text-field>
                <v-text-field
                  v-else-if="field.type === 'number'"
                  v-model.number="config[field.key]"
                  :label="field.label"
                  type="number"
                  :min="field.min"
                  :max="field.max"
                  :suffix="field.suffix"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :clearable="field.clearable"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-text-field
                  v-else
                  v-model="config[field.key]"
                  :label="field.label"
                  :type="field.type || 'text'"
                  :name="field.key"
                  :autocomplete="field.type === 'password' ? 'new-password' : field.autocomplete || 'off'"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template v-if="field.scanProvider" #append-inner>
                    <v-btn
                      icon="mdi-qrcode-scan"
                      variant="text"
                      color="primary"
                      size="small"
                      title="扫码登录"
                      @click="emit('scan', field.scanProvider)" />
                  </template>
                </v-text-field>
              </v-col>
            </template>
          </v-row>
          <v-divider v-if="index < visibleGroups.length - 1" class="group-divider" />
        </section>
      </template>
    </div>
  </div>
</template>

<script setup>
import {computed, ref} from "vue";
import AccountInfo from "./AccountInfo.vue";
import CheckinTimeline from "./CheckinTimeline.vue";
import RegionMediaMapField from "./RegionMediaMapField.vue";

const props = defineProps({
  section: { type: Object, required: true },
  config: { type: Object, required: true },
  api: { type: [Object, Function], required: true },
  refreshingAccounts: { type: Array, default: () => [] },
  testingSource: { type: String, default: "" },
  testingAutoSubscribe: {type: String, default: ""},
  testingProxy: { type: Boolean, default: false },
  testingAutoSubscribeProxy: {type: Boolean, default: false},
  hdhiveOauthAction: { type: String, default: "" },
})
const emit = defineEmits([
  "scan",
  "browse-directory",
  "test-source",
  "test-auto-subscribe",
  "test-proxy",
  "test-auto-subscribe-proxy",
  "refresh-account",
  "hdhive-oauth-start",
  "hdhive-oauth-exchange",
  "checkin-result",
  "copy-text",
])

const hasText = (value) => Boolean(String(value || "").trim())

function isTestSourceConfigured(source) {
  if (source === "online_docs") {
    return (
      Array.isArray(props.config.online_docs) &&
      props.config.online_docs.some(
        (document) =>
          hasText(document?.url) && Array.isArray(document?.resource_types) && document.resource_types.length,
      )
    )
  }
  if (!Array.isArray(props.config.resource_type_order) || !props.config.resource_type_order.length) return false
  if (source === "pansou") {
    return (
      hasText(props.config.pansou_url) &&
      (!props.config.pansou_auth_enabled ||
        (hasText(props.config.pansou_username) && hasText(props.config.pansou_password)))
    )
  }
  if (source === "hdhive") {
    return props.config.hdhive_query_mode === "api"
      ? hasText(props.config.hdhive_api_key) && hasText(props.config.hdhive_access_token)
      : hasText(props.config.hdhive_username) && hasText(props.config.hdhive_password)
  }
  const credentials = {
    dian115: ["dian115_email", "dian115_password"],
    juying: ["juying_username", "juying_password"],
    pinglian: ["pinglian_username", "pinglian_password"],
  }[source]
  return !credentials || credentials.every((key) => hasText(props.config[key]))
}

function testSourceTitle(source) {
  return isTestSourceConfigured(source) ? "测试当前搜索渠道" : "请先完成渠道账号配置并选择资源类型"
}

function isAutoSubscribeConfigured(provider) {
  // 所有支持的榜单提供方均允许直接测试连通性并抓取示例（未配置具体分类时自动使用默认榜单）
  return ["douban", "tmdb", "bangumi", "anilist", "maoyan", "netflix", "mikan"].includes(provider);
}

function autoSubscribeTestTitle(provider) {
  return "测试抓取最多 3 条榜单示例，仅验证连通性，不创建订阅";
}

function onlineDocuments(key) {
  if (!Array.isArray(props.config[key])) props.config[key] = []
  return props.config[key]
}

function addOnlineDocument(key, index) {
  onlineDocuments(key).splice(index + 1, 0, { url: "", resource_types: [] })
}

function removeOnlineDocument(key, index) {
  const documents = onlineDocuments(key)
  if (documents.length <= 1) {
    documents.splice(0, 1, { url: "", resource_types: [] })
    return
  }
  documents.splice(index, 1)
}

const activeSubtab = ref(props.section.subtabs?.[0]?.value || "")
const selectSearch = ref({})

function normalizeSearchText(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/g, "")
}

function filteredSelectItems(field) {
  const items = Array.isArray(field.items) ? field.items : []
  const keyword = normalizeSearchText(selectSearch.value[field.key])
  if (!keyword) return items
  const selected = new Set(
    (Array.isArray(props.config[field.key]) ? props.config[field.key] : [props.config[field.key]])
      .filter((value) => value !== undefined && value !== null)
      .map((value) => String(value)),
  )
  return items.filter(
    (item) =>
      selected.has(String(item?.value ?? "")) ||
      normalizeSearchText(`${item?.title || ""} ${item?.value || ""}`).includes(keyword),
  )
}
const availableGroups = computed(() => props.section.groups.filter((group) => !group.show || group.show(props.config)))
const visibleGroups = computed(() => {
  const leadingGroups = availableGroups.value.filter((group) => group.beforeTabs)
  const tabGroups = availableGroups.value.filter(
    (group) => !group.beforeTabs && (!props.section.subtabs?.length || !group.tab || group.tab === activeSubtab.value),
  )
  return [...leadingGroups, ...tabGroups]
})

function mediaLibraryWebhookUrl(field, serverName) {
  const relativeUrl = String(field?.urls?.[serverName] || "").trim()
  return relativeUrl ? new URL(relativeUrl, window.location.origin).toString() : ""
}
</script>

<style scoped>
.config-tab-content {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.config-section-scroll {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  flex: 1 1 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  touch-action: pan-y;
  scrollbar-width: none;
}

.config-section-scroll::-webkit-scrollbar {
  display: none;
  width: 0;
  height: 0;
}

.config-group {
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.section-tabs {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  flex: 0 0 auto;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.section-tabs :deep(.v-tab) {
  min-width: 112px;
  text-transform: none;
}

.group-heading {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 10px;
}

.group-divider {
  margin: 16px 0;
}

.config-field-col {
  max-width: 100%;
  min-width: 0;
}

.config-field-col :deep(.v-input),
.config-field-col :deep(.v-field) {
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.config-switch {
  min-width: 0;
  min-height: 40px;
  margin: 0;
  padding-inline: 4px;
}

.config-switch :deep(.v-selection-control) {
  min-height: 40px;
}

.config-switch :deep(.v-label) {
  min-width: 0;
  line-height: 1.35;
  white-space: normal;
  overflow-wrap: anywhere;
}

.config-switch :deep(.v-input__details) {
  padding-inline: 4px;
}

.hdhive-oauth-panel {
  padding: 12px;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  background: rgba(var(--v-theme-primary), 0.035);
}

.media-library-webhook {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.media-library-webhook__actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.online-documents {
  min-width: 0;
}

.region-media-map {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.region-media-map__panels {
  margin-top: 8px;
}

.grouped-select {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.grouped-select__panels {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  overflow: hidden;
}

.online-document-row {
  display: grid;
  grid-template-columns: minmax(220px, 1.2fr) minmax(220px, 1fr) 40px 40px;
  align-items: start;
  gap: 10px;
  margin-bottom: 10px;
}

@media (max-width: 720px) {
  .online-document-row {
    grid-template-columns: minmax(0, 1fr) 40px 40px;
  }

  .online-document-row :deep(.v-select) {
    grid-column: 1;
  }
}

:deep(input:-webkit-autofill),
:deep(input:-webkit-autofill:hover),
:deep(input:-webkit-autofill:focus),
:deep(input:-webkit-autofill:active) {
  -webkit-box-shadow: 0 0 0 1000px rgb(var(--v-theme-surface)) inset !important;
  -webkit-text-fill-color: rgb(var(--v-theme-on-surface)) !important;
  transition: background-color 5000s ease-in-out 0s;
}
</style>
