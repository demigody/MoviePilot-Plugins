<script setup>
import { computed, onMounted, ref } from 'vue'
import Page from './Page.vue'
defineOptions({ inheritAttrs: false })
defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'MediaLibraryPlaylistManager' }, initialConfig: { type: Object, default: () => ({}) } })
const emit = defineEmits(['layout', 'switch', 'close'])
const page = ref(null)
const busy = computed(() => Boolean(page.value?.busy))
// The host's save listener PUTs the entire config. This component deliberately
// has no save event and never forwards host attrs/listeners into Page. Page
// persists settings and lists through the scoped plugin routes instead.
onMounted(() => emit('layout', { maxWidth: '78rem' }))
</script>
<template>
  <div class="config-entry">
    <div class="config-controls"><VBtn variant="text" :disabled="busy" @click="emit('switch')">查看详情</VBtn><VBtn variant="text" :disabled="busy" @click="emit('close')">完成</VBtn></div>
    <Page ref="page" :api="api" :plugin-id="pluginId" @switch="emit('switch')" @close="emit('close')" />
  </div>
</template>
<style scoped>
.config-controls { display:flex; justify-content:flex-end; gap:8px; padding:12px 24px 0; }
@media (max-width:600px) { .config-controls { padding:8px 16px 0; } }
</style>
