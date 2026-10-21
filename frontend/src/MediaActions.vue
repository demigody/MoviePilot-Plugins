<script setup>
import { computed, onMounted, ref } from 'vue'
import { createClient } from './api'
import SeasonScope from './SeasonScope.vue'
import { mediaKey, mediaSubtitle, mediaTitle, newList, seasonScopeValid } from './domain'
const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'MediaLibraryPlaylistManager' }, actionKey: String, context: { type: Object, required: true } })
const emit = defineEmits(['close', 'navigate'])
const request = (...args) => createClient(props.api, props.pluginId)(...args)
const lists = ref([]), loading = ref(false), busy = ref(false), error = ref(''), notice = ref(''), name = ref('')
const media = computed(() => props.context?.media || {})
const isCollection = computed(() => media.value.media_type === 'collection')
const validMedia = computed(() => ![media.value.type, media.value.native_type].some(value => ['episode','season'].includes(String(value || '').toLowerCase())) && !!mediaKey(media.value))
const key = computed(() => mediaKey(media.value))
const seasons = ref(null)
const contains = list => (list.members || []).some(member => mediaKey(member) === key.value)
async function load() {
  loading.value = true; error.value = ''
  try { lists.value = (await request('get', 'lists')).lists || [] } catch (failure) { error.value = failure.message } finally { loading.value = false }
}
async function resolvedMedia() {
  if (!validMedia.value) throw new Error('没有可用的 TMDB 编号，请在片单管理页搜索后添加。')
  const scope = media.value.media_type === 'tv' && seasons.value !== null ? { seasons: seasons.value } : {}
  if (!seasonScopeValid({ ...media.value, ...scope })) throw new Error('请至少选择一季。')
  if (media.value.title?.trim()) return { tmdb_id: Number(media.value.tmdb_id), media_type: media.value.media_type, title: media.value.title, year: media.value.year, poster: media.value.poster, ...scope }
  const result = await request('post', 'resolve', { tmdb_ids: [Number(media.value.tmdb_id)], media_type: media.value.media_type })
  const item = result.items?.find(member => mediaKey(member) === key.value)
  if (!item?.title) throw new Error('片名解析失败，请从片单管理页重试。')
  return { ...item, ...scope }
}
async function updateMembership(list) {
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const removal = contains(list)
    const result = await request('post', 'members', { list_id: list.id, action: removal ? 'remove' : 'add', ...(removal ? { keys: [key.value] } : { items: [await resolvedMedia()] }) })
    if (!result.list) throw new Error('服务未返回更新后的片单，请刷新后确认。')
    const index = lists.value.findIndex(item => item.id === list.id)
    lists.value.splice(index, 1, result.list)
    notice.value = removal ? `已从“${list.name}”移除。` : `已加入“${list.name}”。`
  } catch (failure) { error.value = failure.message } finally { busy.value = false }
}
async function create() {
  if (!name.value.trim()) { error.value = '请输入新片单名称。'; return }
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const list = newList(name.value.trim())
    list.members = [await resolvedMedia()]
    const result = await request('post', 'lists', { action: 'save', list, revision: 0 })
    if (Array.isArray(result.lists)) lists.value = result.lists
    else if (result.list) lists.value.push(result.list)
    else throw new Error('服务未返回新片单，请刷新后确认。')
    name.value = ''; notice.value = '片单已新建并加入当前媒体。'
  } catch (failure) { error.value = failure.message } finally { busy.value = false }
}
function manage() { emit('navigate', { navKey: 'main' }) }
onMounted(() => { if (!isCollection.value && validMedia.value) load() })
</script>

<template>
  <VDialog :model-value="true" max-width="560" :persistent="busy" @update:model-value="value => !value && emit('close')">
    <VCard title="加入片单" class="media-actions">
      <VCardText>
        <h3>{{ mediaTitle(media) }}</h3><p class="media-subtitle">{{ isCollection ? 'TMDB 合集' : mediaSubtitle(media) }}</p>
        <VAlert v-if="error" type="error" variant="tonal" class="mb-3" role="alert">{{ error }}</VAlert><VAlert v-if="notice" type="info" variant="tonal" class="mb-3" role="status">{{ notice }}</VAlert>
        <VAlert v-if="isCollection" type="info" variant="tonal">请到片单管理页导入这个合集，选择“一次性导入”或“关注合集”。导入后可选择合集中的电影。</VAlert>
        <VAlert v-else-if="!validMedia" type="warning" variant="tonal">当前内容没有可用的 TMDB 编号，或不是电影/整部电视剧。请到片单管理页搜索后添加。</VAlert>
        <template v-else>
          <SeasonScope v-if="media.media_type === 'tv'" v-model="seasons" :tmdb-id="Number(media.tmdb_id)" :title="mediaTitle(media)" :request="request" :disabled="busy || loading" />
          <VProgressLinear v-if="loading" indeterminate color="primary" class="my-4" />
          <VList v-if="lists.length" lines="one" class="quick-lists"><VListItem v-for="list in lists" :key="list.id" :title="list.name"><template #title><span class="quick-list-title">{{ list.name }}</span></template><template #append><VBtn :color="contains(list) ? undefined : 'primary'" :variant="contains(list) ? 'text' : 'tonal'" :disabled="busy || loading" @click="updateMembership(list)">{{ contains(list) ? '移除' : '加入' }}</VBtn></template></VListItem></VList>
          <p v-else-if="!loading">还没有片单，可以直接新建。</p>
          <div class="quick-create"><VTextField v-model="name" label="新片单名称" :disabled="busy || loading" hide-details density="comfortable" @keyup.enter="!busy && !loading && create()" /><VBtn variant="tonal" :loading="busy" :disabled="loading" @click="create">新建并加入</VBtn></div>
        </template>
      </VCardText>
      <VCardActions><VBtn variant="text" :disabled="busy" @click="manage">管理片单</VBtn><VSpacer /><VBtn :disabled="busy" @click="emit('close')">完成</VBtn></VCardActions>
    </VCard>
  </VDialog>
</template>
<style scoped>
.media-actions h3 { font-size:18px; font-weight:600; }
.media-subtitle { color:rgba(var(--v-theme-on-surface),.8); font-size:13px; line-height:1.6; margin:4px 0 16px; }
.quick-lists { max-height:320px; overflow:auto; margin:12px 0; }
.quick-list-title { display:block; white-space:normal; overflow-wrap:anywhere; line-height:1.5; padding:4px 0; }
.quick-create { display:flex; align-items:center; gap:12px; margin-top:20px; }
@media (max-width:450px) { .quick-create { align-items:stretch; flex-direction:column; } }
</style>
