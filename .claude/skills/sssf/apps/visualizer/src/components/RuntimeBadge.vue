<script setup lang="ts">
import { computed } from 'vue'
import { runtimeLabel } from '../lib/runtime'

const props = defineProps<{ runtime: string | null | undefined }>()

const label = computed(() => runtimeLabel(props.runtime))
</script>

<template>
  <span
    v-if="label"
    class="runtime"
    :class="runtime === 'claude_code' ? 'claude' : 'pi'"
    :title="`coding agent: ${label}`"
    >{{ label }}</span
  >
</template>

<style scoped>
.runtime {
  display: inline-block;
  flex: none;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 13px;
  line-height: 20px;
  white-space: nowrap;
}

/* Claude's terracotta and pi's cyan, both muted enough to sit beside a model name. */
.runtime.claude {
  color: #e8a07a;
  border-color: rgba(217, 119, 87, 0.5);
  background: rgba(217, 119, 87, 0.08);
}

.runtime.pi {
  color: var(--cyan);
  border-color: rgba(90, 210, 221, 0.4);
  background: rgba(90, 210, 221, 0.06);
}
</style>
