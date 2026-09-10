<template>
  <div class="region-media-map-field">
    <div v-if="field.hint" class="text-caption text-medium-emphasis mb-2">{{ field.hint }}</div>
    <v-text-field
      :model-value="summary"
      :label="field.label"
      readonly
      density="compact"
      variant="outlined"
      hide-details="auto"
      append-inner-icon="mdi-chevron-down"
      @click="dialogVisible = true" />

    <v-dialog v-model="dialogVisible" max-width="680">
      <v-card class="region-media-dialog">
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-view-grid-plus-outline" color="primary" />
          <span>{{ field.label }}</span>
        </v-card-title>
        <v-card-text>
          <div class="text-body-2 text-medium-emphasis mb-3">按平台或地区分别选择要监听的媒体类型（可各不相同）</div>
          <v-autocomplete
            :model-value="selectedRegions"
            :items="field.items || []"
            label="添加平台/地区"
            multiple
            item-title="title"
            item-value="value"
            chips
            closable-chips
            density="compact"
            variant="outlined"
            hide-details="auto"
            @update:model-value="setSelectedRegions" />

          <div v-for="region in selectedRegions" :key="region" class="region-media-dialog__item">
            <div class="d-flex align-center justify-space-between mb-2">
              <v-switch
                :model-value="true"
                color="primary"
                density="compact"
                hide-details
                :label="regionLabel(region)"
                @update:model-value="removeRegion(region)" />
              <v-btn
                icon="mdi-close"
                variant="text"
                size="small"
                color="error"
                title="移除平台/地区"
                @click="removeRegion(region)" />
            </div>
            <div class="d-flex flex-wrap ga-2">
              <v-btn
                v-for="column in field.columns || []"
                :key="column.value"
                size="small"
                rounded="pill"
                :variant="values(region).includes(column.value) ? 'tonal' : 'outlined'"
                :color="values(region).includes(column.value) ? 'primary' : undefined"
                @click="toggleValue(region, column.value)">
                {{ column.title }}
              </v-btn>
            </div>
          </div>
        </v-card-text>
        <v-card-actions>
          <v-btn variant="text" color="primary" @click="updateMap({})">清空</v-btn>
          <v-spacer />
          <v-btn color="primary" variant="flat" @click="dialogVisible = false">完成</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup>
import {computed, ref} from "vue";

const props = defineProps({
  modelValue: {type: Object, default: () => ({})},
  field: {type: Object, required: true},
})
const emit = defineEmits(["update:modelValue"]);
const dialogVisible = ref(false);

const normalizedMap = computed(() => {
  const value = props.modelValue;
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
})
const selectedRegions = computed(() => Object.keys(normalizedMap.value));
const summary = computed(() => {
  if (!selectedRegions.value.length) return "未选择";
  return selectedRegions.value.map((region) => `${regionLabel(region)} (${values(region).length})`).join("、");
})

function updateMap(value) {
  emit("update:modelValue", value);
}

function values(region) {
  const value = normalizedMap.value[region];
  return Array.isArray(value) ? value : [];
}

function regionLabel(region) {
  return props.field.items?.find((item) => String(item.value) === String(region))?.title || region;
}

function availableValues(region) {
  const normalizedRegion = String(region);
  return (props.field.columns || [])
    .filter((column) => !Array.isArray(column.rows) || column.rows.some((row) => String(row) === normalizedRegion))
    .map((column) => column.value)
}

function setSelectedRegions(regions) {
  const selected = new Set((Array.isArray(regions) ? regions : []).map(String));
  const next = {};
  for (const region of selected) {
    next[region] = Object.prototype.hasOwnProperty.call(normalizedMap.value, region)
      ? [...values(region)]
      : availableValues(region)
  }
  updateMap(next);
}

function removeRegion(region) {
  const next = {...normalizedMap.value};
  delete next[region];
  updateMap(next);
}

function toggleValue(region, value) {
  const selected = values(region);
  const nextValues = selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value];
  if (!nextValues.length) {
    removeRegion(region);
    return;
  }
  updateMap({...normalizedMap.value, [region]: nextValues});
}
</script>

<style scoped>
.region-media-map-field {
  min-width: 0;
}

.region-media-dialog__item {
  margin-top: 12px;
  padding: 12px;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 10px;
}
</style>
