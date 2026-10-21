<script setup>
import { computed } from 'vue'
import { mediaKey, mediaTitle } from './domain'
import { errorMessage } from './api'

const props = defineProps({ result: { type: Object, required: true }, members: { type: Array, default: () => [] }, section: { type: String, default: 'auto' } })
const section = computed(() => props.section === 'auto' ? (props.result.operation === 'subscribe' ? 'subscribe' : props.result.operation === 'follow' || props.result.source_id ? 'source' : 'media') : props.section)
const pending = computed(() => props.result.pending || props.result.pending_keys || [])
const memberTitles = computed(() => new Map(props.members.map(member => [mediaKey(member), mediaTitle(member)])))
const title = key => {
  const parts = String(key || '').split(':'), parent = parts.slice(0,2).join(':')
  const name = memberTitles.value.get(parent) || `${parts[0] === 'tv' ? '电视剧' : '电影'} TMDB ${parts[1] || key}`
  return parts[2] === 'season' ? `${name} · ${parts[3] === '0' ? '特别篇' : `第 ${parts[3]} 季`}` : name
}
const rowTitle = computed(() => section.value === 'subscribe' ? `订阅内容：${title(props.result.key)}`
  : section.value === 'source' ? `TMDB 合集 ${props.result.source_id || ''}` : '')
const noLibraryMatches = computed(() => props.result.error_code === 'no_matches')
const completedWithoutError = computed(() => noLibraryMatches.value || props.result.status === 'partial' && !props.result.error && !props.result.error_code)
const state = computed(() => completedWithoutError.value ? '完成' : ({ success:'完成', partial:'部分成功', error:'失败', cancelled:'已取消', running:'执行中', queued:'等待中', waiting:'等待媒体库识别', skipped:'已跳过', subscribed:'已订阅' }[props.result.status] || (props.result.success === false ? '失败' : '完成')))
const stateKind = computed(() => completedWithoutError.value || ['success', 'subscribed'].includes(props.result.status) || (!props.result.status && props.result.success !== false)
  ? 'success' : ['partial', 'error', 'cancelled'].includes(props.result.status) || props.result.success === false ? 'error' : 'neutral')
const added = computed(() => props.result.added || [])
const removed = computed(() => props.result.removed || [])
const libraryNames = computed(() => props.result.library_names || [])
const plans = computed(() => {
  const configuredCount = props.result.configured_count ?? props.members.length
  if (props.result.matched_count === 0 && configuredCount > 0) return []
  return props.result.plan || []
})
function planAction(plan) {
  const name = plan.kind === 'playlist' ? '播放列表' : '合集'
  if (plan.missing_remote_id) return plan.create ? `原${name}已从媒体库中删除，将重新创建` : `原${name}已从媒体库中删除，目前没有可同步的内容`
  return plan.create ? `将在媒体库中新建${name}` : `更新媒体库中已有的${name}`
}
</script>

<template>
  <div class="result-row" :class="`result-${section}`">
    <div class="result-heading">
      <strong>{{ section === 'media' ? (result.target_name || result.server_id || '媒体服务器') : rowTitle }}</strong>
      <span class="result-state" :class="`result-state-${stateKind}`">{{ state }}{{ result.dry_run ? ' · 预览' : '' }}</span>
    </div>
    <p v-if="section === 'media' && libraryNames.length">媒体库：{{ libraryNames.join('、') }}</p>
    <p v-if="section === 'media' && result.matched_count !== undefined">匹配到 {{ result.matched_count }} / {{ result.configured_count ?? members.length }} 项<span v-if="pending.length"> · 未匹配 {{ pending.length }} 项</span></p>
    <div v-if="section === 'media' && (added.length || removed.length || pending.length)" class="result-impact">
      <strong>影响范围</strong>
      <p v-if="added.length">新增 {{ added.length }} 项：{{ added.map(title).join('、') }}</p>
      <p v-if="removed.length">移除 {{ removed.length }} 项：{{ removed.map(title).join('、') }}</p>
      <p v-if="pending.length">未匹配 {{ pending.length }} 项：{{ pending.map(title).join('、') }}</p>
    </div>
    <p v-if="result.error && !noLibraryMatches" class="error-text" role="alert">{{ errorMessage(result.error) }}</p>
    <div v-if="result.playback_pending?.length" class="pending-content"><strong>尚无可播放的已入库剧集：</strong>{{ result.playback_pending.map(title).join('、') }}。后续入库后再同步即可。</div>
    <div v-for="(plan,index) in plans" :key="plan.id || index" class="publish-plan">
      <strong>{{ plan.kind === 'playlist' ? '播放列表' : '合集' }} · {{ plan.name }}</strong>
      <p>{{ planAction(plan) }} · 新增 {{ plan.add?.length || 0 }} 项 · 移除 {{ plan.remove?.length || 0 }} 项<span v-if="plan.kind === 'playlist'"> · 播放顺序 {{ plan.order?.length || 0 }} 项</span></p>
    </div>
  </div>
</template>

<style scoped>
.result-row { overflow-wrap:anywhere; min-width:0; }
.result-heading { display:flex; align-items:baseline; flex-wrap:wrap; gap:8px 12px; margin-bottom:8px; }
.result-heading strong { overflow-wrap:anywhere; }
.result-state { font-size:13px; }
.result-state-success { color:#54c78a; }
.result-state-error { color:rgb(var(--v-theme-error)); }
.result-state-neutral { color:var(--list-muted); }
.result-impact { padding:9px 12px; margin:8px 0 10px; border-left:2px solid rgba(var(--v-theme-primary),.55); background:rgba(var(--v-theme-on-surface),.035); border-radius:4px; }
.result-impact p { margin:4px 0; }
.pending-content { font-size:13px; line-height:1.7; color:var(--list-muted); overflow-wrap:anywhere; margin-bottom:10px; }
.publish-plan { padding:10px 14px; margin:8px 0; background:rgba(var(--v-theme-on-surface),.035); border-radius:6px; }
.publish-plan p { margin-bottom:0; }
</style>
