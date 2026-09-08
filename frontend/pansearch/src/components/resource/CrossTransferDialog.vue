<template>
  <v-dialog v-model="model" max-width="460px">
    <v-card class="rounded-xl overflow-hidden border">
      <v-card-title class="px-5 pt-4 pb-2 font-weight-bold d-flex align-center">
        <v-icon icon="mdi-swap-horizontal-bold" color="deep-purple" class="mr-2" />
        选择跨盘目标网盘
      </v-card-title>
      <v-card-text class="px-5 py-3">
        <div class="mb-3 text-caption text-medium-emphasis">
          源网盘
          <strong>{{ sourceName }}</strong>
        </div>
        <v-select
          :model-value="target"
          label="目标网盘"
          :items="drives"
          item-title="name"
          item-value="key"
          variant="outlined"
          density="comfortable"
          hide-details
          class="mb-3"
          @update:model-value="$emit('update:target', $event)">
          <template #prepend-inner>
            <v-icon :icon="icon" color="primary" />
          </template>
        </v-select>
        <div class="text-caption text-medium-emphasis">请选择已配置账号的目标网盘。</div>
      </v-card-text>
      <v-card-actions class="px-5 pb-4 pt-1 justify-end ga-2">
        <v-btn variant="text" size="small" @click="model = false">取消</v-btn>
        <v-btn color="deep-purple" variant="elevated" size="small" :loading="loading" @click="$emit('confirm')">
          确认跨盘转存
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
<script setup>
import {computed} from "vue";

const props = defineProps({
  modelValue: Boolean,
  sourceName: {type: String, default: ""},
  target: {type: String, default: ""},
  drives: {type: Array, default: () => []},
  icon: {type: String, default: "mdi-cloud"},
  loading: Boolean,
});
const emit = defineEmits(["update:modelValue", "update:target", "confirm"]);
const model = computed({get: () => props.modelValue, set: (value) => emit("update:modelValue", value)});
</script>
