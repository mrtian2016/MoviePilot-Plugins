<template>
  <v-dialog v-model="visible" :max-width="providerItems.length > 1 ? 760 : 560">
    <v-card class="directory-card">
      <v-card-title class="directory-title px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-folder-network" color="primary" class="mr-2" />
        <span>{{ title }}</span>
      </v-card-title>
      <v-card-text class="px-3 py-2">
        <div class="directory-browser" :class="{ 'has-providers': providerItems.length > 1 }">
          <v-list v-if="providerItems.length > 1" nav density="compact" class="provider-list border rounded">
            <v-list-subheader>网盘</v-list-subheader>
            <v-list-item
              v-for="item in providerItems"
              :key="item.value"
              :active="selectedProvider === item.value"
              color="primary"
              :disabled="loading"
              @click="selectProvider(item.value)">
              <template #prepend>
                <v-icon icon="mdi-cloud-outline" size="small" />
              </template>
              <v-list-item-title>{{ item.title }}</v-list-item-title>
              <v-list-item-subtitle>
                {{ item.value === (targetProvider || provider) ? "目标网盘" : "跨盘转存" }}
              </v-list-item-subtitle>
            </v-list-item>
          </v-list>
          <div class="directory-pane">
            <v-text-field
              v-model="currentPath"
              label="当前路径"
              variant="outlined"
              density="compact"
              class="mb-2"
              hide-details
              :disabled="loading"
              @keyup.enter="loadDirectories(currentPath)" />
            <div class="directory-actions mb-2">
              <v-btn
                v-if="allowCreate"
                prepend-icon="mdi-folder-plus"
                variant="tonal"
                size="small"
                :disabled="loading || createLoading"
                @click="openCreateDirectoryDialog">
                新建文件夹
              </v-btn>
              <v-btn
                prepend-icon="mdi-refresh"
                variant="text"
                size="small"
                :disabled="loading || createLoading"
                @click="refreshDirectories">
                刷新
              </v-btn>
            </div>
            <div v-if="loading && !treeRoot.loaded" class="directory-loading">
              <v-progress-circular indeterminate color="primary" />
            </div>
            <div v-else class="directory-list border rounded">
              <CloudDirectoryTreeNode
                :node="treeRoot"
                :selected-path="currentPath"
                :disabled="loading || createLoading"
                @select="selectTreeNode"
                @toggle="toggleTreeNode" />
            </div>
          </div>
        </div>
        <v-alert v-if="errorMessage" type="error" density="compact" variant="tonal" class="mt-2 text-caption">
          {{ errorMessage }}
        </v-alert>
      </v-card-text>
      <v-card-actions class="px-3 py-2">
        <v-spacer />
        <v-btn color="primary" variant="text" size="small" :disabled="!currentPath || loading" @click="selectDirectory">
          选择当前目录
        </v-btn>
        <v-btn color="grey" variant="text" size="small" @click="visible = false">取消</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="createDirectoryVisible" max-width="420" persistent>
    <v-card rounded="lg">
      <v-card-title class="text-subtitle-1">新建文件夹</v-card-title>
      <v-card-text>
        <v-text-field
          v-model="newDirectoryName"
          label="文件夹名称"
          placeholder="请输入文件夹名称"
          variant="outlined"
          density="compact"
          autofocus
          :disabled="createLoading"
          :error-messages="createDirectoryError"
          @keyup.enter="createDirectory" />
      </v-card-text>
      <v-card-actions class="px-4 pb-3">
        <v-spacer />
        <v-btn variant="text" :disabled="createLoading" @click="closeCreateDirectoryDialog">取消</v-btn>
        <v-btn color="primary" :loading="createLoading" :disabled="!newDirectoryName.trim()" @click="createDirectory">
          创建
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, ref, watch} from "vue";
import CloudDirectoryTreeNode from "./CloudDirectoryTreeNode.vue";

const props = defineProps({
  modelValue: {type: Boolean, default: false},
  api: {type: [Object, Function], required: true},
  provider: {type: String, default: ""},
  targetProvider: {type: String, default: ""},
  initialPath: {type: String, default: "/"},
  pluginId: {type: String, default: "PanSearch"},
  title: {type: String, default: "选择网盘转存路径"},
  allowCreate: {type: Boolean, default: true},
  providers: {type: Array, default: () => []},
})
const emit = defineEmits(["update:modelValue", "select"]);
const currentPath = ref("/");
const selectedProvider = ref("");
const loading = ref(false);
const errorMessage = ref("");
const createDirectoryVisible = ref(false);
const newDirectoryName = ref("");
const createDirectoryError = ref("");
const createLoading = ref(false);

function directoryNode(source = {}, fallbackPath = "/") {
  return {
    id: String(source.id || source.path || fallbackPath),
    name: String(source.name || (fallbackPath === "/" ? "根目录" : fallbackPath.split("/").pop())),
    path: String(source.path || fallbackPath),
    children: [],
    expanded: fallbackPath === "/",
    loaded: false,
    loading: false,
  }
}

const treeRoot = ref(directoryNode({}, "/"));

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit("update:modelValue", value),
})
const providerItems = computed(() => {
  if (Array.isArray(props.providers) && props.providers.length) return props.providers;
  return props.provider ? [{title: props.provider, value: props.provider}] : [];
})

function unwrap(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) return raw.data;
  return raw || {};
}

function normalizePath(path) {
  const parts = String(path || "/")
    .replace(/\\/g, "/")
    .split("/")
    .filter(Boolean);
  return parts.length ? `/${parts.join("/")}` : "/";
}

function findTreeNode(path, node = treeRoot.value) {
  const normalized = normalizePath(path);
  if (node.path === normalized) return node;
  for (const child of node.children) {
    const matched = findTreeNode(normalized, child);
    if (matched) return matched;
  }
  return null;
}

function ensureTreePath(path) {
  const normalized = normalizePath(path);
  let node = treeRoot.value;
  let current = "";
  for (const part of normalized.split("/").filter(Boolean)) {
    current = `${current}/${part}`;
    let child = node.children.find((item) => item.path === current);
    if (!child) {
      child = directoryNode({name: part, path: current}, current);
      node.children.push(child);
    }
    node.expanded = true;
    node = child;
  }
  return node;
}

async function loadTreeNode(node, force = false) {
  if (!node || node.loading || (!force && node.loaded)) return;
  loading.value = true;
  node.loading = true;
  errorMessage.value = "";
  try {
    const query = new URLSearchParams({
      path: node.path,
      provider: selectedProvider.value || props.provider || "",
    })
    if (force) query.set("refresh", "true");
    const response = unwrap(await props.api.get(`plugin/${props.pluginId}/cloud/directories?${query}`));
    if (response.success === false) throw new Error(response.message || "读取目录失败");
    const data = response.data?.data || response.data || response;
    const previous = new Map(node.children.map((item) => [item.path, item]));
    node.children = (Array.isArray(data.directories) ? data.directories : [])
      .sort((left, right) =>
        String(left.name || "").localeCompare(String(right.name || ""), undefined, {
          numeric: true,
          sensitivity: "base",
        }),
      )
      .map((item) => previous.get(item.path) || directoryNode(item, item.path))
    node.loaded = true;
  } catch (error) {
    errorMessage.value = error.message || String(error);
  } finally {
    node.loading = false;
    loading.value = false;
  }
}

async function loadDirectories(path, force = false) {
  const normalized = normalizePath(path);
  if (!treeRoot.value.loaded) await loadTreeNode(treeRoot.value);
  const node = findTreeNode(normalized) || ensureTreePath(normalized);
  currentPath.value = normalized;
  node.expanded = true;
  await loadTreeNode(node, force);
}

function refreshDirectories() {
  const node = findTreeNode(currentPath.value) || treeRoot.value;
  loadTreeNode(node, true);
}

function selectTreeNode(node) {
  currentPath.value = normalizePath(node?.path);
}

async function toggleTreeNode(node) {
  if (!node || node.loading) return;
  node.expanded = !node.expanded;
  if (node.expanded) await loadTreeNode(node);
}

function selectProvider(provider) {
  const value = String(provider || "").trim();
  if (!value || value === selectedProvider.value || loading.value) return;
  selectedProvider.value = value;
  currentPath.value = "/";
  treeRoot.value = directoryNode({}, "/");
  loadDirectories("/");
}

function openCreateDirectoryDialog() {
  newDirectoryName.value = "";
  createDirectoryError.value = "";
  createDirectoryVisible.value = true;
}

function closeCreateDirectoryDialog() {
  if (createLoading.value) return;
  createDirectoryVisible.value = false;
}

async function createDirectory() {
  if (createLoading.value) return;
  const folderName = newDirectoryName.value.trim();
  if (!folderName) {
    createDirectoryError.value = "文件夹名称不能为空";
    return;
  }
  createLoading.value = true;
  createDirectoryError.value = "";
  const directoryPath = currentPath.value || "/";
  try {
    const response = unwrap(
      await props.api.post(`plugin/${props.pluginId}/cloud/directories/create`, {
        path: directoryPath,
        name: folderName,
        provider: selectedProvider.value || props.provider || "",
      }),
    )
    if (response.success === false) throw new Error(response.message || "创建文件夹失败");
    createDirectoryVisible.value = false;
    newDirectoryName.value = "";
    await loadDirectories(directoryPath, true);
  } catch (error) {
    createDirectoryError.value = error.message || String(error);
  } finally {
    createLoading.value = false;
  }
}

function selectDirectory() {
  emit("select", currentPath.value || "/", selectedProvider.value || props.provider || "");
}

watch(
  () => props.modelValue,
  (value) => {
    if (value) {
      selectedProvider.value = String(props.provider || providerItems.value[0]?.value || "").trim();
      currentPath.value = String(props.initialPath || "/").trim() || "/";
      treeRoot.value = directoryNode({}, "/");
      loadDirectories(currentPath.value);
    } else {
      createDirectoryVisible.value = false;
      newDirectoryName.value = "";
      createDirectoryError.value = "";
    }
  },
  {immediate: true},
)
</script>

<style scoped>
.directory-title {
  display: flex;
  align-items: center;
}

.directory-card {
  min-height: min(560px, 72vh);
}

.directory-list {
  height: min(390px, calc(72vh - 168px));
  overflow-y: auto;
}

.directory-browser {
  display: grid;
  min-width: 0;
}

.directory-browser.has-providers {
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 12px;
}

.provider-list,
.directory-pane {
  min-width: 0;
}

.provider-list {
  height: min(446px, calc(72vh - 112px));
  overflow-y: auto;
}

.directory-actions {
  display: flex;
  gap: 8px;
}

.directory-loading {
  display: flex;
  min-height: min(390px, calc(72vh - 168px));
  align-items: center;
  justify-content: center;
}

@media (max-width: 600px) {
  .directory-browser.has-providers {
    grid-template-columns: 132px minmax(0, 1fr);
    gap: 8px;
  }

  .provider-list :deep(.v-list-item) {
    padding-inline: 8px;
  }
}
</style>
