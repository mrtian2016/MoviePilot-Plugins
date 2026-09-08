<template>
  <v-expand-transition>
    <div v-show="visible" class="discover-filter-bar px-3 py-2">
      <div v-for="group in groups" v-show="group.options.length > 1" :key="group.key" class="filter-row">
        <v-label class="filter-label">{{ group.label }}</v-label>
        <v-chip-group
          :model-value="modelValue[group.key]"
          mandatory
          class="filter-chip-group"
          @update:model-value="update(group.key, $event)">
          <v-chip
            v-for="option in group.options"
            :key="optionValue(option)"
            :value="optionValue(option)"
            :color="modelValue[group.key] === optionValue(option) ? 'primary' : ''"
            filter
            size="small">
            {{ optionTitle(option) }}
          </v-chip>
        </v-chip-group>
      </div>
      <div class="filter-row">
        <v-label class="filter-label">排序</v-label>
        <v-chip-group
          :model-value="modelValue.sort"
          mandatory
          class="filter-chip-group"
          @update:model-value="update('sort', $event)">
          <v-chip
            v-for="option in options.sort || []"
            :key="option.value"
            :value="option.value"
            :color="modelValue.sort === option.value ? 'primary' : ''"
            filter
            size="small">
            {{ option.title }}
          </v-chip>
        </v-chip-group>
        <v-spacer />
        <v-btn
          v-if="activeCount"
          variant="text"
          color="primary"
          size="small"
          prepend-icon="mdi-refresh"
          @click="$emit('reset')">
          重置筛选
        </v-btn>
      </div>
    </div>
  </v-expand-transition>
</template>

<script setup>
import {computed} from "vue";

const props = defineProps({
  visible: Boolean,
  modelValue: {type: Object, required: true},
  options: {type: Object, required: true},
  activeCount: {type: Number, default: 0},
});
const emit = defineEmits(["update:modelValue", "reset"]);
const groups = computed(() => [
  {key: "status", label: "收录状态", options: props.options.status || []},
  {key: "category", label: "类别", options: props.options.category || []},
  {key: "type", label: "风格", options: props.options.type || []},
  {key: "region", label: "地区", options: props.options.region || []},
  {key: "language", label: "语言", options: props.options.language || []},
  {key: "year", label: "年份", options: props.options.year || []},
  {key: "rating", label: "评分", options: props.options.rating || []},
]);
const optionValue = (option) => (typeof option === "object" ? option.value : option);
const optionTitle = (option) => (typeof option === "object" ? option.title : option);

function update(key, value) {
  emit("update:modelValue", {...props.modelValue, [key]: value});
}
</script>

<style scoped>
.discover-filter-bar {
  margin-bottom: 4px;
  border: 1px solid rgba(var(--v-border-color), 0.08);
  border-radius: 8px;
  background: rgb(var(--v-theme-surface));
}

.filter-row {
  display: flex;
  align-items: center;
  min-height: 34px;
}

.filter-label {
  min-width: 76px;
  color: rgba(var(--v-theme-on-surface), 0.75);
  font-size: 0.85rem;
  font-weight: 500;
}

.filter-chip-group {
  min-width: 0;
  overflow-x: auto;
  flex-wrap: nowrap;
}

:global(html[data-theme="glass"]) .discover-filter-bar,
:global(html[data-theme="transparent"]) .discover-filter-bar {
  background: var(--glass-surface-soft, rgba(var(--v-theme-surface), 0.6));
  border-color: var(--glass-border, rgba(var(--v-border-color), 0.08));
  backdrop-filter: var(--glass-surface-backdrop-filter, blur(12px));
}
</style>
