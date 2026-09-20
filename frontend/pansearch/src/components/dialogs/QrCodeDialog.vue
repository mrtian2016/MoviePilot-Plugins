<template>
  <v-dialog :model-value="modelValue" max-width="450" @update:model-value="emit('update:modelValue', $event)">
    <v-card>
      <v-card-title class="text-subtitle-1 d-flex align-center px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-qrcode" class="mr-2" color="primary" size="small" />
        <span>{{ providerName }}扫码登录</span>
      </v-card-title>

      <v-card-text class="text-center py-4">
        <v-alert
          v-if="error"
          type="error"
          density="compact"
          class="mb-3 mx-3"
          variant="tonal"
          closable
          @click:close="error = false">
          {{ statusText }}
        </v-alert>

        <div v-if="loading" class="d-flex flex-column align-center py-3">
          <v-progress-circular indeterminate color="primary" class="mb-3" />
          <div>正在获取二维码...</div>
        </div>

        <div v-else-if="qrCode" class="d-flex flex-column align-center">
          <template v-if="provider === '115'">
            <div class="mb-2 font-weight-medium">扫码渠道</div>
            <v-chip-group v-model="channel" class="channel-list mb-3" mandatory selected-class="text-primary">
              <v-chip
                v-for="item in channels"
                :key="item.value"
                :value="item.value"
                variant="outlined"
                color="primary"
                size="small">
                {{ item.title }}
              </v-chip>
            </v-chip-group>
          </template>
          <v-card flat class="border pa-2 mb-2 qr-card">
            <img :src="qrCode" :alt="`${providerName}登录二维码`" />
          </v-card>
          <div v-if="session?.user_code" class="d-flex align-center ga-2 text-body-2 mb-2">
            <span class="text-medium-emphasis">授权码</span>
            <v-code class="px-2 py-1">{{ session.user_code }}</v-code>
          </div>
          <div class="text-body-2 text-medium-emphasis mb-1">
            {{ scanHint }}
          </div>
          <div class="text-subtitle-2 font-weight-medium text-primary">
            {{ statusText }}
          </div>
        </div>

        <div v-else class="d-flex flex-column align-center py-3">
          <v-icon icon="mdi-qrcode-off" size="64" color="grey" class="mb-3" />
          <div class="text-subtitle-1">二维码获取失败</div>
          <div class="text-body-2 text-medium-emphasis">请点击刷新按钮重试</div>
        </div>
      </v-card-text>

      <v-divider />
      <v-card-actions class="px-3 py-2">
        <v-btn color="grey" variant="text" size="small" prepend-icon="mdi-close" @click="close">关闭</v-btn>
        <v-spacer />
        <v-btn
          color="primary"
          variant="text"
          size="small"
          prepend-icon="mdi-refresh"
          :disabled="loading"
          @click="loadQrCode">
          刷新二维码
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";

const props = defineProps({
  modelValue: Boolean,
  api: { type: [Object, Function], required: true },
  provider: { type: String, default: "115" },
})
const emit = defineEmits(["update:modelValue", "success"])
const pluginId = "PanSearch"
const providerMeta = {
  115: { name: "115网盘", hint: "请使用115客户端扫描二维码" },
  123: { name: "123网盘", hint: "请使用 123 云盘 App 扫描二维码" },
  quark: { name: "夸克网盘", hint: "请使用夸克 App 扫描二维码" },
  guangya: { name: "光鸭网盘", hint: "请使用光鸭网盘完成扫码授权" },
  alipan: { name: "阿里云盘", hint: "请使用阿里云盘 App 扫描二维码" },
  tianyi: { name: "天翼云盘", hint: "请使用小翼管家、支付宝或天翼云盘 App 扫描二维码" },
}
const channels = [
  { title: "网页", value: "web" },
  { title: "TV", value: "tv" },
  { title: "苹果", value: "115ios" },
  { title: "安卓", value: "115android" },
  { title: "ipad", value: "115ipad" },
  { title: "Windows", value: "os_windows" },
  { title: "MacOS", value: "os_mac" },
  { title: "Linux", value: "os_linux" },
  { title: "微信", value: "wechatmini" },
  { title: "支付宝", value: "alipaymini" },
  { title: "鸿蒙", value: "harmony" },
]
const providerName = computed(() => providerMeta[props.provider]?.name || "网盘")
const scanHint = computed(() => providerMeta[props.provider]?.hint || "请扫描二维码完成登录")
const channel = ref("alipaymini")
const loading = ref(false)
const qrCode = ref("")
const statusText = ref("")
const error = ref(false)
const session = ref(null)
let timer = null
let failures = 0
let pollInterval = 3000

function unwrapResponse(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) {
    return raw.data
  }
  return raw || {}
}

function stopPolling() {
  if (timer) clearTimeout(timer)
  timer = null
}

function close() {
  stopPolling()
  session.value = null
  emit("update:modelValue", false)
}

function buildStatusQuery() {
  const query = new URLSearchParams({ provider: props.provider })
  if (props.provider === "115") {
    query.set("uid", session.value.uid || "")
    query.set("time", session.value.time || "")
    query.set("sign", session.value.sign || "")
    query.set("client_type", session.value.client_type || channel.value)
  } else if (props.provider === "123") {
    query.set("uni_id", session.value.uni_id || "")
  } else if (props.provider === "quark") {
    query.set("qr_token", session.value.qr_token || "")
  } else if (props.provider === "guangya") {
    query.set("device_code", session.value.device_code || "")
    query.set("device_id", session.value.device_id || "")
    query.set("client_id", session.value.client_id || "")
  } else if (props.provider === "alipan") {
    query.set("t", session.value.t || "")
    query.set("ck", session.value.ck || "")
  } else if (props.provider === "tianyi") {
    query.set("uuid", session.value.uuid || "")
    query.set("encryuuid", session.value.encryuuid || "")
    query.set("req_id", session.value.req_id || "")
    query.set("lt", session.value.lt || "")
    query.set("param_id", session.value.param_id || "")
  }
  return query
}

function schedulePolling(delay = pollInterval) {
  if (!session.value || document.visibilityState === "hidden") return
  stopPolling()
  timer = setTimeout(
    async () => {
      timer = null
      await checkStatus()
      if (session.value) {
        const retryDelay = failures ? Math.min(pollInterval * 2 ** failures, 15000) : pollInterval
        schedulePolling(retryDelay)
      }
    },
    Math.max(0, delay),
  )
}

function handleVisibilityChange() {
  if (document.visibilityState === "hidden") {
    stopPolling()
  } else if (props.modelValue && session.value) {
    schedulePolling(0)
  }
}

async function checkStatus() {
  if (!session.value) return
  try {
    const result = unwrapResponse(
      await props.api.post(`plugin/${pluginId}/qrcode/check`, Object.fromEntries(buildStatusQuery())),
    )
    if (result.success === false) {
      throw new Error(result.message || "检查登录状态失败")
    }
    failures = 0
    error.value = false
    statusText.value = result.message || "等待扫码"
    if (result.status === "success") {
      stopPolling()
      session.value = null
      emit("success", {
        provider: result.provider || props.provider,
        credentials: result.credentials || {},
      })
      statusText.value = "登录成功，凭证已写入配置"
      setTimeout(close, 800)
    } else if (["expired", "cancelled"].includes(result.status)) {
      stopPolling()
      session.value = null
      error.value = true
    }
  } catch (statusError) {
    failures += 1
    if (failures >= 5) {
      stopPolling()
      session.value = null
      error.value = true
      statusText.value = `${statusError.message || "检查登录状态失败"}，请刷新二维码重试`
    } else {
      statusText.value = `登录状态检查暂时失败，正在重试 (${failures}/5)`
    }
  }
}

async function loadQrCode() {
  stopPolling()
  session.value = null
  loading.value = true
  qrCode.value = ""
  error.value = false
  failures = 0
  statusText.value = "正在获取二维码..."
  try {
    const query = new URLSearchParams({
      provider: props.provider,
      client_type: channel.value,
    })
    const result = unwrapResponse(await props.api.get(`plugin/${pluginId}/qrcode?${query}`))
    if (result.success === false) {
      throw new Error(result.message || "获取二维码失败")
    }
    session.value = result.data?.data || result.data || result
    qrCode.value = session.value.qrcode || ""
    if (!qrCode.value) throw new Error("接口未返回二维码")
    pollInterval = Math.max(2, Number(session.value.interval || 3)) * 1000
    const channelName =
      session.value.channel_name || channels.find((item) => item.value === channel.value)?.title || "115客户端"
    statusText.value = props.provider === "115" ? `等待${channelName}扫码` : "等待扫码"
    schedulePolling()
  } catch (loadError) {
    error.value = true
    statusText.value = loadError.message || "获取二维码失败"
  } finally {
    loading.value = false
  }
}

watch([() => props.modelValue, () => props.provider], ([visible]) => (visible ? loadQrCode() : stopPolling()))
watch(channel, () => props.modelValue && props.provider === "115" && loadQrCode())
onMounted(() => document.addEventListener("visibilitychange", handleVisibilityChange))
onBeforeUnmount(() => {
  document.removeEventListener("visibilitychange", handleVisibilityChange)
  stopPolling()
})
</script>

<style scoped>
.channel-list {
  max-width: 100%;
  overflow-x: auto;
}

.qr-card img {
  display: block;
  width: min(220px, 70vw);
  aspect-ratio: 1;
  object-fit: contain;
}
</style>
