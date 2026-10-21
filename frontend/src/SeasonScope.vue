<script setup>
import { computed, onScopeDispose, ref, watch } from 'vue'

const props = defineProps({ tmdbId: { type: Number, required: true }, modelValue: { type: Array, default: null }, title: String, request: { type: Function, required: true }, disabled: Boolean })
const emit = defineEmits(['update:modelValue'])
const choices = ref([]), loading = ref(false), error = ref('')
const specific = computed(() => Array.isArray(props.modelValue))
const modes = [{ title: '全部季', value: 'all' }, { title: '指定季', value: 'selected' }]
const options = computed(() => {
  const values = new Map(choices.value.map(item => [item.season_number, { value: item.season_number,
    title: `${item.season_number === 0 ? '特别篇' : `第 ${item.season_number} 季`} · 共 ${item.episode_count} 集` }]))
  for (const number of props.modelValue || []) if (!values.has(number)) values.set(number, { value: number, title: number === 0 ? '特别篇' : `第 ${number} 季` })
  return [...values.values()].sort((a,b) => a.value - b.value)
})
let generation = 0
async function load() {
  const current = ++generation, id = props.tmdbId
  loading.value = true; error.value = ''
  try {
    const result = await props.request('post', 'seasons', { tmdb_id: id })
    if (current !== generation) return
    if (!Array.isArray(result.items) || !result.items.length) throw Error('未取得季信息，请重试。')
    choices.value = result.items
  } catch (failure) { if (current === generation) error.value = failure.message }
  finally { if (current === generation) loading.value = false }
}
function mode(value) {
  if (props.disabled) return
  emit('update:modelValue', value === 'selected' ? [] : null)
}
watch([() => props.tmdbId, specific], ([id, selected], old) => {
  if (!old || old[0] !== id) { generation++; choices.value = []; error.value = ''; loading.value = false }
  if (selected && !choices.value.length) load()
}, { immediate: true })
onScopeDispose(() => generation++)
</script>

<template>
  <div class="season-scope" role="group" :aria-label="`${title || '电视剧'}的季范围`">
    <div class="season-scope-fields">
      <div class="season-mode"><VSelect :model-value="specific ? 'selected' : 'all'" :items="modes" label="电视剧范围" density="compact" hide-details :mobile-layout="false" :disabled="disabled" @update:model-value="mode" /></div>
      <div v-if="specific" class="season-selection"><VSelect :model-value="modelValue" :items="options" label="选择季（可多选）" multiple chips closable-chips density="compact" hide-details :mobile-layout="false" :loading="loading" :disabled="disabled || loading" :error="!modelValue.length" @update:model-value="value => !disabled && emit('update:modelValue',[...value].sort((a,b) => a-b))" /></div>
    </div>
    <div v-if="specific && (!modelValue.length || error)" class="season-scope-message" role="status">
      <span>{{ error || (loading ? '正在获取季信息…' : '请至少选择一季。') }}</span><VBtn v-if="error" size="small" variant="text" :disabled="disabled || loading" @click="load">重试</VBtn>
    </div>
    <p v-if="specific" class="season-scope-help">仅所选季参与合集、播放列表和自动订阅。</p>
  </div>
</template>

<style scoped>
.season-scope { min-width:0; margin:12px 0; }
.season-scope-fields { display:flex; flex-wrap:wrap; gap:12px; align-items:start; }
.season-mode { flex:0 1 160px; min-width:130px; }
.season-selection { flex:1 1 230px; min-width:0; }
.season-scope-message { display:flex; gap:8px; align-items:center; color:rgb(var(--v-theme-error)); font-size:13px; margin-top:8px; }
.season-scope-help { color:rgba(var(--v-theme-on-surface),.75); font-size:13px; line-height:1.5; margin:8px 0 0; }
@media (max-width:600px) { .season-mode,.season-selection { flex-basis:100%; } }
</style>
