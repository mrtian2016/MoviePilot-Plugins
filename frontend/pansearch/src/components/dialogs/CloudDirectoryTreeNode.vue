<template>
  <div class="tree-node">
    <div
      class="tree-row"
      :class="{ selected: node.path === selectedPath }"
      :style="{ paddingLeft: `${depth * 18 + 4}px` }"
      @click="$emit('select', node)"
      @dblclick.stop="$emit('toggle', node)">
      <v-btn
        :icon="node.expanded ? 'mdi-chevron-down' : 'mdi-chevron-right'"
        variant="text"
        size="x-small"
        :disabled="disabled || node.loading"
        :loading="node.loading"
        @click.stop="$emit('toggle', node)"
        @dblclick.stop />
      <v-icon
        :icon="node.expanded ? 'mdi-folder-open' : 'mdi-folder'"
        size="small"
        color="amber-darken-2"
        class="mx-1" />
      <span class="tree-label">{{ node.name }}</span>
    </div>
    <template v-if="node.expanded">
      <CloudDirectoryTreeNode
        v-for="child in node.children"
        :key="child.id || child.path"
        :node="child"
        :selected-path="selectedPath"
        :depth="depth + 1"
        :disabled="disabled"
        @select="$emit('select', $event)"
        @toggle="$emit('toggle', $event)" />
      <div
        v-if="node.loaded && !node.loading && !node.children.length"
        class="tree-empty text-caption text-medium-emphasis"
        :style="{ paddingLeft: `${(depth + 1) * 18 + 34}px` }">
        无子文件夹
      </div>
    </template>
  </div>
</template>

<script setup>
defineProps({
  node: {type: Object, required: true},
  selectedPath: {type: String, default: "/"},
  depth: {type: Number, default: 0},
  disabled: {type: Boolean, default: false},
})
defineEmits(["select", "toggle"]);
</script>

<style scoped>
.tree-row {
  display: flex;
  min-height: 36px;
  align-items: center;
  border-radius: 4px;
  cursor: pointer;
}

.tree-row:hover {
  background: rgba(var(--v-theme-on-surface), 0.05);
}

.tree-row.selected {
  color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.12);
}

.tree-label {
  min-width: 0;
  padding-right: 8px;
  overflow-wrap: anywhere;
}

.tree-empty {
  min-height: 28px;
  line-height: 28px;
}
</style>
