<template>
  <v-dialog v-model="visible" max-width="480" persistent>
    <v-card rounded="lg">
      <v-card-title class="text-subtitle-1">清理缓存</v-card-title>
      <v-card-text>
        <v-checkbox
          v-for="item in cacheCategories"
          :key="item.value"
          v-model="selected"
          :value="item.value"
          :label="item.title"
          :messages="item.description"
          color="warning"
          density="compact"
          class="cache-option" />
      </v-card-text>
      <v-card-actions>
        <v-btn variant="text" size="small" @click="toggleAll">
          {{ allSelected ? "取消全选" : "全选" }}
        </v-btn>
        <v-spacer />
        <v-btn variant="text" :disabled="loading" @click="visible = false">取消</v-btn>
        <v-btn color="warning" :loading="loading" :disabled="!selected.length" @click="emit('confirm', [...selected])">
          确认清理
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, ref, watch} from "vue";

const cacheCategories = [
  {
    value: "search",
    title: "搜索资源缓存",
    description: "搜索结果、候选详情、文件预览和 Magnet 元数据",
  },
  {
    value: "cloud",
    title: "网盘数据缓存",
    description: "分享信息、文件列表、路径、离线任务和账户容量",
  },
  {
    value: "sync",
    title: "同步计算缓存",
    description: "媒体识别、季目录、播出日历、延期和洗版基线",
  },
  {
    value: "interface",
    title: "页面选项缓存",
    description: "订阅、站点、媒体服务器和配置下拉选项",
  },
  {
    value: "platform",
    title: "概览与智能体缓存",
    description: "概览统计和智能体候选资源",
  },
]

const props = defineProps({
  modelValue: Boolean,
  loading: Boolean,
})
const emit = defineEmits(["update:modelValue", "confirm"])
const selected = ref(["search"])
const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit("update:modelValue", value),
})
const allSelected = computed(() => selected.value.length === cacheCategories.length)

watch(
  () => props.modelValue,
  (value) => {
    if (value) selected.value = ["search"]
  },
)

function toggleAll() {
  selected.value = allSelected.value ? [] : cacheCategories.map((item) => item.value)
}
</script>

<style scoped>
.cache-option {
  margin-bottom: 6px;
}
</style>
