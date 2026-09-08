<template>
  <v-sheet :class="['account-info', { 'account-info--compact': compact }]" rounded="lg" :aria-busy="loading">
    <v-progress-linear v-if="loading" class="account-loading-bar" color="primary" height="2" indeterminate />
    <div v-if="loading && !hasLoadedInfo" class="account-skeleton">
      <v-skeleton-loader type="avatar" />
      <div class="account-skeleton-lines">
        <v-skeleton-loader type="text" />
        <v-skeleton-loader type="text" width="58%" />
      </div>
    </div>
    <div v-else class="account-content">
      <v-avatar :size="compact ? 36 : 42" class="account-avatar">
        <v-img v-if="user.avatar" :src="user.avatar" :alt="user.name" />
        <v-icon v-else icon="mdi-account-circle" :size="compact ? 24 : 28" />
      </v-avatar>
      <div class="account-main">
        <div class="account-heading">
          <span class="text-body-2 font-weight-medium account-name">
            {{ account.connected ? user.name || "未知用户" : "账号未连接" }}
          </span>
          <v-chip v-if="account.connected && user.badge" color="primary" size="x-small" variant="tonal">
            {{ user.badge }}
          </v-chip>
          <v-chip
            v-if="account.connected && user.membership_supported !== false"
            :color="user.is_vip ? 'amber-darken-2' : 'grey'"
            size="x-small"
            variant="tonal">
            {{ vipText }}
          </v-chip>
          <v-chip
            v-if="account.connected && compact && hasPoints"
            class="account-points-chip"
            color="info"
            size="x-small"
            variant="tonal">
            {{ points.label || "可用积分" }} {{ formattedPoints }}
          </v-chip>
        </div>
        <div v-if="account.connected && !compact && hasPoints" class="account-points">
          <span class="text-caption text-medium-emphasis">
            {{ points.label || "可用积分" }}
          </span>
          <span class="text-body-2 font-weight-bold text-primary">
            {{ formattedPoints }}
          </span>
        </div>
        <div v-if="account.connected && hasStorageInfo" class="account-storage text-caption text-medium-emphasis">
          已用 {{ storage.used || "未知" }} / {{ storage.total || "未知" }}
          <span v-if="storage.remaining">，剩余 {{ storage.remaining }}</span>
        </div>
        <div v-if="account.connected && details.length" class="account-details">
          <div v-for="item in details" :key="`${item.label}-${item.value}`" class="account-detail">
            <span class="account-detail-label">{{ item.label }}</span>
            <span class="account-detail-value">{{ item.value }}</span>
          </div>
        </div>
        <div v-if="!account.connected" class="text-caption text-warning account-error">
          {{ account.error || "请填写登录凭证并保存配置" }}
        </div>
      </div>
      <v-btn
        v-if="refreshable"
        class="account-refresh"
        icon="mdi-refresh"
        color="primary"
        variant="text"
        size="small"
        :loading="loading"
        :disabled="loading || disabled"
        title="刷新账户信息"
        aria-label="刷新账户信息"
        @click="emit('refresh')" />
    </div>
  </v-sheet>
</template>

<script setup>
import {computed} from "vue";

const props = defineProps({
  account: { type: Object, default: () => ({}) },
  compact: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  refreshable: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(["refresh"])
const user = computed(() => props.account.user || {})
const storage = computed(() => props.account.storage || {})
const points = computed(() => props.account.points || {})
const details = computed(() => (Array.isArray(props.account.details) ? props.account.details : []))
const hasLoadedInfo = computed(() => Boolean(props.account?.connected || props.account?.refreshed_at))
const hasPoints = computed(() => points.value.available !== undefined && points.value.available !== null)
const hasStorageInfo = computed(() => Boolean(storage.value.used || storage.value.total || storage.value.remaining))
const formattedPoints = computed(() => {
  const value = Number(points.value.available)
  return Number.isFinite(value) ? value.toLocaleString("zh-CN") : String(points.value.available || 0)
})
const vipText = computed(() => {
  if (user.value.vip_label) {
    return user.value.vip_expire_date
      ? `${user.value.vip_label} 至 ${user.value.vip_expire_date}`
      : user.value.vip_label
  }
  if (!user.value.is_vip) return "非VIP"
  if (user.value.is_forever_vip) return "永久VIP"
  return user.value.vip_expire_date ? `VIP 至 ${user.value.vip_expire_date}` : "VIP"
})
</script>

<style scoped>
.account-info {
  position: relative;
  overflow: hidden;
  min-height: 76px;
  padding: 14px 16px;
  border: 1px solid rgba(var(--v-theme-primary), 0.14);
  background:
    linear-gradient(135deg, rgba(var(--v-theme-primary), 0.075), transparent 58%), rgb(var(--v-theme-surface));
  box-shadow: 0 5px 18px rgba(var(--v-theme-on-surface), 0.055);
}

.account-info--compact {
  min-height: 68px;
  padding: 10px 12px;
}

.account-loading-bar {
  position: absolute;
  inset: 0 0 auto;
}

.account-content,
.account-skeleton {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 12px;
}

.account-skeleton {
  align-items: center;
}

.account-skeleton :deep(.v-skeleton-loader) {
  background: transparent;
}

.account-skeleton-lines {
  flex: 1 1 auto;
  min-width: 0;
}

.account-avatar {
  flex: 0 0 auto;
  color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.09);
}

.account-main {
  flex: 1 1 auto;
  min-width: 0;
}

.account-heading {
  display: flex;
  min-height: 24px;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding-right: 30px;
}

.account-name {
  max-width: min(280px, 55vw);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-refresh {
  position: absolute;
  top: 7px;
  right: 7px;
}

.account-points {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 2px;
}

.account-storage,
.account-error {
  margin-top: 3px;
}

.account-details {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 18px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid rgba(var(--v-theme-primary), 0.1);
}

.account-info--compact .account-details {
  grid-template-columns: repeat(auto-fit, minmax(125px, 1fr));
  gap: 4px 14px;
  margin-top: 6px;
  padding-top: 6px;
}

.account-info--compact .account-heading {
  flex-wrap: nowrap;
}

.account-info--compact .account-name {
  flex: 0 1 auto;
}

.account-points-chip {
  flex: 0 0 auto;
}

.account-detail {
  display: flex;
  min-width: 0;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  font-size: 0.75rem;
}

.account-detail-label {
  flex: 0 0 auto;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
}

.account-detail-value {
  min-width: 0;
  color: rgba(var(--v-theme-on-surface), var(--v-high-emphasis-opacity));
  overflow-wrap: anywhere;
  text-align: right;
}

@media (max-width: 600px) {
  .account-details,
  .account-info--compact .account-details {
    grid-template-columns: 1fr;
  }
}
</style>
