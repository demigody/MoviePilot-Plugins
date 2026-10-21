<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AddMedia from './AddMedia.vue'
import SeasonScope from './SeasonScope.vue'
import TargetEditor from './TargetEditor.vue'
import RunResults from './RunResults.vue'
import RunHistory from './RunHistory.vue'
import { createClient, errorMessage } from './api'
import { clone, editableList, mediaKey, mediaTitle, mediaSubtitle, mergeCandidates, newList, removeMember, uid, seasonScopeValid, supportsMediaServer, retrySummary } from './domain'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'MediaLibraryPlaylistManager' }, show_switch: { type: Boolean, default: true } })
const emit = defineEmits(['action', 'switch', 'close'])
const request = (...args) => createClient(props.api, props.pluginId)(...args)
const loading = ref(false), saving = ref(false), savingSettings = ref(false), error = ref(''), notice = ref('')
const importError = ref('')
const togglingIds = ref(new Set()), listStateErrors = ref({})
const settings = ref({ enabled: false, cron: '*/30 * * * *' }), lists = ref([]), servers = ref([]), history = ref([])
const historyRevision = ref(0)
const editing = ref(null), editorOpen = ref(false), tab = ref('members'), addOpen = ref(false)
const syncDialog = ref(false), syncLists = ref([]), publishListId = ref(null), publishing = ref(false), job = ref(null)
const syncSkippedCount = ref(0)
const polling = ref(false), finalizingJob = ref(false), progressError = ref('')
const importDialog = ref(false), importSource = ref('trakt'), importUrl = ref(''), importBusy = ref(false)
const importPreview = ref(null), importSelected = ref([])
const importFileInput = ref(null)
const activeJobStatuses = ['queued', 'running', 'waiting']
const finishedJobStatuses = ['success', 'partial', 'error', 'cancelled']
let pollTimer = null, alive = true, pollCount = 0
const hasRunningJob = computed(() => publishing.value || finalizingJob.value || activeJobStatuses.includes(job.value?.status))
const progressLabel = computed(() => job.value?.status === 'waiting' ? '等待媒体库识别…' : '正在同步片单…')
const measuredProgress = computed(() => {
  if (publishing.value && !finalizingJob.value && !activeJobStatuses.includes(job.value?.status)) return null
  const value = job.value?.progress
  if (!value || !Number.isInteger(value.completed) || !Number.isInteger(value.total)
      || value.total < 1 || value.completed < 0 || value.completed > value.total) return null
  return {...value, percent: 100 * value.completed / value.total}
})
const stageLabel = computed(() => finalizingJob.value ? '正在更新片单和运行记录…' : ({
  preflight:'正在检查媒体服务器', inventory:'正在读取媒体库', episodes:'正在读取电视剧集',
  seasons:'正在读取电视剧季', plan:'正在核对片单内容', add:'正在加入内容', verify:'正在核对同步结果',
  order:'正在调整播放顺序', remove:'正在移除片单外的内容', verify_final:'正在核对最终结果',
  sources:'正在更新关注的合集', subscribe:'正在检查自动订阅', saving:'正在保存结果',
  target_finished:'正在处理下一项任务', finished:'正在完成任务',
}[measuredProgress.value?.stage] || '正在处理…'))
const enabledLists = computed(() => lists.value.filter(list => list.enabled))
const hasMediaTarget = list => (list.targets || []).some(target => target.server_id && !target.pending_mapping)
const syncableLists = computed(() => enabledLists.value.filter(hasMediaTarget))
const importNameConflicts = computed(() => {
  if (!importPreview.value) return []
  const selected = importSelected.value.map(index => ({ index, list: importPreview.value.lists[index] }))
  const counts = new Map()
  for (const { list } of selected) {
    const key = String(list?.name || '').trim().toLocaleLowerCase()
    if (key) counts.set(key, (counts.get(key) || 0) + 1)
  }
  return selected.filter(({ list }) => {
    const key = String(list?.name || '').trim().toLocaleLowerCase()
    return !key || counts.get(key) > 1 || lists.value.some(existing => existing.name.trim().toLocaleLowerCase() === key)
  }).map(({ index }) => index)
})
const emptySyncLists = computed(() => syncLists.value.filter(list => list.clearsOutput))
const counts = computed(() => ({ movies: editing.value?.members.filter(item => item.media_type === 'movie').length || 0, series: editing.value?.members.filter(item => item.media_type === 'tv').length || 0 }))
const resultRows = computed(() => job.value?.results || job.value?.result?.results || (Array.isArray(job.value?.result) ? job.value.result : []))
function applyLists(result) {
  if (Object.prototype.hasOwnProperty.call(result, 'binding_error')) settings.value.binding_error = result.binding_error
  if (Array.isArray(result.lists)) {
    const current = new Map(lists.value.map(list => [list.id,list]))
    lists.value = result.lists.map(list => current.get(list.id)?.revision > list.revision ? current.get(list.id) : list)
  }
  else if (result.list) {
    const index = lists.value.findIndex(item => item.id === result.list.id)
    if (index < 0) lists.value.push(result.list)
    else if (!(lists.value[index].revision > result.list.revision)) lists.value.splice(index, 1, result.list)
  }
}
function applyHistory(result) {
  const revision = Number(result.revision || 0)
  if (revision < historyRevision.value) return
  history.value = result.items || result.history || []
  historyRevision.value = revision
}
async function refresh() {
  loading.value = true; error.value = ''
  const results = await Promise.allSettled(['settings', 'lists', 'servers', 'history'].map(endpoint => request('get', endpoint)))
  if (!alive) return
  const failures = []
  results.forEach((result, index) => {
    if (result.status === 'rejected') { failures.push(result.reason.message); return }
    const data = result.value
    if (index === 0) settings.value = { ...settings.value, ...(data.settings || data) }
    if (index === 1) applyLists(data)
    if (index === 2) servers.value = data.items || data.servers || []
    if (index === 3) applyHistory(data)
  })
  error.value = [...new Set(failures)].join('；')
  loading.value = false
}
async function saveSettings() {
  savingSettings.value = true; error.value = ''
  try {
    const result = await request('post', 'settings', { enabled: settings.value.enabled, cron: settings.value.cron })
    settings.value = { ...settings.value, ...(result.settings || result) }
    notice.value = '全局设置已保存。'; emit('action')
  } catch (failure) { error.value = failure.message } finally { savingSettings.value = false }
}
function openImport(source = 'trakt') {
  importSource.value = source; importUrl.value = ''; importPreview.value = null
  importSelected.value = []; importError.value = ''
  importDialog.value = true
}
function setImportDialog(value) {
  if (importBusy.value && !value) return
  importDialog.value = value
  if (!value) importError.value = ''
}
function closeImport() { setImportDialog(false) }
function toggleImportSelection(index, checked) {
  const selected = new Set(importSelected.value)
  if (checked) selected.add(index); else selected.delete(index)
  importSelected.value = [...selected].sort((a, b) => a - b)
}
function importNameError(index) {
  return importNameConflicts.value.includes(index) ? '片单名称重复，请修改名称后再导入。' : ''
}
async function previewTrakt() {
  if (!importUrl.value.trim()) { importError.value = '请输入 Trakt 片单链接。'; return }
  importBusy.value = true; importError.value = ''
  try {
    const result = await request('post', 'import/preview', { source: 'trakt', url: importUrl.value.trim() }, { timeout: 120000 })
    importPreview.value = result
    importSelected.value = (result.lists || []).map((_, index) => index)
  } catch (failure) { importError.value = failure.message } finally { importBusy.value = false }
}
async function previewFile(file) {
  if (!file) return
  importBusy.value = true; importError.value = ''
  try {
    const document = JSON.parse(await file.text())
    const result = await request('post', 'import/preview', { source: 'file', document }, { timeout: 120000 })
    importPreview.value = result
    importSelected.value = (result.lists || []).map((_, index) => index)
    importSource.value = 'file'
  } catch (failure) { importError.value = failure.message || '无法读取片单文件。' } finally { importBusy.value = false }
}
function onImportFile(event) {
  const file = event?.target?.files?.[0]
  if (file) previewFile(file)
  if (event?.target) event.target.value = ''
}
function importedOutputSummary(list) {
  const targets = list.targets || []
  if (!targets.length) return ''
  const collections = targets.filter(target => target.collection?.enabled || target.collection?.name).length
  const playlists = targets.reduce((count, target) => count + (target.playlists?.length || 0), 0)
  return `${collections} 个合集 · ${playlists} 个播放列表`
}
function downloadBlob(name, content) {
  const blob = new Blob([content], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a'); link.href = url; link.download = name
  document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}
async function exportList(list = null) {
  error.value = ''
  try {
    const result = await request('get', 'export', list ? { list_id: list.id } : {}, { timeout: 60000 })
    const document = result.document
    const safeName = (list?.name || 'MoviePilot-片单备份').replace(/[<>:"/\\|?*]/g, '_').slice(0, 80) || 'MoviePilot-片单'
    downloadBlob(`${safeName}.json`, `${JSON.stringify(document, null, 2)}\n`)
    notice.value = list ? `已导出片单“${list.name}”。` : '已导出全部片单。'
  } catch (failure) { error.value = failure.message }
}
async function commitImport() {
  if (!importPreview.value || !importSelected.value.length) { importError.value = '请选择要导入的片单。'; return }
  if (importNameConflicts.value.length) { importError.value = '片单名称重复，请修改重复名称后再导入。'; return }
  const selected = importSelected.value.map(index => importPreview.value.lists[index])
  importBusy.value = true; importError.value = ''
  try {
    const result = await request('post', 'import/commit', { lists: selected }, { timeout: 120000 })
    applyLists(result)
    importDialog.value = false; importError.value = ''
    notice.value = `已导入 ${result.imported?.length || selected.length} 个片单。它们默认停用，请检查内容和同步设置后再启用。`
  } catch (failure) { importError.value = failure.message } finally { importBusy.value = false }
}
function open(list) {
  if (saving.value || (list && togglingIds.value.has(list.id))) return
  editing.value = list ? editableList(list) : { ...newList(), enabled: false }
  tab.value = 'members'; addOpen.value = false; error.value = ''; notice.value = ''; editorOpen.value = true
}
function closeEditor() {
  if (saving.value) return
  editorOpen.value = false; editing.value = null; addOpen.value = false; notice.value = ''; error.value = ''
}
async function saveList() {
  if (!editing.value?.name?.trim()) { error.value = '请输入片单名称。'; return }
  if (editing.value.members.some(member => !seasonScopeValid(member))) { error.value = '指定季的电视剧需要至少选择一季。'; return }
  if (editing.value.enabled && !hasMediaTarget(editing.value)) { error.value = '请先添加并选择媒体服务器，再启用片单。'; return }
  if (editing.value.targets.some(target => !target.server_id || !supportsMediaServer(target))) { error.value = '请为每组媒体库同步设置选择已配置的媒体服务器。'; return }
  if (editing.value.targets.some(target => target.playlists.some(playlist => !playlist.name?.trim()))) { error.value = '请输入每个播放列表的名称。'; return }
  saving.value = true; error.value = ''
  try {
    const snapshot = editableList(editing.value)
    snapshot.name = snapshot.name.trim()
    const result = await request('post', 'lists', { action: 'save', list: clone(snapshot), revision: snapshot.revision })
    applyLists(result)
    editorOpen.value = false; editing.value = null; notice.value = '片单已保存。'; emit('action')
  } catch (failure) { error.value = failure.message } finally { saving.value = false }
}
async function deleteList(list) {
  if (!window.confirm(`删除片单“${list.name}”？仅移除本地片单，服务器已有合集和播放列表会保留。`)) return
  error.value = ''; saving.value = true
  try {
    const result = await request('post', 'lists', { action: 'delete', list_id: list.id, revision: list.revision })
    if (Array.isArray(result.lists)) applyLists(result)
    else lists.value = lists.value.filter(item => item.id !== list.id)
    notice.value = '片单已删除，服务器内容保留。'; emit('action')
  } catch (failure) { error.value = failure.message } finally { saving.value = false }
}
async function setListEnabled(list, enabled) {
  if (saving.value || loading.value || hasRunningJob.value || togglingIds.value.has(list.id) || enabled === list.enabled) return
  togglingIds.value.add(list.id); delete listStateErrors.value[list.id]
  try {
    const result = await request('post', 'lists', { action: 'set_enabled', list_id: list.id, enabled, revision: list.revision })
    if (!result.list || result.list.id !== list.id || result.list.enabled !== enabled) throw new Error('未确认片单状态已保存，请刷新后重试。')
    if (alive) applyLists({list:result.list})
  } catch (failure) { if (alive) listStateErrors.value[list.id] = failure.message }
  finally { togglingIds.value.delete(list.id) }
}
function addMembers(items, source) {
  const count = mergeCandidates(editing.value, items, source)
  notice.value = `已添加 ${count} 项内容${source ? '，并保留合集更新设置' : ''}。请保存片单。`
}
function addTarget() {
  editing.value.targets.push({ id: uid(), server_id: '', kind: 'emby', library_ids: [], collection: { enabled: false, name: '', remote_id: null }, playlists: [] })
}
function startSync(listId = null) {
  if (hasRunningJob.value || syncDialog.value || saving.value || loading.value) return
  if (togglingIds.value.size) { error.value = '片单状态正在保存，请稍后再同步。'; return }
  const requested = enabledLists.value.filter(list => !listId || list.id === listId)
  const selected = requested.filter(hasMediaTarget)
  syncSkippedCount.value = requested.length - selected.length
  if (!selected.length) { error.value = '没有已设置媒体服务器的已启用片单可同步。'; return }
  error.value = ''
  publishListId.value = listId
  syncLists.value = selected.map(list => ({
    id:list.id, name:list.name,
    movies:list.members.filter(member => member.media_type === 'movie').length,
    series:list.members.filter(member => member.media_type === 'tv').length,
    targets:list.targets.length, empty:!list.members.length,
    clearsOutput:!list.members.length && list.targets.some(target =>
      target.collection.enabled || target.playlists.some(playlist => playlist.enabled)),
  }))
  syncDialog.value = true
}
async function refreshHistory() {
  try { const result = await request('get', 'history'); if (alive) applyHistory(result) } catch (failure) { error.value = failure.message }
}
async function pollJob() {
  if (!alive || !job.value?.id || polling.value) return
  const jobId = job.value.id
  polling.value = true
  if (error.value === progressError.value) error.value = ''
  progressError.value = ''
  try {
    const result = await request('get', 'job', { job_id: jobId })
    if (!alive || job.value?.id !== jobId) return
    const next = result.job || result
    if (![...activeJobStatuses, ...finishedJobStatuses].includes(next.status)) throw new Error('服务器返回的任务状态无效，请刷新进度。')
    finalizingJob.value = finishedJobStatuses.includes(next.status)
    job.value = { ...next, id: jobId }
    if (activeJobStatuses.includes(job.value.status)) {
      if (++pollCount < 180) pollTimer = setTimeout(pollJob, job.value.status === 'waiting' ? 30000 : 2000)
      else progressError.value = '任务仍在后台运行，请刷新进度。'
    } else {
      notice.value = job.value.status === 'success' ? '任务完成。' : '任务已结束，请查看执行结果。'
      const listResult = await request('get', 'lists')
      if (!alive) return
      applyLists(listResult)
      await refreshHistory()
      if (alive) emit('action')
    }
  } catch (failure) {
    if (alive) { progressError.value = `读取任务失败：${failure.message}`; error.value = progressError.value }
  } finally { polling.value = false; finalizingJob.value = false }
}
async function publish() {
  if (hasRunningJob.value || !syncDialog.value || !syncLists.value.length) return
  const allowEmpty = emptySyncLists.value.length > 0
  if (togglingIds.value.size) { error.value = '片单状态正在保存，请稍后再同步。'; return }
  if (!syncableLists.value.some(list => !publishListId.value || list.id === publishListId.value)) { error.value = '没有已设置媒体服务器的已启用片单可同步。'; return }
  error.value = ''; progressError.value = ''; publishing.value = true; syncDialog.value = false
  try {
    const result = await request('post', 'publish', { ...(publishListId.value ? { list_id: publishListId.value } : {}), dry_run: false, allow_empty: allowEmpty })
    if (!alive) return
    if (!result.job_id) throw new Error('服务没有返回发布任务 ID。')
    job.value = { id: result.job_id, status: 'queued' }; pollCount = 0
    clearTimeout(pollTimer); await pollJob()
  } catch (failure) { error.value = failure.message } finally { publishing.value = false }
}
function refreshJob() { if (polling.value) return; clearTimeout(pollTimer); pollCount = 0; pollJob() }
function keepProgressFocus(event) {
  const card = event.currentTarget
  const last = [...card.querySelectorAll('button:not([disabled])')].at(-1) || card
  if (event.shiftKey && event.target === card) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && event.target === last) { event.preventDefault(); card.focus() }
}
const statusLabel = value => ({ queued: '等待中', running: '执行中', waiting: '等待媒体库识别', success: '完成', skipped: '已跳过', partial: '部分成功', error: '失败', cancelled: '已取消' }[value] || (value ? '状态待确认' : '待执行'))
const statusKind = value => value === 'success' ? 'success' : ['partial', 'error', 'cancelled'].includes(value) ? 'error' : 'neutral'
const membersFor = result => lists.value.find(list => list.id === result.list_id)?.members || []
const formatTime = value => {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' })
}
const historyTime = record => formatTime(record.timestamp || record.time || record.finished_at || record.started_at || record.created_at)
const historyTitle = record => {
  const names = record.list_name || record.rule_name || [...new Set((record.results || []).map(result => result.list_name).filter(Boolean))].join('、')
  return [names, historyTime(record), record.dry_run ? '预览' : ''].filter(Boolean).join(' · ')
}
const historyStatus = record => statusLabel(record.status || (record.success === false ? 'error' : 'success'))
const historyStatusKind = record => statusKind(record.status || (record.success === false ? 'error' : 'success'))
onMounted(refresh)
onUnmounted(() => { alive = false; clearTimeout(pollTimer) })
defineExpose({ open, saveList, refresh, editing, lists, settings, busy: computed(() => saving.value || savingSettings.value || hasRunningJob.value) })
</script>

<template>
  <div class="playlist-plugin" :inert="hasRunningJob || undefined" :aria-busy="hasRunningJob">
    <header class="plugin-heading"><div><h1>媒体库片单管理</h1><p>Media Library Playlist Manager · 管理电影和电视剧片单，支持多种媒体服务器的合集和播放列表同步。</p></div><VBtn variant="tonal" :loading="loading" :disabled="saving" @click="refresh">刷新</VBtn></header>
    <VAlert v-if="error" type="error" variant="tonal" class="mb-4" closable @click:close="error = ''" role="alert">{{ error }}</VAlert>
    <VAlert v-if="settings.binding_error" type="error" variant="tonal" class="mb-4" role="alert">已读取到存在重复绑定的片单。为避免改错媒体库内容，同步和自动任务已暂停；请编辑或删除重复的发布目标后再继续。{{ settings.binding_error }}</VAlert>
    <VAlert v-if="notice" type="info" variant="tonal" class="mb-4" closable @click:close="notice = ''" role="status">{{ notice }}</VAlert>
    <VAlert v-if="settings.migration_warnings?.length" type="warning" variant="tonal" class="mb-4"><div v-for="(warning,index) in settings.migration_warnings" :key="index">{{ warning }}</div></VAlert>
    <VAlert v-if="settings.migrated_interval_minutes" type="info" variant="tonal" class="mb-4">目前沿用旧设置，每 {{ settings.migrated_interval_minutes }} 分钟执行一次。选择新的执行周期并保存后，会改用新设置。</VAlert>
    <section class="settings-section">
      <div class="section-heading"><div><h2>自动执行</h2><p>开启后，按设定的周期更新关注的合集，并执行片单中开启的自动任务。</p></div></div>
      <div class="settings-controls">
        <div class="setting-toggle"><VSwitch v-model="settings.enabled" label="启用插件" color="primary" hide-details :disabled="savingSettings || loading || !!settings.binding_error" /></div>
        <div class="setting-cron"><VCronField v-model="settings.cron" label="执行周期" hint="点击选择执行频率和时间，也可直接填写 Cron 表达式。" persistent-hint prepend-inner-icon="mdi-clock-outline" :disabled="savingSettings || loading" /></div>
        <div class="setting-save"><VBtn variant="tonal" :loading="savingSettings" :disabled="loading" @click="saveSettings">保存设置</VBtn></div>
      </div>
    </section>
    <section class="lists-section">
      <div class="section-heading"><h2>我的片单 <span class="count">{{ lists.length }}</span></h2><div class="action-row"><VBtn variant="outlined" :disabled="loading || saving" @click="openImport('trakt')">导入片单</VBtn><VBtn variant="outlined" :disabled="loading || !lists.length" @click="exportList()">导出全部</VBtn><VBtn variant="outlined" :disabled="hasRunningJob || loading || !!settings.binding_error || !syncableLists.length" @click="startSync()">同步全部</VBtn><VBtn color="primary" variant="outlined" prepend-icon="mdi-plus" :disabled="saving || loading" @click="open()">新建片单</VBtn></div></div>
      <div v-if="!lists.length && !loading" class="empty-state"><VIcon icon="mdi-playlist-plus" size="42" /><h3>从一个片单开始</h3><p>先取个名字，再添加电影、电视剧或 TMDB 合集。</p><VBtn color="primary" variant="outlined" @click="open()">新建片单</VBtn></div>
      <div v-else class="list-directory">
        <article v-for="list in lists" :key="list.id" class="list-entry" :class="{ 'list-state-pending': togglingIds.has(list.id) }">
          <button type="button" class="list-summary" :disabled="saving || togglingIds.has(list.id)" :aria-label="`打开片单 ${list.name}`" @click="open(list)"><VIcon icon="mdi-playlist-play" /><span class="list-copy"><span class="list-name">{{ list.name }}</span><span class="muted list-meta">{{ list.members?.filter(item => item.media_type === 'movie').length || 0 }} 部电影 · {{ list.members?.filter(item => item.media_type === 'tv').length || 0 }} 部电视剧 · {{ list.targets?.length || 0 }} 组媒体库设置</span></span></button>
          <div class="list-actions"><div class="list-state"><VSwitch :model-value="list.enabled" :label="list.enabled ? '已启用' : '已停用'" :aria-label="`启用片单 ${list.name}`" color="primary" density="compact" hide-details inset :mobile-layout="false" :loading="togglingIds.has(list.id)" :disabled="saving || loading || hasRunningJob || togglingIds.has(list.id) || (!!settings.binding_error && !list.enabled) || (!list.enabled && !hasMediaTarget(list))" @update:model-value="value => setListEnabled(list,value)" /></div><VBtn variant="outlined" :aria-label="`编辑 ${list.name}`" :disabled="saving || togglingIds.has(list.id)" @click="open(list)">编辑</VBtn><VBtn variant="outlined" :disabled="!list.enabled || !hasMediaTarget(list) || hasRunningJob || saving || togglingIds.has(list.id) || !!settings.binding_error" :aria-label="`同步 ${list.name}`" @click="startSync(list.id)">同步</VBtn><VBtn icon="mdi-download-outline" size="40" variant="outlined" :aria-label="`导出 ${list.name}`" :disabled="loading" @click="exportList(list)"/><VBtn icon="mdi-delete-outline" size="40" color="error" variant="outlined" :disabled="saving || hasRunningJob || togglingIds.has(list.id)" :aria-label="`删除 ${list.name}`" @click="deleteList(list)" /></div>
          <p v-if="listStateErrors[list.id]" class="list-state-error" role="alert">{{ listStateErrors[list.id] }}</p>
        </article>
      </div>
    </section>
    <section v-if="job" class="job-section" aria-live="polite"><div class="section-heading"><h2>当前任务 · <span :class="`task-status-${statusKind(job.status)}`">{{ statusLabel(job.status) }}</span></h2><VBtn size="small" variant="text" :disabled="publishing" @click="refreshJob">刷新任务</VBtn></div><p v-if="retrySummary(job)" class="muted">{{ retrySummary(job) }}</p><p v-if="job.error" class="error-text">{{ errorMessage(job.error) }}</p><RunResults :results="resultRows" :members-for="membersFor" /></section>
    <RunHistory :records="history" :request="request" :title-for="historyTitle" :status-for="historyStatus" :status-kind-for="historyStatusKind" :members-for="membersFor" :loading="loading" @updated="applyHistory" />

    <input ref="importFileInput" class="import-file-input" type="file" accept=".json,application/json" @change="onImportFile">
    <VDialog :model-value="importDialog" max-width="760" scrollable :persistent="importBusy" aria-label="导入片单" @update:model-value="setImportDialog">
      <VCard class="playlist-plugin import-dialog">
        <VCardTitle class="import-dialog-title">
          <span>导入片单</span>
          <VTooltip location="bottom" max-width="340">
            <template #activator="{ props: tooltipProps }">
              <VBtn v-bind="tooltipProps" icon="mdi-help-circle-outline" variant="text" size="32" aria-label="查看导入说明" />
            </template>
            <span>导入片单内容、合集信息、播放列表，需自行设置媒体服务器信息。片单默认停用，自动订阅、自动同步默认关闭。</span>
          </VTooltip>
        </VCardTitle>
        <VCardText>
          <VAlert v-if="importError" type="error" variant="tonal" class="mb-4" role="alert">{{ importError }}</VAlert>
          <VTabs v-if="!importPreview" v-model="importSource" color="primary" class="mb-4"><VTab value="trakt">Trakt 片单</VTab><VTab value="file">MoviePilot 文件</VTab></VTabs>
          <div v-if="!importPreview && importSource === 'trakt'" class="import-source-form">
            <VTextField v-model="importUrl" label="Trakt 公开片单链接" placeholder="https://app.trakt.tv/users/.../lists/..." :disabled="importBusy" @keydown.enter="previewTrakt" />
            <p class="muted">导入片单当前默认显示的内容顺序。私有片单需要授权，暂不支持。</p>
            <VBtn color="primary" variant="tonal" :loading="importBusy" @click="previewTrakt">读取片单</VBtn>
          </div>
          <div v-else-if="!importPreview" class="import-source-form">
            <p>选择之前导出的 MoviePilot 片单 JSON 文件。导入的片单默认停用。</p>
            <VBtn color="primary" variant="tonal" prepend-icon="mdi-file-upload-outline" :disabled="importBusy" @click="importFileInput?.click()">选择 JSON 文件</VBtn>
          </div>
          <div v-else class="import-preview">
            <div class="section-heading"><div><h3>导入预览</h3><p>{{ importPreview.source === 'trakt' ? '来自 Trakt' : '来自 MoviePilot 标准文件' }} · {{ importSelected.length }} / {{ importPreview.lists.length }} 个片单</p></div><VBtn variant="text" :disabled="importBusy" @click="importPreview = null">返回</VBtn></div>
            <article v-for="(list,index) in importPreview.lists" :key="list.id" class="import-list-preview">
              <div class="import-list-heading">
                <VCheckbox :model-value="importSelected.includes(index)" :aria-label="`选择导入片单 ${list.name}`" :disabled="importBusy" hide-details @update:model-value="value => toggleImportSelection(index,value)" />
                <div class="import-list-copy">
                  <VTextField v-model="list.name" label="导入后的片单名称" hide-details="auto" :error-messages="importNameError(index)" :disabled="importBusy" />
                  <p class="import-list-count">{{ list.members.length }} 项 · {{ list.members.filter(item => item.media_type === 'movie').length }} 部电影 · {{ list.members.filter(item => item.media_type === 'tv').length }} 部电视剧</p>
                </div>
              </div>
              <details class="import-details">
                <summary>查看片单内容（{{ list.members.length }} 项）</summary>
                <ol><li v-for="member in list.members" :key="mediaKey(member)">{{ mediaTitle(member) }} · {{ member.media_type === 'movie' ? '电影' : '电视剧' }} · TMDB {{ member.tmdb_id }}<span v-if="member.seasons"> · {{ member.seasons.map(season => season === 0 ? '特别篇' : `第 ${season} 季`).join('、') }}</span></li></ol>
              </details>
              <p v-if="list.targets?.length" class="import-output-summary"><VIcon icon="mdi-sync" size="18" aria-hidden="true" /><span>媒体库同步</span><strong>{{ importedOutputSummary(list) }}</strong></p>
            </article>
            <VAlert v-if="importPreview.unsupported?.length" type="warning" variant="tonal" class="mt-3">有 {{ importPreview.unsupported.length }} 项无法导入：{{ importPreview.unsupported.slice(0,8).join('；') }}<span v-if="importPreview.unsupported.length > 8">等</span></VAlert>
          </div>
        </VCardText>
        <VCardActions class="dialog-actions">
          <VBtn :disabled="importBusy" @click="closeImport">取消</VBtn>
          <VBtn v-if="importPreview" color="primary" variant="tonal" :loading="importBusy" :disabled="!importSelected.length || importNameConflicts.length" @click="commitImport">确认导入</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>

    <VDialog :model-value="editorOpen" max-width="1160" :fullscreen="$vuetify.display.smAndDown" scrollable :persistent="saving" @update:model-value="value => !value && closeEditor()">
      <VCard v-if="editing" class="playlist-plugin editor-card">
        <VCardTitle>片单设置</VCardTitle>
        <VCardText class="editor-body">
          <VAlert v-if="error" type="error" variant="tonal" class="mb-3" role="alert">{{ error }}</VAlert><VAlert v-if="notice" type="info" variant="tonal" class="mb-3" role="status">{{ notice }}</VAlert>
          <div class="editor-basics"><div><VTextField v-model="editing.name" label="片单名称" hide-details="auto" :disabled="saving" /></div><div class="editor-toggle"><VSwitch v-model="editing.enabled" label="启用此片单" color="primary" hide-details :disabled="saving || (!editing.enabled && !hasMediaTarget(editing))" /></div></div>
          <VTabs v-model="tab" color="primary" class="editor-tabs"><VTab value="members">片单内容</VTab><VTab value="targets">媒体库同步</VTab><VTab value="policy">自动任务</VTab></VTabs>
          <div v-if="tab === 'members'">
            <div class="section-heading"><div><h3>已添加的内容</h3><p>{{ counts.movies }} 部电影 · {{ counts.series }} 部电视剧</p></div><VBtn variant="tonal" prepend-icon="mdi-plus" :disabled="saving" @click="addOpen = !addOpen">{{ addOpen ? '收起添加' : '添加媒体' }}</VBtn></div>
            <VList v-if="editing.members.length" class="bounded-list member-list mb-4" lines="two"><div v-for="member in editing.members" :key="mediaKey(member)" class="member-entry"><VListItem :title="mediaTitle(member)" :subtitle="mediaSubtitle(member)"><template #title><span class="media-item-title">{{ mediaTitle(member) }}</span></template><template #prepend><VIcon :icon="member.media_type === 'tv' ? 'mdi-television' : 'mdi-movie-open-outline'" class="mr-3" /></template><template #append><VBtn icon="mdi-close" variant="text" size="40" :disabled="saving" :aria-label="`移除 ${mediaTitle(member)}`" @click="removeMember(editing,mediaKey(member))" /></template></VListItem><div v-if="member.media_type === 'tv'" class="member-season"><SeasonScope v-model="member.seasons" :tmdb-id="Number(member.tmdb_id)" :title="mediaTitle(member)" :request="request" :disabled="saving" /></div></div></VList>
            <p v-else class="muted mb-4">还没有内容，点击“添加媒体”选择电影或电视剧。</p>
            <AddMedia v-if="addOpen && !saving" :request="request" :members="editing.members" :servers="servers" @add="addMembers" />
            <div v-if="editing.sources.length" class="sources-section mt-5"><h3>已导入的 TMDB 合集</h3><div v-for="(source,index) in editing.sources" :key="source.tmdb_id" class="source-row"><div><strong>{{ source.name || `合集 ${source.tmdb_id}` }}</strong><p>{{ source.mode === 'follow' ? '关注合集 · 定时添加新电影' : '一次性导入 · 后续不更新' }}<span v-if="source.last_checked"> · 上次检查 {{ formatTime(source.last_checked) }}</span></p><p v-if="source.error" class="error-text">{{ source.error }}</p></div><VBtn size="small" variant="text" color="error" :disabled="saving" @click="editing.sources.splice(index,1)">移除来源</VBtn></div></div>
          </div>
          <div v-if="tab === 'targets'"><TargetEditor v-for="(target,index) in editing.targets" :key="target.id" :target="target" :members="editing.members" :servers="servers" :request="request" :disabled="saving" @remove="editing.targets.splice(index,1)" /><p v-if="!editing.targets.length" class="muted mb-4">还没有媒体服务器设置。</p><div class="action-row"><VBtn variant="tonal" prepend-icon="mdi-plus" :disabled="saving" @click="addTarget">添加媒体服务器设置</VBtn></div></div>
          <div v-if="tab === 'policy'" class="policy-section"><div class="policy-option"><VSwitch v-model="editing.policy.auto_subscribe" label="自动订阅尚未入库的内容" color="primary" :disabled="saving" hide-details /><p class="muted">将缺少的电影或所选电视剧季加入 MoviePilot 订阅，可能开始下载。</p></div><div class="policy-option"><VSwitch v-model="editing.policy.publish_on_library" label="入库后自动同步到媒体库" color="primary" :disabled="saving" hide-details /><p class="muted">新内容入库后，更新对应的合集和播放列表。</p></div></div>
        </VCardText>
        <VCardActions class="editor-actions"><div class="action-row"><VBtn variant="text" :disabled="saving" @click="closeEditor">取消</VBtn><VBtn color="primary" variant="tonal" :loading="saving" @click="saveList">保存片单</VBtn></div></VCardActions>
      </VCard>
    </VDialog>
    <VDialog v-model="syncDialog" max-width="560" aria-label="确认同步">
      <VCard title="确认同步" class="sync-confirm-dialog">
        <VCardText>
          <p v-if="!publishListId" class="sync-confirm-scope">本次同步 {{ syncLists.length }} 个已启用片单</p>
          <ul class="sync-confirm-lists">
            <li v-for="list in syncLists" :key="list.id" class="sync-confirm-list">
              <strong class="sync-confirm-name">{{ list.name }}<span v-if="list.empty" class="sync-empty-label">空片单</span></strong>
              <p class="sync-confirm-counts">{{ list.movies }} 部电影 · {{ list.series }} 部电视剧 · {{ list.targets }} 组媒体库设置</p>
            </li>
          </ul>
          <VAlert v-if="syncSkippedCount" type="info" variant="tonal" class="sync-skip-unconfigured">另有 {{ syncSkippedCount }} 个未设置媒体服务器的片单已跳过。</VAlert>
          <VAlert type="warning" variant="tonal" class="sync-empty-warning">{{ emptySyncLists.length ? '本次包含空片单，确认同步将清空其对应合集和播放列表中的内容。' : '空片单同步会清空对应合集和播放列表中的内容。' }}</VAlert>
          <VAlert v-if="error" type="error" variant="tonal" class="mt-3">{{ error }}</VAlert>
        </VCardText>
        <VCardActions class="dialog-actions">
          <VBtn @click="syncDialog = false">取消</VBtn>
          <VBtn :color="emptySyncLists.length ? 'error' : 'primary'" variant="tonal" @click="publish">确认同步</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>
    <VDialog :model-value="hasRunningJob" max-width="360" persistent :close-on-back="false" :no-click-animation="true" aria-label="同步进度">
      <VCard class="publish-progress" tabindex="0" @keydown.tab="keepProgressFocus">
        <VCardText class="publish-progress-body" role="status" aria-live="polite">
          <VProgressCircular indeterminate color="primary" :size="44" :width="4" aria-hidden="true" />
          <p class="publish-progress-label">{{ progressLabel }}</p>
          <template v-if="measuredProgress">
            <p class="publish-progress-context">{{ [measuredProgress.list_name, measuredProgress.server_id].filter(Boolean).join(' · ') }}</p>
            <p class="publish-progress-stage">{{ stageLabel }}</p>
            <VProgressLinear :model-value="measuredProgress.percent" color="primary" rounded :height="6" :aria-label="`已完成 ${measuredProgress.completed} / ${measuredProgress.total} 项任务`" />
            <p class="publish-progress-count">已完成 {{ measuredProgress.completed }} / {{ measuredProgress.total }} 项任务</p>
          </template>
          <template v-if="progressError"><p class="publish-progress-error" role="alert">{{ progressError }}</p><VBtn variant="tonal" color="primary" :loading="polling" :disabled="polling" @click="refreshJob">刷新进度</VBtn></template>
        </VCardText>
      </VCard>
    </VDialog>
  </div>
</template>

<style>
.task-status-success { color:#54c78a; }
.task-status-error { color:rgb(var(--v-theme-error)); }
.task-status-neutral { color:var(--list-muted,rgba(var(--v-theme-on-surface),.72)); }
.playlist-plugin .member-season { padding:0 16px 8px 60px; }
.playlist-plugin { --list-border:rgba(var(--v-theme-on-surface),.12); --list-muted:rgba(var(--v-theme-on-surface),.72); width:100%; max-width:1240px; margin:0 auto; padding:24px; color:rgb(var(--v-theme-on-surface)); }
.playlist-plugin h1 { font-size:28px; font-weight:650; letter-spacing:-.5px; margin:0 0 8px; }
.playlist-plugin h2 { font-size:20px; font-weight:600; margin:0; }
.playlist-plugin h3 { font-size:16px; font-weight:600; margin:0; }
.playlist-plugin p { font-size:14px; line-height:1.7; margin:4px 0 12px; }
.playlist-plugin .plugin-heading,.playlist-plugin .section-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
.playlist-plugin .plugin-heading { margin-bottom:28px; }
.playlist-plugin .plugin-heading > div,.playlist-plugin .section-heading > div { min-width:0; }
.playlist-plugin .plugin-heading p,.playlist-plugin .section-heading p,.playlist-plugin .muted { color:var(--list-muted); }
.playlist-plugin .plugin-heading p,.playlist-plugin .section-heading p { margin-bottom:0; }
.playlist-plugin .section-heading { margin-bottom:20px; }
.playlist-plugin .section-heading h2,.playlist-plugin .section-heading h3 { min-height:36px; display:flex; align-items:center; flex-wrap:wrap; }
.playlist-plugin .settings-section,.playlist-plugin .lists-section,.playlist-plugin .job-section,.playlist-plugin .history-section { padding:28px 0; border-top:1px solid var(--list-border); }
.playlist-plugin .settings-controls { display:grid; grid-template-columns:minmax(160px,1fr) minmax(0,2fr) auto; align-items:start; gap:20px; }
.playlist-plugin .setting-toggle,.playlist-plugin .setting-save,.playlist-plugin .editor-toggle { min-height:56px; display:flex; align-items:center; }
.playlist-plugin .setting-cron { min-width:0; }
.playlist-plugin .count { font-size:13px; font-weight:400; color:var(--list-muted); margin-left:8px; }
.playlist-plugin .action-row,.playlist-plugin .candidate-tools,.playlist-plugin .list-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.playlist-plugin .action-row { flex-shrink:0; }
.playlist-plugin .list-directory { border-top:1px solid var(--list-border); }
.playlist-plugin .list-entry { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:16px; padding:18px 0; border-bottom:1px solid var(--list-border); }
.playlist-plugin .list-summary { display:flex; align-items:center; gap:16px; min-width:0; color:inherit; text-align:start; cursor:pointer; border-radius:4px; }
.playlist-plugin .list-summary:focus-visible { outline:2px solid rgb(var(--v-theme-primary)); outline-offset:4px; }
.playlist-plugin .list-summary:disabled { cursor:default; opacity:.6; }
.playlist-plugin .list-state-pending .list-summary:disabled { opacity:1; }
.playlist-plugin .list-copy { min-width:0; display:block; }
.playlist-plugin .list-name { display:block; font-size:16px; font-weight:600; line-height:1.5; overflow-wrap:anywhere; }
.playlist-plugin .list-meta { display:block; font-size:13px; line-height:1.7; margin-top:4px; }
.playlist-plugin .list-actions { justify-content:flex-end; }
.playlist-plugin .list-state { min-width:124px; flex:0 0 auto; }
.playlist-plugin .list-state-error { grid-column:1/-1; color:rgb(var(--v-theme-error)); margin:0; }
.playlist-plugin .empty-state { padding:32px; text-align:center; background:rgba(var(--v-theme-on-surface),.025); border:1px dashed var(--list-border); border-radius:12px; }
.playlist-plugin .empty-state h3 { margin-top:12px; }
.playlist-plugin .bounded-list { max-height:350px; overflow:auto; border:1px solid var(--list-border); border-radius:8px; }
.playlist-plugin .media-item-title { display:block; white-space:normal; overflow-wrap:anywhere; line-height:1.5; padding:3px 0; }
.playlist-plugin .add-media,.playlist-plugin .target-editor { border:1px solid var(--list-border); padding:20px; border-radius:12px; margin-bottom:24px; }
.playlist-plugin .playlist-editor { border-top:1px solid var(--list-border); padding-top:20px; }
.playlist-plugin .source-row,.playlist-plugin .result-row { border-top:1px solid var(--list-border); padding:16px 0; }
.playlist-plugin .source-row { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }
.playlist-plugin .source-row > div { min-width:0; overflow-wrap:anywhere; }
.playlist-plugin .error-text { color:rgb(var(--v-theme-error)); }
.playlist-plugin.editor-card { padding:0; width:100%; }
.playlist-plugin .editor-body { padding:20px 24px; min-width:0; }
.playlist-plugin .editor-basics { display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:start; gap:24px; margin-bottom:20px; }
.playlist-plugin .editor-basics > div { min-width:0; }
.playlist-plugin .editor-tabs { margin-bottom:24px; }
.playlist-plugin .editor-actions { border-top:1px solid var(--list-border); padding:16px 24px; justify-content:flex-end; gap:12px; flex-wrap:wrap; }
.playlist-plugin .policy-option { padding:16px 0; border-bottom:1px solid var(--list-border); margin-bottom:16px; }
.playlist-plugin .policy-option:first-child { padding-top:0; }
.playlist-plugin .policy-option p { margin:8px 0 0; }
.sync-confirm-dialog { display:flex; flex-direction:column; max-height:calc(100dvh - 48px); }
.sync-confirm-dialog > .v-card-text { min-height:0; overflow:auto; }
.sync-confirm-dialog > .dialog-actions { flex:none; }
.sync-confirm-scope { margin:0 0 16px; font-size:14px; }
.sync-confirm-lists { display:grid; gap:16px; margin:0 0 20px; padding:0; list-style:none; max-height:min(40dvh,320px); overflow:auto; }
.sync-confirm-name { display:block; font-size:15px; line-height:1.6; overflow-wrap:anywhere; }
.sync-confirm-counts { margin:4px 0 0; font-size:13px; line-height:1.7; color:rgba(var(--v-theme-on-surface),.72); }
.sync-empty-label { display:inline-block; margin-left:8px; font-size:12px; color:rgb(var(--v-theme-warning)); }
.dialog-actions { justify-content:flex-end; gap:8px; padding:16px 24px; }
.publish-progress .publish-progress-body { display:flex; flex-direction:column; align-items:center; gap:20px; padding:32px 24px; text-align:center; }
.publish-progress .publish-progress-label { margin:0; font-size:16px; line-height:1.5; }
.publish-progress .publish-progress-context,.publish-progress .publish-progress-stage,.publish-progress .publish-progress-count { margin:0; max-width:100%; overflow-wrap:anywhere; line-height:1.6; }
.publish-progress .publish-progress-context,.publish-progress .publish-progress-count { color:rgba(var(--v-theme-on-surface),.72); font-size:13px; }
.publish-progress .publish-progress-stage { font-size:14px; }
.publish-progress .v-progress-linear { width:100%; flex:none; }
.publish-progress .publish-progress-error { margin:0; overflow-wrap:anywhere; color:rgb(var(--v-theme-error)); color:color-mix(in srgb, rgb(var(--v-theme-error)) 65%, rgb(var(--v-theme-on-surface)) 35%); font-size:14px; line-height:1.7; }
.publish-progress:focus-visible { outline:2px solid rgb(var(--v-theme-primary)); outline-offset:4px; }
@media (max-width:960px) {
  .playlist-plugin .settings-controls { grid-template-columns:minmax(0,1fr); gap:12px; }
  .playlist-plugin .setting-toggle,.playlist-plugin .setting-save,.playlist-plugin .editor-toggle { min-height:0; }
  .playlist-plugin .setting-save { justify-content:flex-end; }
  .playlist-plugin .editor-basics { grid-template-columns:minmax(0,1fr); gap:12px; }
}
@media (max-width:700px) {
  .playlist-plugin .list-entry { grid-template-columns:minmax(0,1fr); gap:12px; }
  .playlist-plugin .list-actions { width:100%; justify-content:flex-start; }
  .playlist-plugin .list-state { min-width:124px; }
}
@media (max-width:360px) {
  .playlist-plugin .list-actions { gap:6px; }
}
@media (max-width:600px) {
  .playlist-plugin .member-season { padding-inline:16px; }
  .playlist-plugin { padding:16px; }
  .playlist-plugin .plugin-heading,.playlist-plugin .section-heading { flex-wrap:wrap; gap:12px; }
  .playlist-plugin .section-heading > .action-row { width:100%; justify-content:flex-end; }
  .playlist-plugin .add-media,.playlist-plugin .target-editor { padding:16px; }
  .playlist-plugin .editor-body { padding:16px; }
  .playlist-plugin .editor-actions { padding:12px 16px; }
  .playlist-plugin .source-row { flex-wrap:wrap; gap:8px; }
  .playlist-plugin .source-row > div { width:100%; }
  .playlist-plugin .source-row > button { margin-left:auto; }
}
.playlist-plugin .import-dialog-title { display:flex; align-items:center; gap:4px; }
.playlist-plugin .import-list-preview { margin:12px 0; padding:16px; border:1px solid rgba(var(--v-theme-on-surface),.16); border-radius:12px; background:rgba(var(--v-theme-surface),.58); }
.playlist-plugin .import-list-preview:last-of-type { margin-bottom:0; }
.playlist-plugin .import-list-heading { display:grid; grid-template-columns:40px minmax(0,1fr); gap:12px; align-items:start; }
.playlist-plugin .import-list-copy { display:grid; gap:6px; min-width:0; }
.playlist-plugin .import-list-count { margin:0 0 0 12px; color:var(--list-muted); }
.playlist-plugin .import-details { margin:12px 0 0 52px; padding-top:10px; border-top:1px solid var(--list-border); color:var(--list-muted); }
.playlist-plugin .import-details summary { cursor:pointer; }
.playlist-plugin .import-details ol { max-height:240px; overflow:auto; margin:8px 0 0; padding-left:24px; }
.playlist-plugin .import-details li { padding-block:3px; overflow-wrap:anywhere; }
.playlist-plugin .import-output-summary { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin:12px 0 0 52px; color:var(--list-muted); font-size:14px; }
.playlist-plugin .import-output-summary strong { color:rgb(var(--v-theme-on-surface)); font-weight:500; }
.playlist-plugin .import-source-form { display:grid; gap:10px; }
.playlist-plugin .import-source-form > p { margin:0; }
.playlist-plugin .import-dialog .dialog-actions { justify-content:flex-end; }
.playlist-plugin .import-file-input { display:none; }
@media (max-width:600px) {
  .playlist-plugin .import-list-preview { padding:12px; }
  .playlist-plugin .import-details,.playlist-plugin .import-output-summary { margin-left:48px; }
}
</style>
