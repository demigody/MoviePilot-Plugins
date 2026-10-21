import { onScopeDispose, ref } from 'vue'
import { PluginError, statusMessage } from './api'
import { uid, uniqueCandidates, mediaKey } from './domain'

// Each run has a distinct identity. Cancellation invalidates that identity before
// signalling the backend, so providers that cannot abort never restore old results.
export function useCandidates(request, excludeKeys, { timeoutMs = 300000 } = {}) {
  const status = ref('idle'), message = ref(''), items = ref([]), source = ref(null), errors = ref([])
  let current = null
  function invalidate() {
    if (!current) return
    const active = current
    current = null
    clearTimeout(active.timer)
    active.abort.abort()
    active.cancel()
    request('post', 'cancel', { request_id: active.id }, { timeout: 4000 }).catch(() => {})
  }
  function cancel() {
    invalidate()
    status.value = 'cancelled'; message.value = statusMessage('cancelled')
  }
  function reset() {
    invalidate()
    status.value = 'idle'; message.value = ''; items.value = []; source.value = null; errors.value = []
  }
  async function run(endpoint, payload) {
    invalidate()
    const active = { id: uid(), abort: new AbortController(), cancel: () => {} }
    current = active
    status.value = 'loading'; message.value = '正在查询…'; items.value = []; source.value = null; errors.value = []
    const cancelled = new Promise((_, reject) => { active.cancel = () => reject(new PluginError(statusMessage('cancelled'), 'cancelled')) })
    const timeout = new Promise((_, reject) => { active.timer = setTimeout(() => reject(new PluginError(statusMessage('timeout'), 'timeout')), timeoutMs) })
    try {
      const result = await Promise.race([request('post', endpoint, {
        ...payload, request_id: active.id, exclude_keys: excludeKeys(),
      }, { signal: active.abort.signal, timeout: timeoutMs }), timeout, cancelled])
      if (current !== active) return
      items.value = uniqueCandidates(result.items || [], excludeKeys())
      source.value = result.source || null
      errors.value = result.errors || []
      status.value = items.value.length ? 'success' : 'empty'
      const addable = items.value.filter(mediaKey).length
      message.value = items.value.length ? `找到 ${items.value.length} 项结果，${addable} 项可添加。` : statusMessage('empty')
    } catch (error) {
      if (current !== active) return
      errors.value = error.detail?.errors || []
      status.value = error.status || 'error'; message.value = error.message || statusMessage(status.value)
      if (status.value === 'timeout') {
        active.abort.abort()
        request('post', 'cancel', { request_id: active.id }, { timeout: 4000 }).catch(() => {})
      }
    } finally {
      clearTimeout(active.timer)
      if (current === active) current = null
    }
  }
  onScopeDispose(invalidate)
  return { status, message, items, source, errors, run, cancel, reset }
}
