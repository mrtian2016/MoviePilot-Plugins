<template>
  <v-dialog v-model="model" max-width="780" scrollable class="resource-preview-dialog">
    <v-card class="source-preview-card">
      <v-card-title class="source-preview-header d-flex align-center justify-space-between py-3 px-4">
        <div class="d-flex align-center ga-2 min-w-0">
          <v-avatar color="primary" variant="tonal" size="32" class="rounded-lg">
            <v-icon icon="mdi-folder-open-outline" size="18" />
          </v-avatar>
          <div class="min-w-0">
            <div class="text-subtitle-1 font-weight-bold text-truncate">资源内容预览</div>
            <div class="text-caption text-medium-emphasis text-truncate" :title="meta.share_url || meta.display_name">
              {{ meta.display_name || meta.share_url || "文件树解析" }}
            </div>
          </div>
        </div>
        <div class="d-flex align-center ga-1">
          <v-btn
            v-if="shareUrl"
            icon="mdi-content-copy"
            size="small"
            variant="text"
            title="复制网盘/磁力链接"
            @click="$emit('copy', shareUrl)" />
          <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="model = false" />
        </div>
      </v-card-title>

      <v-divider />
      <v-card-text class="source-preview-body pa-4">
        <div class="source-preview-meta mb-3 d-flex align-center ga-2 flex-wrap">
          <v-chip v-if="meta.provider_name" size="small" variant="tonal" color="primary">
            <v-icon start icon="mdi-cloud-outline" size="13" />
            {{ meta.provider_name }}
          </v-chip>
          <v-chip v-if="meta.resource_type_name" size="small" variant="tonal" color="secondary">
            <v-icon start icon="mdi-tag-outline" size="13" />
            {{ meta.resource_type_name }}
          </v-chip>
          <v-chip v-if="meta.size" size="small" variant="tonal" color="info">
            <v-icon start icon="mdi-harddisk" size="13" />
            {{ formatBytes(meta.size) }}
          </v-chip>
          <v-chip size="small" variant="tonal">共 {{ items.length }} 个项目</v-chip>
        </div>

        <div v-if="breadcrumbs.length > 1" class="source-preview-breadcrumbs mb-2.5">
          <template v-for="(breadcrumb, index) in breadcrumbs" :key="`${breadcrumb.id}-${index}`">
            <span v-if="index > 0" class="breadcrumb-separator">/</span>
            <v-btn
              variant="text"
              size="x-small"
              class="breadcrumb-btn"
              :disabled="loading || index === breadcrumbs.length - 1"
              @click="$emit('open-breadcrumb', index)">
              {{ breadcrumb.name }}
            </v-btn>
          </template>
        </div>

        <div
          v-if="items.length"
          class="source-preview-batch-bar d-flex align-center justify-space-between px-3 py-1.5 mb-2 ga-2">
          <div class="d-flex align-center ga-2">
            <v-checkbox-btn
              :model-value="allSelected"
              density="compact"
              hide-details
              color="primary"
              @update:model-value="$emit('toggle-all', $event)" />
            <span class="text-caption font-weight-medium">全选本层目录</span>
            <span v-if="selectedItems.length" class="text-caption text-primary font-weight-bold">
              (已选 {{ selectedItems.length }} 项)
            </span>
          </div>
          <v-btn
            v-if="selectedItems.length"
            size="small"
            color="primary"
            variant="flat"
            prepend-icon="mdi-check-all"
            class="preview-batch-btn font-weight-bold"
            @click="$emit('batch-transfer')">
            批量转存已选项 ({{ selectedItems.length }})
          </v-btn>
        </div>

        <div v-if="loading" class="pa-8 text-center">
          <v-progress-circular indeterminate color="primary" size="36" />
          <div class="text-caption text-medium-emphasis mt-2">正在解析资源文件目录，请稍候...</div>
        </div>
        <v-alert v-else-if="error" type="error" variant="tonal" density="compact" class="my-3">{{ error }}</v-alert>
        <div v-else-if="!items.length" class="text-center pa-8 text-medium-emphasis">
          <v-icon icon="mdi-folder-alert-outline" size="40" class="mb-2" />
          <div class="text-body-2">当前目录为空或未解析到文件</div>
        </div>
        <div v-else class="source-preview-list-scroll">
          <v-list density="compact" lines="one" class="pa-0">
            <template v-for="(file, index) in items" :key="`${file.name}-${index}`">
              <v-list-item
                class="source-preview-file py-1"
                :class="{ 'source-preview-file--directory': file.can_enter }">
                <template #prepend>
                  <v-checkbox-btn
                    :model-value="isSelected(file)"
                    density="compact"
                    hide-details
                    color="primary"
                    class="mr-2"
                    @click.stop="$emit('toggle-item', file)" />
                  <v-icon
                    :icon="previewFileIcon(file)"
                    :color="file.is_dir ? 'amber' : 'primary'"
                    size="20"
                    class="mr-2"
                    @click="file.can_enter && $emit('open-folder', file)" />
                </template>
                <v-list-item-title
                  class="source-preview-file-name cursor-pointer"
                  :title="file.name"
                  @click="file.can_enter && $emit('open-folder', file)">
                  <span v-if="file.is_dir" class="font-weight-medium">{{ file.name }}</span>
                  <span v-else>{{ previewFileStem(file.name) }}</span>
                  <span v-if="!file.is_dir" class="text-medium-emphasis">{{ previewFileExtension(file.name) }}</span>
                </v-list-item-title>
                <template #append>
                  <span v-if="file.size" class="text-caption text-medium-emphasis mr-2">
                    {{ formatBytes(file.size) }}
                  </span>
                  <v-btn
                    size="small"
                    color="primary"
                    variant="tonal"
                    class="preview-item-transfer-btn font-weight-medium"
                    prepend-icon="mdi-download"
                    title="仅转存此单个内容并自动整理"
                    @click.stop="$emit('single-transfer', file)">
                    转存
                  </v-btn>
                  <v-icon
                    v-if="file.can_enter"
                    icon="mdi-chevron-right"
                    size="18"
                    color="medium-emphasis"
                    class="ml-1 cursor-pointer"
                    @click.stop="$emit('open-folder', file)" />
                </template>
              </v-list-item>
              <v-divider v-if="index < items.length - 1" />
            </template>
          </v-list>
        </div>
      </v-card-text>

      <v-divider />
      <v-card-actions class="pa-3 justify-space-between">
        <span class="text-caption text-medium-emphasis">支持下钻浏览各层级子文件夹</span>
        <div class="d-flex ga-2">
          <v-btn variant="tonal" size="small" @click="model = false">关闭</v-btn>
          <v-btn
            v-if="canTransfer"
            color="primary"
            variant="flat"
            size="small"
            prepend-icon="mdi-download"
            @click="$emit('quick-transfer')">
            一键转存
          </v-btn>
        </div>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed} from "vue";
import {formatBytes, previewFileExtension, previewFileIcon, previewFileStem} from "../../composables/resourceUtils";

const props = defineProps({
  modelValue: Boolean,
  loading: Boolean,
  error: {type: String, default: ""},
  items: {type: Array, default: () => []},
  selectedItems: {type: Array, default: () => []},
  meta: {type: Object, default: () => ({})},
  breadcrumbs: {type: Array, default: () => []},
  shareUrl: {type: String, default: ""},
  allSelected: Boolean,
  canTransfer: Boolean,
  isSelected: {type: Function, required: true},
});
const emit = defineEmits([
  "update:modelValue",
  "copy",
  "open-breadcrumb",
  "open-folder",
  "toggle-all",
  "toggle-item",
  "single-transfer",
  "batch-transfer",
  "quick-transfer",
]);
const model = computed({get: () => props.modelValue, set: (value) => emit("update:modelValue", value)});
</script>

<style scoped>
.source-preview-card {
  max-height: min(80vh, 740px);
  border-radius: 8px;
  overflow: hidden;
}

.source-preview-header {
  min-height: 56px;
}

.source-preview-meta {
  padding: 4px 0;
}

.source-preview-batch-bar {
  background: rgba(var(--v-theme-primary), 0.05);
  border: 1px solid rgba(var(--v-theme-primary), 0.16);
  border-radius: 8px;
}

.source-preview-breadcrumbs {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px;
  padding: 4px 6px;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.08);
}

.breadcrumb-separator {
  color: rgba(var(--v-theme-on-surface), 0.4);
  margin: 0 3px;
  font-size: 0.75rem;
}

.breadcrumb-btn {
  text-transform: none !important;
  padding: 0 6px !important;
  font-size: 0.75rem !important;
}

.source-preview-list-scroll {
  min-height: 0;
  max-height: min(50vh, 440px);
  overflow-y: auto;
}

.source-preview-file {
  min-height: 44px;
  border-radius: 6px;
  transition: background 0.15s ease;
}

.source-preview-file--directory {
  cursor: pointer;
}

.source-preview-file--directory:hover {
  background: rgba(var(--v-theme-primary), 0.08);
}

.source-preview-file-name {
  font-size: 0.85rem;
  line-height: 1.4;
  word-break: break-all;
}

.preview-batch-btn {
  border-radius: 6px !important;
  height: 28px !important;
  padding: 0 12px !important;
  font-size: 0.78rem !important;
  box-shadow: 0 2px 6px rgba(var(--v-theme-primary), 0.28) !important;
}

.preview-item-transfer-btn {
  border-radius: 6px !important;
  height: 26px !important;
  padding: 0 10px !important;
  font-size: 0.74rem !important;
  letter-spacing: 0;
}
</style>
