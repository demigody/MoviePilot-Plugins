<script setup>
import { computed, ref, watch } from 'vue'
import { useCandidates } from './useCandidates'
import SeasonScope from './SeasonScope.vue'
import { mediaKey, candidateKey, mediaTitle, mediaSubtitle, parseIds, seasonScopeValid, supportsMediaServer, mediaServerTitle } from './domain'

const props = defineProps({ request: { type: Function, required: true }, members: { type: Array, required: true }, servers: { type: Array, default: () => [] } })
const emit = defineEmits(['add'])
const origin = ref('search'), mediaType = ref('all'), query = ref(''), idText = ref(''), collectionId = ref(''), mode = ref('once')
const serverId = ref(''), libraryIds = ref([]), libraries = ref([]), collections = ref([]), selected = ref([]), lookupError = ref(''), lookupBusy = ref(false)
const c = useCandidates(props.request, () => props.members.map(mediaKey))
const { status, message, items, source, errors } = c
const availableServers = computed(() => props.servers.filter(server => supportsMediaServer(server) && server.configured !== false).map(server => ({ ...server, title: mediaServerTitle(server) })))
const candidates = computed(() => items.value.filter(item => !props.members.some(member => mediaKey(member) === candidateKey(item))))
const selectable = computed(() => candidates.value.filter(mediaKey))
const busy = computed(() => status.value === 'loading')
const scopes = ref({})
const scoped = item => ({ ...item, seasons: mediaKey(item) in scopes.value ? scopes.value[mediaKey(item)] : item.seasons })
const scopeValid = computed(() => candidates.value.filter(item => selected.value.includes(mediaKey(item))).every(item => seasonScopeValid(scoped(item))))
const modes = [{ title: '一次性导入 · 后续不更新', value: 'once' }, { title: '关注合集 · 自动添加新电影', value: 'follow' }]
const origins = [{ title: '名称搜索', value: 'search' }, { title: '媒体库', value: 'library' }, { title: 'TMDB ID', value: 'ids' }, { title: 'TMDB 合集', value: 'tmdb' }, { title: '媒体库合集', value: 'server' }]
const mediaTypes = [{ title: '电影', value: 'movie' }, { title: '电视剧', value: 'tv' }]
const typeOptions = [{ title: '全部类型', value: 'all' }, ...mediaTypes]
watch([origin, mediaType], () => { c.reset(); selected.value = []; lookupError.value = '' })
watch(serverId, () => { c.reset(); selected.value = []; libraries.value = []; collections.value = []; libraryIds.value = []; collectionId.value = '' })
watch(items, () => { selected.value = []; scopes.value = {} })
async function lookup(endpoint) {
  if (!serverId.value) { lookupError.value = '请先选择媒体服务器。'; return }
  lookupBusy.value = true; lookupError.value = ''
  const queriedServer = serverId.value
  try {
    const result = await props.request('post', endpoint, { server_id: queriedServer })
    if (queriedServer !== serverId.value) return
    const mapped = (result.items || []).map(item => ({ ...item, title: item.name || item.title, value: item.id ?? item.value }))
    if (endpoint === 'libraries') libraries.value = mapped
    else collections.value = mapped
    if (!mapped.length) lookupError.value = endpoint === 'libraries' ? '该服务器没有可用的电影或电视剧媒体库。' : '该服务器没有合集。'
  } catch (error) { lookupError.value = error.message } finally { lookupBusy.value = false }
}
async function run() {
  lookupError.value = ''
  try {
    if (origin.value === 'search') {
      if (!query.value.trim()) throw new Error('请输入电影或电视剧名称。')
      await c.run('search', { query: query.value.trim(), media_type: mediaType.value })
    } else if (origin.value === 'ids') {
      await c.run('resolve', { tmdb_ids: parseIds(idText.value), media_type: mediaType.value })
    } else if (origin.value === 'tmdb') {
      const ids = parseIds(collectionId.value)
      if (ids.length !== 1) throw new Error('每次请输入一个 TMDB 合集 ID。')
      await c.run('tmdb-collection', { tmdb_id: ids[0], mode: mode.value })
    } else {
      if (!serverId.value) throw new Error('请先选择媒体服务器。')
      if (origin.value === 'library') await c.run('library-media', { server_id: serverId.value, library_ids: libraryIds.value, media_type: mediaType.value, query: query.value.trim() })
      else {
        if (!collectionId.value) throw new Error('请选择媒体库合集。')
        await c.run('server-collection', { server_id: serverId.value, collection_id: collectionId.value })
      }
    }
  } catch (error) { lookupError.value = error.message }
}
function add() {
  if (!scopeValid.value) return
  const chosen = selectable.value.filter(item => selected.value.includes(mediaKey(item))).map(scoped)
  emit('add', chosen, source.value)
  selected.value = []
  c.reset()
}
function toggle(item) {
  const key = mediaKey(item)
  if (key) selected.value = selected.value.includes(key) ? selected.value.filter(value => value !== key) : [...selected.value, key]
}
</script>

<template>
  <section class="add-media" aria-label="添加媒体">
    <div class="section-heading"><h3>添加媒体</h3></div>
    <VSelect v-model="origin" :items="origins" label="内容来源" density="comfortable" hide-details class="mb-4" :disabled="busy" />
    <div class="media-fields">
      <div v-if="!['tmdb', 'server'].includes(origin)"><VSelect v-model="mediaType" :items="typeOptions" label="类型筛选（可选）" hide-details="auto" :disabled="busy" /></div>
      <div v-if="['search', 'library'].includes(origin)"><VTextField v-model="query" :label="origin === 'library' ? '片名筛选（可留空）' : '电影或电视剧名称'" hide-details="auto" :disabled="busy" @keyup.enter="!busy && run()" /></div>
      <div v-if="origin === 'ids'"><VTextarea v-model="idText" label="TMDB ID" rows="2" hint="同时查找电影和电视剧；出现两个结果时，请按片名选择。多个 ID 用空格、逗号或换行分隔。" persistent-hint :disabled="busy" /></div>
    </div>
    <template v-if="['library', 'server'].includes(origin)">
      <div class="source-field"><VSelect v-model="serverId" :items="availableServers" item-title="title" item-value="id" label="媒体服务器" hide-details="auto" :disabled="busy || lookupBusy" /></div>
      <div class="lookup-row"><VBtn variant="text" :loading="lookupBusy" :disabled="busy || !serverId" @click="lookup(origin === 'library' ? 'libraries' : 'collections')">{{ origin === 'library' ? '读取媒体库范围' : '读取合集列表' }}</VBtn></div>
      <div class="source-field"><VSelect v-if="origin === 'library'" v-model="libraryIds" :items="libraries" item-title="title" item-value="value" label="媒体库范围" multiple chips clearable hide-details :disabled="busy || lookupBusy" /><VSelect v-else v-model="collectionId" :items="collections" item-title="title" item-value="value" label="媒体库合集" hide-details="auto" :disabled="busy || lookupBusy" /></div>
    </template>
    <template v-if="origin === 'tmdb'">
      <div class="source-field"><VTextField v-model="collectionId" label="TMDB 合集 ID" hint="TMDB 合集只包含电影。" persistent-hint :disabled="busy" /></div>
      <div class="source-field"><VSelect v-model="mode" :items="modes" label="导入模式" hide-details="auto" :disabled="busy" /></div>
      <p class="muted">{{ mode === 'follow' ? '保存并启用插件后，会按执行周期检查合集中的新电影，只追加新内容。手动移除的内容不会重新加入。' : '只添加本次选择的内容，以后不再检查这个合集。' }}</p>
    </template>
    <div class="action-row query-actions"><VBtn color="primary" :loading="busy" :disabled="lookupBusy" @click="run">{{ origin === 'ids' ? '解析片名' : '查询内容' }}</VBtn><VBtn v-if="busy" variant="text" @click="c.cancel">取消查询</VBtn></div>
    <VAlert v-if="lookupError" type="error" variant="tonal" class="mt-3">{{ lookupError }}</VAlert>
    <VAlert v-if="status !== 'idle'" :type="['success','empty','loading','cancelled'].includes(status) ? 'info' : 'warning'" variant="tonal" class="mt-3" role="status" aria-live="polite">{{ message }}</VAlert>
    <VAlert v-if="errors.length" type="warning" variant="tonal" class="mt-2"><div v-for="(failure,index) in errors" :key="index">{{ typeof failure === 'string' ? failure : `TMDB ${failure.tmdb_id || failure.id || ''}：${failure.error || failure.message || '解析失败'}` }}</div></VAlert>
    <div v-if="candidates.length" class="candidate-results mt-3">
      <div class="candidate-tools"><span>{{ candidates.length }} 项结果 · 可添加 {{ selectable.length }} 项</span><div class="action-row"><VBtn variant="text" size="small" :disabled="!selectable.length" @click="selected = selectable.map(mediaKey)">全选</VBtn><VBtn variant="text" size="small" @click="selected = []">清空选择</VBtn></div></div>
      <VList class="bounded-list" lines="two">
        <template v-for="item in candidates" :key="candidateKey(item)"><VListItem :title="mediaTitle(item)" :subtitle="mediaSubtitle(scoped(item))" :disabled="!mediaKey(item)" @click="toggle(item)">
          <template #title><span class="media-item-title">{{ mediaTitle(item) }}</span></template>
          <template #prepend><VCheckboxBtn :model-value="selected.includes(mediaKey(item))" :disabled="!mediaKey(item)" :aria-label="`选择 ${mediaTitle(item)}`" @click.stop @update:model-value="value => mediaKey(item) && (selected = value ? [...new Set([...selected,mediaKey(item)])] : selected.filter(key => key !== mediaKey(item)))" /></template>
        </VListItem>
        <div v-if="item.media_type === 'tv' && selected.includes(mediaKey(item))" class="candidate-scope"><SeasonScope :tmdb-id="Number(item.tmdb_id)" :title="mediaTitle(item)" :model-value="scoped(item).seasons" :request="request" :disabled="busy" @update:model-value="value => scopes[mediaKey(item)] = value" /></div></template>
      </VList>
    </div>
    <div v-if="candidates.length || source" class="action-row candidate-add"><VBtn color="primary" variant="tonal" :disabled="(!selected.length && !source) || !scopeValid" @click="add">{{ selected.length ? `添加所选 ${selected.length} 项` : source ? '保留合集更新设置' : '请先选择内容' }}</VBtn></div>
  </section>
</template>

<style scoped>
.media-fields { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,2fr); gap:16px; margin-bottom:16px; }
.media-fields:empty { display:none; }
.media-fields > div { min-width:0; }
.source-field { margin:16px 0; }
.lookup-row { display:flex; justify-content:flex-end; margin:8px 0 16px; }
.query-actions { margin-top:20px; }
.candidate-tools { justify-content:space-between; gap:12px; margin-bottom:12px; }
.candidate-add { margin-top:16px; justify-content:flex-end; }
.candidate-scope { padding:0 16px 8px; }
@media (max-width:960px) { .media-fields { grid-template-columns:minmax(0,1fr); } }
</style>
