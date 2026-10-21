<script setup>
import { computed, onScopeDispose, ref, watch } from 'vue'
import RunResults from './RunResults.vue'
import { retrySummary } from './domain'

const props = defineProps({ records: { type: Array, default: () => [] }, request: { type: Function, required: true },
  titleFor: { type: Function, required: true }, statusFor: { type: Function, required: true }, statusKindFor: { type: Function, required: true },
  membersFor: { type: Function, required: true }, loading: Boolean })
const emit = defineEmits(['updated'])
const page = ref(1), pageSize = ref(10), selected = ref([]), pending = ref(null)
const reading = ref(false), deleting = ref(false), error = ref(''), notice = ref('')
let alive = true
const keyFor = record => String(record.history_id || record.job_id || record.id || '')
const pages = computed(() => Math.max(1, Math.ceil(props.records.length / pageSize.value)))
const rows = computed(() => props.records.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const pageKeys = computed(() => rows.value.map(keyFor).filter(Boolean))
const pageSelected = computed(() => pageKeys.value.length > 0 && pageKeys.value.every(key => selected.value.includes(key)))
const pagePartial = computed(() => !pageSelected.value && pageKeys.value.some(key => selected.value.includes(key)))
const busy = computed(() => props.loading || reading.value || deleting.value)
watch(() => props.records.map(keyFor), keys => {
  selected.value = selected.value.filter(key => keys.includes(key))
  page.value = Math.min(page.value, pages.value)
})
watch(pageSize, () => { page.value = 1 })
function selectPage(checked) {
  selected.value = checked ? [...new Set([...selected.value, ...pageKeys.value])] : selected.value.filter(key => !pageKeys.value.includes(key))
}
function selectRecord(record, checked) {
  const key = keyFor(record)
  selected.value = checked ? [...new Set([...selected.value, key])] : selected.value.filter(item => item !== key)
}
function askDelete(ids, label = '') {
  if (busy.value || !ids.length) return
  error.value = ''; pending.value = { action: 'delete', ids: [...ids], label }
}
function askClear() {
  if (busy.value || !props.records.length) return
  error.value = ''; pending.value = { action: 'clear' }
}
async function refresh() {
  reading.value = true; error.value = ''; notice.value = ''
  try {
    const result = await props.request('get', 'history')
    if (alive) emit('updated', result)
  } catch (failure) { if (alive) error.value = failure.message } finally { if (alive) reading.value = false }
}
async function remove() {
  if (!pending.value || busy.value) return
  const body = pending.value.action === 'clear' ? { action: 'clear' } : { action: 'delete', ids: pending.value.ids }
  deleting.value = true; error.value = ''
  try {
    const result = await props.request('post', 'history', body)
    if (!alive) return
    emit('updated', result)
    notice.value = body.action === 'clear' ? '运行记录已清空。' : `已删除 ${result.deleted_count || 0} 条运行记录。`
    pending.value = null
  } catch (failure) { if (alive) error.value = failure.message } finally { if (alive) deleting.value = false }
}
onScopeDispose(() => { alive = false })
</script>

<template>
  <section class="history-section run-history">
    <div class="section-heading"><h2>运行记录 <span class="count">{{ records.length }}</span></h2><div class="action-row">
      <VBtn size="small" variant="text" :loading="reading" :disabled="busy" @click="refresh">刷新记录</VBtn>
      <VBtn size="small" color="error" variant="tonal" :disabled="busy || !selected.length" @click="askDelete(selected)">删除所选（{{ selected.length }}）</VBtn>
      <VBtn size="small" color="error" variant="text" :disabled="busy || !records.length" @click="askClear">清空全部</VBtn>
    </div></div>
    <VAlert v-if="error && !pending" type="error" variant="tonal" class="mb-3" role="alert">{{ error }}</VAlert>
    <VAlert v-if="notice" type="info" variant="tonal" class="mb-3" role="status">{{ notice }}</VAlert>
    <p v-if="!records.length" class="muted">暂无运行记录。</p>
    <template v-else>
      <div class="history-selection"><div class="history-select-page"><VCheckboxBtn label="选择本页" :model-value="pageSelected" :indeterminate="pagePartial" :disabled="busy" @update:model-value="selectPage" /></div><span v-if="selected.length" class="muted">已选 {{ selected.length }} 条（可跨页选择）</span><VBtn v-if="selected.length" size="small" variant="text" :disabled="busy" @click="selected = []">取消选择</VBtn></div>
      <div v-for="record in rows" :key="keyFor(record)" class="history-row">
        <!-- Vuetify selection controls use a named grid area; keep them out of the row's grid. -->
        <div class="history-row-control"><VCheckboxBtn :model-value="selected.includes(keyFor(record))" :disabled="busy || !keyFor(record)" :aria-label="`选择记录：${titleFor(record)}`" @update:model-value="value => selectRecord(record,value)" /></div>
        <div class="history-details"><VExpansionPanels variant="accordion"><VExpansionPanel :title="`${titleFor(record)} · ${statusFor(record)}`"><template #title><span class="history-record-title">{{ titleFor(record) }}</span><span class="history-record-status" :class="`history-status-${statusKindFor(record)}`">{{ statusFor(record) }}</span></template><VExpansionPanelText><p v-if="record.message || record.error">{{ record.message || record.error }}</p><p v-if="retrySummary(record)" class="muted">{{ retrySummary(record) }}</p><RunResults :results="record.results || []" :members-for="membersFor" /></VExpansionPanelText></VExpansionPanel></VExpansionPanels></div>
        <div class="history-row-control"><VBtn icon="mdi-delete-outline" size="small" color="error" variant="text" :disabled="busy || !keyFor(record)" :aria-label="`删除记录：${titleFor(record)}`" @click="askDelete([keyFor(record)],titleFor(record))" /></div>
      </div>
      <div class="history-footer"><div class="history-size"><VSelect v-model="pageSize" :items="[5,10,20,50]" label="每页条数" density="compact" hide-details :disabled="busy" /></div><span class="muted">第 {{ (page - 1) * pageSize + 1 }}–{{ Math.min(page * pageSize,records.length) }} 条，共 {{ records.length }} 条</span><div v-if="pages > 1" class="history-pagination"><VPagination v-model="page" :length="pages" :total-visible="5" :disabled="busy" aria-label="运行记录分页" /></div></div>
    </template>
    <VDialog :model-value="!!pending" max-width="520" :persistent="deleting" @update:model-value="value => !value && !deleting && (pending = null)"><VCard :title="pending?.action === 'clear' ? '清空运行记录' : '删除运行记录'"><VCardText>
      <p>{{ pending?.action === 'clear' ? `清空当前保存的全部 ${records.length} 条运行记录？` : pending?.label ? `删除“${pending.label}”这条运行记录？` : `删除所选 ${pending?.ids.length || 0} 条运行记录？` }}</p><p class="muted">只删除这里的记录，不影响片单或媒体库内容。删除后无法恢复。</p>
      <VAlert v-if="error" type="error" variant="tonal" role="alert">{{ error }}</VAlert>
    </VCardText><VCardActions><VSpacer /><VBtn :disabled="deleting" @click="pending = null">取消</VBtn><VBtn color="error" :loading="deleting" @click="remove">{{ pending?.action === 'clear' ? '确认清空' : '确认删除' }}</VBtn></VCardActions></VCard></VDialog>
  </section>
</template>

<style scoped>
.history-selection,.history-footer { display:flex; align-items:center; flex-wrap:wrap; gap:12px; margin:12px 0; }
.history-select-page { flex:0 0 auto; }
.history-row { display:grid; grid-template-columns:40px minmax(0,1fr)40px; align-items:start; gap:8px; margin:8px 0; }
.history-row-control { display:flex; justify-content:center; align-items:center; min-width:0; min-height:56px; }
.history-details { min-width:0; overflow-wrap:anywhere; }
.history-record-title { min-width:0; text-align:start; line-height:1.5; }
.history-record-status { flex:0 0 auto; margin-inline-start:12px; font-size:14px; white-space:nowrap; }
.history-status-success { color:#54c78a; }
.history-status-error { color:rgb(var(--v-theme-error)); }
.history-status-neutral { color:var(--list-muted,rgba(var(--v-theme-on-surface),.72)); }
.history-size { flex:0 0 110px; }
.history-pagination { margin-left:auto; }
@media (max-width:600px) { .history-pagination { width:100%; margin-left:0; } }
</style>
