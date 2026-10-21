<script setup>
import { computed, onScopeDispose, ref, watch } from 'vue'
import { mediaKey, uid, supportsMediaServer, mediaServerTitle, collectionDisabledReason } from './domain'
import PlaylistOrder from './PlaylistOrder.vue'
const props = defineProps({ target: { type: Object, required: true }, members: { type: Array, required: true }, servers: { type: Array, default: () => [] }, request: { type: Function, required: true }, disabled: Boolean })
defineEmits(['remove'])
const libraries = ref([]), librariesLoaded = ref(false), error = ref(''), loading = ref(false)
let libraryRequest = 0, libraryAbort = null, alive = true
const selectedServer = computed(() => props.servers.find(server => server.id === props.target.server_id))
const collectionRestriction = computed(() => collectionDisabledReason(selectedServer.value?.kind || props.target.kind, props.members))
watch([collectionRestriction, () => props.target.collection.enabled], () => {
  if (collectionRestriction.value) props.target.collection.enabled = false
}, { immediate: true, flush: 'sync' })
const canLoadLibraries = computed(() => supportsMediaServer(selectedServer.value) && selectedServer.value?.configured !== false)
const libraryItems = computed(() => {
  const items = [...libraries.value]
  const known = new Set(items.map(item => item.value))
  for (const id of props.target.library_ids) {
    const value = String(id)
    if (!known.has(value)) {
      const state = loading.value ? '正在读取名称' : librariesLoaded.value ? '未找到或不可访问' : '名称未读取'
      items.push({ title: `媒体库 ${value}（${state}）`, value })
      known.add(value)
    }
  }
  return items
})
const serverItems = computed(() => props.servers.map(server => ({ ...server, title: mediaServerTitle(server), props: { disabled: !supportsMediaServer(server) || server.configured === false } })))
function serverChanged() {
  props.target.kind = selectedServer.value?.kind || 'emby'
  props.target.pending_mapping = !props.target.server_id
  props.target.library_ids = []
  props.target.collection.remote_id = null
  delete props.target.connection_fingerprint
  for (const playlist of props.target.playlists) playlist.remote_id = null
}
function cancelLibraryRead() {
  libraryRequest += 1
  libraryAbort?.abort()
  libraryAbort = null
}
async function loadLibraries() {
  if (!props.target.server_id) { error.value = '请先选择媒体服务器。'; return }
  cancelLibraryRead()
  const requestId = libraryRequest
  libraryAbort = new AbortController()
  loading.value = true; error.value = ''
  const server = props.target.server_id
  try {
    const result = await props.request('post', 'libraries', { server_id: server }, { signal: libraryAbort.signal })
    if (!alive || requestId !== libraryRequest || server !== props.target.server_id) return
    libraries.value = (result.items || []).filter(item => (item.id ?? item.value) != null)
      .map(item => ({ title: item.name || item.title || `媒体库 ${item.id ?? item.value}`, value: String(item.id ?? item.value) }))
    librariesLoaded.value = true
    if (!libraries.value.length) error.value = '没有找到可用媒体库。'
  } catch (failure) {
    if (alive && requestId === libraryRequest && server === props.target.server_id) error.value = failure.message || '媒体库读取失败，请重试。'
  } finally {
    if (alive && requestId === libraryRequest) { loading.value = false; libraryAbort = null }
  }
}
watch([() => props.target.server_id, canLoadLibraries], () => {
  cancelLibraryRead()
  libraries.value = []; librariesLoaded.value = false; error.value = ''; loading.value = false
  if (canLoadLibraries.value) loadLibraries()
}, { immediate: true })
onScopeDispose(() => { alive = false; cancelLibraryRead() })
function addPlaylist() {
  props.target.playlists.push({ id: uid(), name: `播放列表 ${props.target.playlists.length + 1}`, enabled: true, remote_id: null, order: props.members.map(mediaKey) })
}
</script>

<template>
  <div class="target-editor">
    <div class="section-heading"><h3>媒体服务器设置</h3><VBtn size="small" color="error" variant="text" :disabled="disabled" @click="$emit('remove')">移除设置</VBtn></div>
    <div class="target-server"><VSelect v-model="target.server_id" :items="serverItems" item-title="title" item-value="id" label="媒体服务器" hide-details="auto" :disabled="disabled" @update:model-value="serverChanged" /></div>
    <p v-if="selectedServer && !supportsMediaServer(selectedServer)" class="muted">暂不支持此服务器类型，请选择其他媒体服务器。</p>
    <div class="library-heading"><span class="muted">匹配范围</span><VBtn size="small" variant="text" :loading="loading" :disabled="disabled || !canLoadLibraries" @click="loadLibraries">刷新媒体库</VBtn></div>
    <div class="target-library"><VSelect v-model="target.library_ids" :items="libraryItems" label="全部" multiple chips clearable hide-details :loading="loading" :disabled="disabled || loading" /></div>
    <VAlert v-if="error" type="error" variant="tonal" class="mb-3">{{ error }}</VAlert>
    <div class="collection-settings"><div class="target-toggle"><VSwitch v-model="target.collection.enabled" label="同步为合集" color="primary" hide-details :disabled="disabled || !!collectionRestriction" /></div><div><VTextField v-model="target.collection.name" label="合集名称" placeholder="留空使用片单名称" hide-details :disabled="disabled || !target.collection.enabled" /></div></div>
    <p v-if="collectionRestriction" class="collection-restriction" role="status">{{ collectionRestriction }}</p>
    <div class="section-heading playlist-heading"><h3>播放列表</h3><VBtn variant="tonal" size="small" prepend-icon="mdi-plus" :disabled="disabled" @click="addPlaylist">添加播放列表</VBtn></div>
    <section v-for="(playlist,index) in target.playlists" :key="playlist.id" class="playlist-editor mt-3" :aria-label="`${playlist.name || '播放列表'}顺序`">
      <div class="playlist-controls"><div class="playlist-name"><VTextField v-model="playlist.name" label="播放列表名称" hide-details :disabled="disabled" /></div><div class="target-toggle"><VSwitch v-model="playlist.enabled" label="启用" hide-details :disabled="disabled" /></div><div class="playlist-remove"><VBtn icon="mdi-delete-outline" variant="text" size="40" color="error" :aria-label="`移除播放列表 ${playlist.name}`" :disabled="disabled" @click="target.playlists.splice(index,1)" /></div></div>
      <PlaylistOrder :playlist="playlist" :members="members" :disabled="disabled" />
    </section>
    <p v-if="!target.playlists.length" class="muted">还没有播放列表，也可以只同步合集。</p>
  </div>
</template>

<style scoped>
.target-server { margin-bottom:16px; }
.target-library { margin-bottom:20px; }
.library-heading { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:8px; }
.collection-settings { display:grid; grid-template-columns:minmax(160px,1fr) minmax(0,2fr); gap:20px; align-items:start; }
.collection-settings > div,.playlist-controls > div { min-width:0; }
.collection-restriction { margin-block:8px 0; overflow-wrap:anywhere; color:rgb(var(--v-theme-error)); color:color-mix(in srgb, rgb(var(--v-theme-error)) 65%, rgb(var(--v-theme-on-surface)) 35%); }
.target-toggle,.playlist-remove { min-height:56px; display:flex; align-items:center; }
/* Keep switch animation layers local without clipping the ripple or focus ring. */
.target-toggle { contain:paint; overflow-clip-margin:20px; }
.playlist-controls { display:grid; grid-template-columns:minmax(0,1fr) auto 40px; gap:20px; align-items:start; margin-block-end:20px; }
.playlist-heading { margin-top:28px; }
@media (max-width:960px) {
  .collection-settings { grid-template-columns:minmax(0,1fr); gap:12px; }
  .target-toggle,.playlist-remove { min-height:0; }
  .playlist-controls { grid-template-columns:minmax(0,1fr) 40px; gap:12px; }
  .playlist-name { grid-column:1/-1; }
  .playlist-remove { justify-content:flex-end; align-self:center; }
}
</style>
