<template>
  <div class="tabs-container">
    <v-tabs
      :model-value="searchMode ? undefined : activeTab"
      color="primary"
      density="compact"
      align-tabs="start"
      show-arrows
      class="platform-native-tabs"
      @update:model-value="$emit('change', $event)">
      <v-tab v-for="tab in tabs" :key="tab.value" :value="tab.value" class="platform-native-tab" :disabled="searchMode">
        <v-icon v-if="tab.icon" :icon="tab.icon" size="18" class="mr-1.5 tab-icon" />
        <span class="tab-title">{{ tab.title }}</span>
      </v-tab>
    </v-tabs>

    <v-badge
      :content="filterCount"
      :model-value="filterCount > 0"
      color="primary"
      class="filter-badge-btn flex-shrink-0">
      <v-btn
        icon="mdi-filter-variant"
        size="small"
        :variant="filterVisible ? 'tonal' : 'text'"
        :color="filterVisible || filterCount ? 'primary' : undefined"
        title="探索筛选"
        aria-label="探索筛选"
        @click="$emit('toggle-filter')" />
    </v-badge>
  </div>
</template>

<script setup>
defineProps({
  tabs: {type: Array, default: () => []},
  activeTab: {type: String, default: ""},
  searchMode: Boolean,
  filterVisible: Boolean,
  filterCount: {type: Number, default: 0},
});
defineEmits(["change", "toggle-filter"]);
</script>

<style scoped>
.tabs-container {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0 0 12px 0;
  width: 100%;
  min-height: 44px;
}

.platform-native-tabs {
  flex: 1 1 auto;
  min-width: 0;
  height: 44px !important;
  background: transparent !important;
}

.platform-native-tabs :deep(.v-slide-group__content) {
  align-items: center;
}

.platform-native-tab {
  font-size: 0.94rem !important;
  font-weight: 500 !important;
  letter-spacing: 0.2px !important;
  text-transform: none !important;
  padding: 0 16px !important;
  min-width: unset !important;
  height: 42px !important;
  transition: color 0.2s ease,
  font-weight 0.2s ease !important;
  color: rgba(var(--v-theme-on-surface), 0.72) !important;
}

.platform-native-tab.v-tab--selected {
  color: rgb(var(--v-theme-primary)) !important;
  font-weight: 700 !important;
}

.tab-icon {
  opacity: 0.85;
}

.filter-badge-btn {
  margin-left: 6px;
}

@media (max-width: 600px) {
  .tabs-container {
    margin: 0 0 8px 0;
    min-height: 38px;
    gap: 4px;
  }

  .platform-native-tabs {
    height: 38px !important;
  }

  .platform-native-tab {
    padding: 0 10px !important;
    font-size: 0.84rem !important;
    height: 36px !important;
  }

  .tab-icon {
    font-size: 16px !important;
    margin-right: 4px !important;
  }
}
</style>
