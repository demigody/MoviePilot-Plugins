<script setup>
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { mediaKey, mediaSubtitle, mediaTitle, moveOrder, normaliseOrder } from './domain'

const props = defineProps({ playlist: { type: Object, required: true }, members: { type: Array, required: true }, disabled: Boolean })
const query = ref(''), selectedKey = ref(''), destination = ref(''), feedback = ref(''), invalid = ref(false)
const scroller = ref(null), drag = ref(null), dropTarget = ref(null)
const byKey = computed(() => new Map(props.members.map(member => [mediaKey(member), member])))
const sequence = computed(() => normaliseOrder(props.playlist.order, props.members))
const searchText = value => String(value || '').normalize('NFKC').replace(/第\s*(\d+)\s*季/g, '第$1季').trim().toLowerCase()
const terms = computed(() => searchText(query.value).split(/\s+/).filter(Boolean))
const rows = computed(() => sequence.value.map((key, position) => ({ key, position, member: byKey.value.get(key) })).filter(row => {
  const text = searchText(`${mediaTitle(row.member)} ${mediaSubtitle(row.member)}`)
  return terms.value.every(term => text.includes(term))
}))
const position = computed(() => sequence.value.indexOf(selectedKey.value))
const selectedTitle = computed(() => position.value >= 0 ? mediaTitle(byKey.value.get(selectedKey.value)) : '')
const canMove = computed(() => !props.disabled && position.value >= 0)
let alive = true, frame = null, pointer = null, pendingTarget = null, lastFrame = 0

function cancelScroll() {
  if (frame !== null) cancelAnimationFrame(frame)
  frame = null; lastFrame = 0
}
function endDrag() { cancelScroll(); pointer = null; pendingTarget = null; drag.value = null; setDropTarget(null) }
function setDropTarget(target) {
  if (dropTarget.value?.key === target?.key && dropTarget.value?.after === target?.after) return
  dropTarget.value = target
}
function resetFeedback() { feedback.value = ''; invalid.value = false }
function clearSelection() { selectedKey.value = ''; destination.value = ''; resetFeedback() }
watch(sequence, keys => {
  if (selectedKey.value && !keys.includes(selectedKey.value)) clearSelection()
  else if (selectedKey.value) destination.value = String(keys.indexOf(selectedKey.value) + 1)
  if (drag.value && !keys.includes(drag.value)) endDrag()
})
watch(query, () => {
  endDrag(); resetFeedback()
  if (selectedKey.value && !rows.value.some(row => row.key === selectedKey.value)) clearSelection()
})
watch(() => props.disabled, disabled => { if (disabled) endDrag() })
onScopeDispose(() => { alive = false; endDrag() })

function select(key) {
  if (props.disabled || !sequence.value.includes(key)) return
  selectedKey.value = key; destination.value = String(sequence.value.indexOf(key) + 1); resetFeedback()
}
async function revealSelected() {
  await nextTick()
  if (!alive || !scroller.value) return
  const container = scroller.value
  const row = [...container.querySelectorAll('[data-order-key]')].find(element => element.dataset.orderKey === selectedKey.value)
  if (!row) return
  // Scroll only the list, never the surrounding editor or its fixed controls.
  const bounds = container.getBoundingClientRect(), item = row.getBoundingClientRect()
  const top = bounds.top + container.clientTop, bottom = top + container.clientHeight
  if (item.top < top) container.scrollTop += item.top - top
  else if (item.bottom > bottom) container.scrollTop += item.bottom - bottom
}
function move(key, to) {
  const from = sequence.value.indexOf(key)
  if (props.disabled || from < 0 || !Number.isInteger(to) || to < 0 || to >= sequence.value.length) return
  selectedKey.value = key
  props.playlist.order = moveOrder(sequence.value, from, to)
  destination.value = String(to + 1); invalid.value = false
  feedback.value = ''
  revealSelected()
}
function jump() {
  if (!canMove.value) return
  const value = String(destination.value || '').normalize('NFKC').trim()
  const target = Number(value)
  if (!/^[1-9]\d*$/.test(value) || !Number.isSafeInteger(target) || target > sequence.value.length) {
    invalid.value = true; feedback.value = `请输入 1–${sequence.value.length} 之间的整数。`; return
  }
  move(selectedKey.value, target - 1)
}
function startDrag(key, event) {
  if (props.disabled) { event.preventDefault(); return }
  select(key); endDrag(); drag.value = key
  event.dataTransfer?.setData('text/plain', key)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}
function findDropTarget(event) {
  const container = scroller.value
  if (!container) return
  let element = event?.target?.closest?.('[data-order-key]')
  if (!element || !container.contains(element)) {
    element = pointer && document.elementFromPoint?.(pointer.x, pointer.y)?.closest('[data-order-key]')
  }
  if (!element || !container.contains(element)) {
    const visible = container.querySelectorAll('[data-order-key]')
    if (!visible.length || !pointer) { setDropTarget(null); return }
    const first = visible[0], last = visible[visible.length - 1]
    if (pointer.y <= first.getBoundingClientRect().top) { setDropTarget({ key: first.dataset.orderKey, after: false }); return }
    if (pointer.y >= last.getBoundingClientRect().bottom) { setDropTarget({ key: last.dataset.orderKey, after: true }); return }
    setDropTarget(null); return
  }
  const box = element.getBoundingClientRect()
  setDropTarget({ key: element.dataset.orderKey, after: pointer.y > box.top + box.height / 2 })
}
function scrollSpeed() {
  if (!pointer || !scroller.value) return 0
  const box = scroller.value.getBoundingClientRect()
  if (box.height <= 0 || pointer.x < box.left || pointer.x > box.right || pointer.y < box.top || pointer.y > box.bottom) return 0
  const edge = Math.min(64, box.height / 3)
  if (pointer.y < box.top + edge) return -18 * (box.top + edge - pointer.y) / edge
  if (pointer.y > box.bottom - edge) return 18 * (pointer.y - box.bottom + edge) / edge
  return 0
}
function scrollFrame(time) {
  frame = null
  if (!alive || !drag.value || props.disabled || !scroller.value) return
  const speed = scrollSpeed()
  const container = scroller.value, previous = container.scrollTop
  const elapsed = lastFrame ? Math.min(32, time - lastFrame) : 16
  lastFrame = time
  if (speed) container.scrollTop = Math.max(0, Math.min(container.scrollHeight - container.clientHeight, previous + speed * elapsed / 16))
  // Consume the newest drag event once per frame, including in the middle of
  // the list. Auto-scroll must hit-test again after the rows have moved.
  findDropTarget(container.scrollTop === previous && pendingTarget ? { target: pendingTarget } : undefined)
  pendingTarget = null
  if (container.scrollTop !== previous) frame = requestAnimationFrame(scrollFrame)
  else lastFrame = 0
}
function dragOver(event) {
  if (!drag.value || props.disabled) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  pointer = { x: event.clientX, y: event.clientY }
  pendingTarget = event.target
  if (frame === null) frame = requestAnimationFrame(scrollFrame)
}
function dragLeave(event) {
  const box = scroller.value?.getBoundingClientRect()
  if (box && (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom)) {
    cancelScroll(); pointer = null; pendingTarget = null; setDropTarget(null)
  }
}
function drop(event) {
  if (!drag.value || props.disabled) { endDrag(); return }
  event.preventDefault()
  pointer = { x: event.clientX, y: event.clientY }; findDropTarget(event)
  const key = drag.value, target = dropTarget.value
  if (target) {
    const from = sequence.value.indexOf(key), anchor = sequence.value.indexOf(target.key)
    if (from >= 0 && anchor >= 0) {
      const insertion = anchor + (target.after ? 1 : 0)
      move(key, Math.max(0, Math.min(sequence.value.length - 1, insertion - (from < insertion ? 1 : 0))))
    }
  }
  endDrag()
}
</script>

<template>
  <section class="playlist-sorter" :data-dragging="!!drag" :aria-label="`${playlist.name || '播放列表'}排序工具`">
    <div class="sort-tools">
      <div class="sort-filter"><VTextField v-model="query" label="查找列表内影片" placeholder="片名、年份或 TMDB ID" prepend-inner-icon="mdi-magnify" clearable hide-details density="compact" :mobile-layout="false" :disabled="disabled" /><span class="sort-count">{{ terms.length ? `显示 ${rows.length} / ${sequence.length} 项` : `共 ${sequence.length} 项` }}</span><VBtn v-if="terms.length" size="small" variant="text" :disabled="disabled" @click="query = ''">显示全部</VBtn></div>
      <div class="sort-selection"><span class="sort-selected-title" :title="selectedTitle">{{ selectedTitle || '先点击影片选择，再调整顺序' }}</span><span v-if="position >= 0" class="sort-current">第 {{ position + 1 }} / {{ sequence.length }} 位</span></div>
      <div class="sort-actions">
        <div class="sort-step-actions"><VBtn size="small" variant="tonal" :disabled="!canMove || position === 0" @click="move(selectedKey,position-1)">上移</VBtn><VBtn size="small" variant="tonal" :disabled="!canMove || position === sequence.length-1" @click="move(selectedKey,position+1)">下移</VBtn><VBtn size="small" variant="text" :disabled="!canMove || position === 0" @click="move(selectedKey,0)">置顶</VBtn><VBtn size="small" variant="text" :disabled="!canMove || position === sequence.length-1" @click="move(selectedKey,sequence.length-1)">置底</VBtn></div>
        <div class="sort-jump"><VTextField v-model="destination" label="移到第几位" inputmode="numeric" density="compact" hide-details :mobile-layout="false" :error="invalid" :disabled="!canMove" @update:model-value="resetFeedback" @keyup.enter="jump" /><VBtn size="small" variant="tonal" :disabled="!canMove" @click="jump">移动</VBtn></div>
      </div>
      <div v-if="invalid" class="sort-status sort-error" role="status" aria-live="polite">{{ feedback }}</div>
    </div>
    <div ref="scroller" class="sort-scroll playlist-order" role="list" :aria-label="`${playlist.name || '播放列表'}内容`" @dragenter="dragOver" @dragover="dragOver" @dragleave="dragLeave" @drop="drop">
      <div v-for="row in rows" :key="row.key" v-memo="[mediaTitle(row.member), mediaSubtitle(row.member), row.position, disabled, selectedKey === row.key, dropTarget?.key === row.key ? dropTarget.after : null]" class="sort-order-row" :class="{ 'sort-selected': selectedKey === row.key, 'sort-drop-before': dropTarget?.key === row.key && !dropTarget.after, 'sort-drop-after': dropTarget?.key === row.key && dropTarget.after }" :data-order-key="row.key" role="listitem">
        <VBtn class="sort-drag-handle" :data-drag-key="row.key" icon="mdi-drag" size="28" variant="text" :draggable="!disabled" :disabled="disabled" :aria-label="`拖动 ${mediaTitle(row.member)}`" title="拖动排序，靠近列表边缘可自动滚动" @dragstart="startDrag(row.key,$event)" @dragend="endDrag" @click="select(row.key)" />
        <span class="sort-position">{{ row.position + 1 }}</span>
        <button type="button" class="sort-item-select" :aria-pressed="selectedKey === row.key" :disabled="disabled" @click="select(row.key)"><span class="sort-item-title">{{ mediaTitle(row.member) }}</span><span class="sort-item-subtitle">{{ mediaSubtitle(row.member) }}</span></button>
        <div class="sort-row-actions"><VBtn icon="mdi-arrow-up" variant="text" size="32" :disabled="disabled || row.position === 0" :aria-label="`上移 ${mediaTitle(row.member)}`" @click="move(row.key,row.position-1)" /><VBtn icon="mdi-arrow-down" variant="text" size="32" :disabled="disabled || row.position === sequence.length-1" :aria-label="`下移 ${mediaTitle(row.member)}`" @click="move(row.key,row.position+1)" /></div>
      </div>
      <p v-if="!rows.length" class="sort-empty">{{ sequence.length ? '没有找到匹配的影片。试试其他片名、年份或编号。' : '添加内容后即可调整播放顺序。' }}</p>
    </div>
  </section>
</template>

<style scoped>
.playlist-sorter { min-width:0; border:1px solid var(--list-border,rgba(var(--v-theme-on-surface),.12)); border-radius:10px; }
.sort-tools { position:sticky; top:0; z-index:2; padding:16px; border-radius:10px 10px 0 0; background:rgb(var(--v-theme-surface)); border-bottom:1px solid var(--list-border,rgba(var(--v-theme-on-surface),.12)); }
.sort-filter { display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px; }
.sort-filter > :first-child { flex:1 1 240px; min-width:0; }
.sort-count,.sort-current { white-space:nowrap; font-size:13px; color:var(--list-muted); }
.sort-selection { display:flex; align-items:baseline; justify-content:space-between; gap:12px; min-height:24px; margin:12px 0 8px; }
.sort-selected-title { min-width:0; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; font-size:14px; }
.sort-actions,.sort-step-actions { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
.sort-actions { justify-content:space-between; gap:12px; }
.sort-jump { display:grid; grid-template-columns:minmax(100px,160px) auto; align-items:center; gap:8px; }
.sort-status { min-height:20px; margin-top:10px; font-size:13px; line-height:20px; color:var(--list-muted); }
.sort-error { color:rgb(var(--v-theme-error)); }
.sort-scroll { max-height:360px; overflow:auto; overscroll-behavior:contain; border-radius:0 0 10px 10px; }
.sort-order-row { position:relative; display:grid; grid-template-columns:28px 24px minmax(0,1fr) auto; align-items:center; gap:6px; padding:10px 12px; border-bottom:1px solid var(--list-border,rgba(var(--v-theme-on-surface),.12)); }
.sort-order-row:last-child { border-bottom:0; }
.sort-selected { background:rgba(var(--v-theme-primary),.10); }
.sort-position { text-align:center; font-size:13px; color:var(--list-muted); font-variant-numeric:tabular-nums; }
.sort-drag-handle { cursor:grab; }
.sort-item-select { min-width:0; width:100%; padding:2px 0; text-align:start; color:inherit; cursor:pointer; border-radius:3px; }
.sort-item-select:focus-visible { outline:2px solid rgb(var(--v-theme-primary)); outline-offset:3px; }
.sort-item-select:disabled { cursor:default; }
.sort-item-title,.sort-item-subtitle { display:block; white-space:normal; overflow-wrap:anywhere; line-height:1.5; }
.sort-item-title { font-size:14px; }
.sort-item-subtitle { margin-top:4px; font-size:12px; color:var(--list-muted); }
.sort-row-actions { display:flex; }
.sort-drop-before::before,.sort-drop-after::after { content:''; position:absolute; left:0; right:0; border-top:2px solid rgb(var(--v-theme-primary)); pointer-events:none; }
.sort-drop-before::before { top:0; }
.sort-drop-after::after { bottom:0; }
.sort-empty { padding:20px; color:var(--list-muted); }
@media (max-width:600px) {
  .sort-tools { padding:12px; }
  .sort-filter > :first-child { flex-basis:100%; }
  .sort-actions,.sort-jump { width:100%; }
  .sort-step-actions { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); width:100%; gap:4px; }
  .sort-step-actions > button { min-width:0; padding-inline:8px; }
  .sort-jump { grid-template-columns:minmax(0,1fr) auto; }
  .sort-scroll { max-height:320px; }
  .sort-order-row { grid-template-columns:28px 22px minmax(0,1fr) 32px; gap:4px; padding:10px 8px; }
  .sort-row-actions { flex-direction:column; }
}
</style>
