import { effectScope } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createClient } from '../src/api'
import { useCandidates } from '../src/useCandidates'
const movie = { tmdb_id: 10, media_type: 'movie', title: '电影' }
const series = { tmdb_id: 10, media_type: 'tv', title: '电视剧' }
const scopes = []
function create(post, options = {}) {
  const api = { post }, scope = effectScope()
  const candidates = scope.run(() => useCandidates(createClient(api), () => ['movie:10'], options))
  scopes.push(scope)
  return candidates
}
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()); vi.useRealTimers() })
describe('candidate requests', () => {
  it('excludes already selected library media and keeps equal-id Series', async () => {
    const post = vi.fn().mockResolvedValue({ success: true, items: [movie, series] })
    const c = create(post)
    await c.run('library-media', { server_id: 'Emby', media_type: 'tv' })
    expect(c.items.value).toEqual([series]); expect(c.status.value).toBe('success')
    expect(post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/library-media', expect.objectContaining({ exclude_keys: ['movie:10'], media_type: 'tv' }), expect.anything())
  })
  it('shows empty results as empty rather than failure', async () => {
    const c = create(vi.fn().mockResolvedValue({ success: true, items: [] }))
    await c.run('search', { query: '未知' })
    expect(c.status.value).toBe('empty'); expect(c.message.value).toContain('没有找到')
  })
  it('keeps source-native results visible without fabricating TMDB identities', async () => {
    const unresolved = { media_source: 'douban', media_id: '9', media_type: 'movie', title: '未关联影片' }
    const c = create(vi.fn().mockResolvedValue({ success: true, items: [movie, unresolved, unresolved, series] }))
    await c.run('search', { query: '片' })
    expect(c.items.value).toEqual([unresolved, series])
    expect(c.status.value).toBe('success')
  })
  it.each(['cooldown', 'busy', 'provider_error'])('distinguishes %s from empty', async status => {
    const c = create(vi.fn().mockResolvedValue({ success: false, status, error: `反馈 ${status}` }))
    await c.run('search', { query: 'x' })
    expect(c.status.value).toBe(status); expect(c.message.value).toContain(status)
  })
  it('times out even when a provider never resolves and signals backend cancellation', async () => {
    vi.useFakeTimers()
    const post = vi.fn((path) => path.endsWith('/cancel') ? Promise.resolve({ success: true }) : new Promise(() => {}))
    const c = create(post, { timeoutMs: 1000 })
    const running = c.run('search', { query: '慢' })
    await vi.advanceTimersByTimeAsync(1000); await running
    expect(c.status.value).toBe('timeout'); expect(c.message.value).toContain('超时')
    expect(post.mock.calls.some(([path]) => path.endsWith('/cancel'))).toBe(true)
  })
  it('allows the full five-minute window for a slow multi-source search', async () => {
    vi.useFakeTimers()
    const post = vi.fn((path) => path.endsWith('/cancel') ? Promise.resolve({ success: true }) : new Promise(() => {}))
    const c = create(post)
    const running = c.run('search', { query: '慢搜索' })
    expect(post).toHaveBeenCalledWith(expect.stringContaining('/search'), expect.any(Object), expect.objectContaining({ timeout: 300000, signal: expect.any(AbortSignal) }))
    await vi.advanceTimersByTimeAsync(299999)
    expect(c.status.value).toBe('loading')
    await vi.advanceTimersByTimeAsync(1)
    await running
    expect(c.status.value).toBe('timeout')
  })
  it('discards late results after cancellation', async () => {
    let resolve
    const post = vi.fn(path => path.endsWith('/cancel') ? Promise.resolve({ success: true }) : new Promise(done => { resolve = done }))
    const c = create(post), pending = c.run('search', { query: '旧查询' })
    c.cancel(); resolve({ success: true, items: [series] }); await pending; await flushPromises()
    expect(c.status.value).toBe('cancelled'); expect(c.items.value).toEqual([])
  })
  it('a newer query owns results even if the older query finishes last', async () => {
    let resolveOld
    const post = vi.fn((path, body) => path.endsWith('/cancel') ? Promise.resolve({ success: true }) : body.query === 'old' ? new Promise(done => { resolveOld = done }) : Promise.resolve({ success: true, items: [series] }))
    const c = create(post), old = c.run('search', { query: 'old' })
    await c.run('search', { query: 'new' }); resolveOld({ success: true, items: [] }); await old
    expect(c.items.value).toEqual([series]); expect(c.status.value).toBe('success')
  })
})
