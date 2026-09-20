<template>
  <div class="stats-grid" aria-label="转存统计">
    <div
      v-for="(stat, index) in stats"
      :key="stat.title"
      :class="['stat-card', { 'stat-card--desktop-only': ['成功', '失败'].includes(stat.title) }]"
      :style="{
        '--stat-color': `var(--v-theme-${stat.color})`,
        '--stat-delay': `${index * 45}ms`,
      }">
      <span class="stat-accent" aria-hidden="true" />
      <div class="stat-copy">
        <div class="stat-label">{{ stat.title }}</div>
        <div class="stat-value">{{ stat.value }}</div>
      </div>
      <div class="stat-icon">
        <v-icon :icon="stat.icon" size="21" />
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({stats: {type: Array, default: () => []}});
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.stat-card {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  min-height: 76px;
  overflow: hidden;
  padding: 11px 13px 11px 16px;
  color: rgb(var(--v-theme-on-surface));
  background: rgba(var(--stat-color), 0.065);
  border: 1px solid rgba(var(--stat-color), 0.16);
  border-radius: 10px;
  animation: stat-enter 260ms ease-out both;
  animation-delay: var(--stat-delay);
}

.stat-accent {
  position: absolute;
  inset-block: 10px;
  inset-inline-start: 0;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: rgb(var(--stat-color));
}

.stat-copy {
  min-width: 0;
}

.stat-label {
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.75rem;
  font-weight: 500;
  line-height: 1.2;
}

.stat-value {
  margin-top: 5px;
  color: rgb(var(--stat-color));
  font-size: 1.45rem;
  font-weight: 750;
  line-height: 1;
  letter-spacing: -0.02em;
}

.stat-icon {
  display: grid;
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  place-items: center;
  color: rgb(var(--stat-color));
  background: rgba(var(--stat-color), 0.12);
  border-radius: 10px;
}

@keyframes stat-enter {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .stat-card {
    animation: none;
  }
}

@media (max-width: 600px) {
  .stat-card--desktop-only {
    display: none;
  }

  .stats-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .stat-card {
    min-height: 68px;
    padding: 9px 10px 9px 14px;
  }

  .stat-icon {
    width: 32px;
    height: 32px;
  }

  .stat-value {
    font-size: 1.3rem;
  }
}
</style>
