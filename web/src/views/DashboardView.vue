<script setup lang="ts">
import { NCard, NGrid, NGridItem, NStatistic } from 'naive-ui'
</script>

<template>
  <div class="dashboard">
    <div class="page-header">
      <div>
        <h1 class="page-title">仪表盘</h1>
        <p class="page-subtitle">查看 Sophos 的运行概览与关键指标</p>
      </div>
    </div>
    <NGrid :x-gap="16" :y-gap="16" :cols="3" responsive="screen">
      <NGridItem>
        <NCard class="glass-panel" title="消息总数">
          <NStatistic value="--" />
        </NCard>
      </NGridItem>
      <NGridItem>
        <NCard class="glass-panel" title="活跃群聊">
          <NStatistic value="--" />
        </NCard>
      </NGridItem>
      <NGridItem>
        <NCard class="glass-panel" title="记忆条目">
          <NStatistic value="--" />
        </NCard>
      </NGridItem>
    </NGrid>
  </div>
</template>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.page-title {
  font-size: 1.5rem;
  font-weight: 600;
}

.page-subtitle {
  margin-top: 6px;
  opacity: 0.68;
}
</style>
