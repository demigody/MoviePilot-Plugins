<script setup>
import { computed } from 'vue'
import RunResult from './RunResult.vue'
import { errorMessage } from './api'
import { mediaKey, mediaTitle } from './domain'

const props = defineProps({ results: { type: Array, default: () => [] }, membersFor: { type: Function, required: true } })
const lists = computed(() => {
  const grouped = new Map()
  for (const [index, result] of props.results.entries()) {
    const key = String(result.list_id || result.list_name || `unknown-${index}`)
    if (!grouped.has(key)) grouped.set(key, { key, name: result.list_name || '片单', media: [], sources: [], subscriptions: [] })
    const group = grouped.get(key)
    if (result.operation === 'subscribe') group.subscriptions.push({ result, index })
    else if (result.operation === 'follow' || result.source_id) group.sources.push({ result, index })
    else group.media.push({ result, index })
  }
  return [...grouped.values()].map(list => ({ ...list, outcome: outcomeFor(list) }))
})
const resultStatuses = {
  active: new Set(['queued', 'running', 'waiting']),
  successful: new Set(['success', 'subscribed', 'skipped']),
}
function outcomeFor(list) {
  const allResults = [...list.media, ...list.sources, ...list.subscriptions].map(entry => entry.result)
  const results = allResults.map(result => result.error_code === 'no_matches'
    ? { ...result, success: true, status: 'success', error: null } : result)
  if (!results.length) return allResults.length ? { label: '尚未入库', kind: 'neutral' } : { label: '无结果', kind: 'empty' }
  if (results.some(result => resultStatuses.active.has(result.status))) {
    return { label: results.some(result => result.status === 'waiting') ? '等待媒体库识别' : '执行中', kind: 'active' }
  }

  const succeeded = results.some(result => result.success === true || resultStatuses.successful.has(result.status))
  const failed = results.some(result => result.status === 'error' || result.status === 'cancelled'
    || Boolean(result.error) || Boolean(result.error_code && result.error_code !== 'no_matches'))
  if (succeeded && failed) return { label: '部分成功', kind: 'partial' }
  if (failed) return { label: '失败', kind: 'failed' }
  return { label: '成功', kind: 'success' }
}
const subscriptionTypes = entries => {
  const groups = new Map()
  for (const entry of entries) {
    const kind = entry.result.media_type || String(entry.result.key || '').split(':')[0]
    const label = kind === 'movie' ? '电影' : kind === 'tv' ? '电视剧' : '其他'
    if (!groups.has(label)) groups.set(label, [])
    groups.get(label).push(entry)
  }
  return [...groups].map(([label, rows]) => ({ label, count: rows.length, summaries: subscriptionSummaries(rows) }))
}
function subscriptionSummaries(entries) {
  const groups = new Map()
  for (const { result } of entries) {
    let label = '失败', kind = 'error'
    if (result.pending_download_state === 'queued') {
      label = '等待下载开始'; kind = 'neutral'
    } else if (result.pending_download_state === 'downloading') {
      label = '下载中'; kind = 'neutral'
    } else if (result.pending_download_state === 'downloaded') {
      label = '下载完成，等待入库'; kind = 'neutral'
    } else if (result.status === 'subscribed' || (result.success === true && !['partial', 'error', 'cancelled'].includes(result.status))) {
      label = result.already_exists ? '已在订阅中' : '已订阅'; kind = 'success'
    } else if (result.status === 'queued') {
      label = '等待中'; kind = 'neutral'
    } else if (result.status === 'running') {
      label = '执行中'; kind = 'neutral'
    } else if (result.status === 'waiting') {
      label = '等待媒体库识别'; kind = 'neutral'
    } else if (result.status === 'skipped') {
      label = '已跳过'; kind = 'neutral'
    } else if (result.status === 'partial') {
      label = '部分成功'; kind = 'error'
    }
    if (!groups.has(label)) groups.set(label, { label, kind, items: [] })
    const parts = String(result.key || '').split(':'), parentKey = parts.slice(0, 2).join(':')
    const member = (props.membersFor(result) || []).find(item => mediaKey(item) === parentKey)
    const baseTitle = member ? mediaTitle(member) : `${parts[0] === 'tv' ? '电视剧' : '电影'} TMDB ${parts[1] || result.key || ''}`
    const title = parts[2] === 'season' ? `${baseTitle} · ${parts[3] === '0' ? '特别篇' : `第 ${parts[3]} 季`}` : baseTitle
    const error = result.error ? `（${errorMessage(result.error)}）` : ''
    groups.get(label).items.push(`${title}${error}`)
  }
  return [...groups.values()].map(group => ({ ...group, text: group.items.join('、') }))
}
</script>

<template>
  <div class="run-results">
    <details v-for="list in lists" :key="list.key" class="run-list-result">
      <summary class="run-list-summary">
        <span class="run-list-title">{{ list.name }}</span>
        <span class="run-list-status" :class="list.outcome.kind === 'success' ? 'run-status-success' : list.outcome.kind === 'neutral' ? 'run-status-neutral' : 'run-status-error'">{{ list.outcome.label }}</span>
      </summary>
      <div class="run-list-content">
        <section v-if="list.media.length" class="result-module">
          <h4 class="result-module-title">媒体库</h4>
          <RunResult v-for="entry in list.media" :key="entry.index" :result="entry.result" :members="membersFor(entry.result)" section="media" />
        </section>
        <section v-if="list.sources.length" class="result-module">
          <h4 class="result-module-title">合集更新</h4>
          <RunResult v-for="entry in list.sources" :key="entry.index" :result="entry.result" :members="membersFor(entry.result)" section="source" />
        </section>
        <section v-if="list.subscriptions.length" class="result-module">
          <h4 class="result-module-title">订阅</h4>
          <div v-for="group in subscriptionTypes(list.subscriptions)" :key="group.label" class="subscription-category">
            <h5 class="result-category-title">{{ group.label }}订阅 · {{ group.count }} 项</h5>
            <p v-for="summary in group.summaries" :key="summary.label" class="subscription-summary" :class="`subscription-${summary.kind}`">
              <strong>{{ summary.label }}：</strong>{{ summary.text }}
            </p>
          </div>
        </section>
      </div>
    </details>
  </div>
</template>

<style scoped>
.run-list-result { min-width:0; padding:12px 0; border-top:1px solid var(--list-border,rgba(var(--v-theme-on-surface),.12)); }
.run-list-summary { display:flex; align-items:center; flex-wrap:wrap; gap:8px 14px; min-width:0; cursor:pointer; list-style:none; }
.run-list-summary::-webkit-details-marker { display:none; }
.run-list-summary::before { content:'›'; flex:0 0 auto; font-size:24px; line-height:1; transition:transform .15s ease; }
.run-list-result[open] > .run-list-summary::before { transform:rotate(90deg); }
.run-list-title { min-width:0; font-size:17px; font-weight:650; overflow-wrap:anywhere; }
.run-list-status { flex:0 0 auto; padding:3px 9px; border-radius:999px; font-size:12px; line-height:1.5; background:rgba(var(--v-theme-on-surface),.08); }
.run-status-success { color:#54c78a; background:rgba(46,160,96,.14); }
.run-status-error { color:rgb(var(--v-theme-error)); background:rgba(var(--v-theme-error),.12); }
.run-status-neutral { color:var(--list-muted,rgba(var(--v-theme-on-surface),.72)); background:rgba(var(--v-theme-on-surface),.08); }
.run-list-content { min-width:0; padding-top:8px; }
.result-module { min-width:0; margin:10px 0 14px 12px; padding-left:14px; border-left:2px solid rgba(var(--v-theme-primary),.28); }
.result-module-title { margin:0 0 10px; font-size:15px; font-weight:600; }
.result-category-title { margin:12px 0 8px; color:var(--list-muted,rgba(var(--v-theme-on-surface),.72)); font-size:13px; font-weight:600; }
.subscription-summary { margin:4px 0 8px; overflow-wrap:anywhere; }
.subscription-success { color:#54c78a; }
.subscription-error { color:rgb(var(--v-theme-error)); }
.subscription-neutral { color:var(--list-muted,rgba(var(--v-theme-on-surface),.72)); }
.subscription-summary strong { margin-inline-end:4px; }
.result-row + .result-row { margin-top:14px; padding-top:12px; border-top:1px dashed var(--list-border,rgba(var(--v-theme-on-surface),.12)); }
</style>
