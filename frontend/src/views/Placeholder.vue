<script setup>
import { useRoute } from 'vue-router'
const route = useRoute()
</script>

<template>
  <div class="ph">
    <el-empty description="该模块建设中（本切片仅完成 日志检索 /search 的真实后端对接）">
      <template #description>
        <div>
          <div style="font-size: 16px; color: var(--color-text-1); margin-bottom: 8px">
            {{ route.meta.title || '模块' }} · 建设中
          </div>
          <div style="font-size: 13px; color: var(--color-text-3)">
            当前切片：日志检索已打通 UI → web(nginx) → search-service → OpenSearch(PPL/DSL)
          </div>
        </div>
      </template>
    </el-empty>
  </div>
</template>

<style scoped>
.ph { height: 100%; display: flex; align-items: center; justify-content: center; }
</style>
