<script setup lang="ts">
import { NCard, NGrid, NGridItem, NStatistic } from 'naive-ui'
</script>

<template>
  <div class="dashboard">
    <h1 class="page-title">仪表盘</h1>
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
.page-title {
  font-size: 1.5rem;
  font-weight: 600;
  margin-bottom: 20px;
}
</style>
