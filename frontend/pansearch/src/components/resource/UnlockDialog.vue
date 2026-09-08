<template>
  <v-dialog v-model="model" max-width="420px" class="unlock-confirm-dialog">
    <v-card class="rounded-xl overflow-hidden">
      <v-card-title class="px-5 pt-4 pb-2 font-weight-bold d-flex align-center ga-2">
        <v-icon icon="mdi-lock-open-variant-outline" color="warning" />
        <span>{{ Number(item?.unlock_points || 0) > 0 ? "积分解锁资源" : "获取免费资源链接" }}</span>
      </v-card-title>
      <v-card-text class="px-5 py-3">
        <div class="text-body-2 mb-2">
          <template v-if="Number(item?.unlock_points || 0) > 0">
            确认消耗
            <strong class="text-warning">{{ Number(item?.unlock_points || 0) }}</strong>
            积分解锁该资源？
          </template>
          <template v-else>该资源免费，确认获取资源链接？</template>
        </div>
        <div class="text-caption text-medium-emphasis text-truncate bg-surface-variant px-3 py-2 rounded-lg">
          {{ item?.title }}
        </div>
        <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mt-3">{{ error }}</v-alert>
      </v-card-text>
      <v-card-actions class="px-5 pb-4">
        <v-spacer />
        <v-btn variant="text" size="small" @click="model = false">取消</v-btn>
        <v-btn color="warning" variant="flat" size="small" :loading="loading" @click="$emit('confirm')">
          {{ Number(item?.unlock_points || 0) > 0 ? "确认解锁" : "获取链接" }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
<script setup>
import {computed} from "vue";

const props = defineProps({
  modelValue: Boolean,
  item: {type: Object, default: null},
  error: {type: String, default: ""},
  loading: Boolean,
});
const emit = defineEmits(["update:modelValue", "confirm"]);
const model = computed({get: () => props.modelValue, set: (value) => emit("update:modelValue", value)});
</script>
