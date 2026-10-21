import { describe, expect, it, vi } from 'vitest'
import { createClient } from '../src/api'
describe('V3 plugin client contract', () => {
  it('uses plugin instance id and direct payload with GET query params', async () => {
    const payload = { success: true, status: 'running', data: 'preserve payload data' }
    const api = { get: vi.fn().mockResolvedValue(payload) }
    const request = createClient(api, 'MediaLibraryPlaylistManager_2')
    expect(await request('get', 'job', { job_id: 'job-1' })).toBe(payload)
    expect(api.get).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager_2/job', expect.objectContaining({ params: { job_id: 'job-1' }, timeout: 25000 }))
  })
  it('rejects success:false even for HTTP 200, preserving granular error status', async () => {
    const request = createClient({ post: vi.fn().mockResolvedValue({ success: false, status: 'error', error_code: 'conflict', error: '片单已更新' }) })
    await expect(request('post', 'lists', {})).rejects.toMatchObject({ status: 'conflict', message: '片单已更新' })
  })
  it.each([['ECONNABORTED', 'timeout'], ['ERR_CANCELED', 'cancelled']])('normalises transport %s', async (code, status) => {
    const request = createClient({ post: vi.fn().mockRejectedValue({ code }) })
    await expect(request('post', 'search', {})).rejects.toMatchObject({ status })
  })
  it.each([
    ['Network Error', '无法连接服务'],
    ['Failed to fetch', '无法连接服务'],
    ['Request failed with status code 503', 'HTTP 503'],
    ['Unexpected remote response', '操作失败'],
  ])('renders transport errors in Chinese: %s', async (message, expected) => {
    const request = createClient({ get: vi.fn().mockRejectedValue(new Error(message)) })
    try { await request('get', 'lists'); throw new Error('must fail') }
    catch (error) { expect(error.message).toContain(expected); expect(error.message).toMatch(/[\u3400-\u9fff]/u) }
  })
  it('keeps a specific Chinese server diagnostic and its machine code', async () => {
    const request = createClient({ post: vi.fn().mockResolvedValue({ success: false, status: 'partial',
      error_code: 'membership_unverified', error: '合集未能确认已加入的内容：马大帅 · 第三季。' }) })
    await expect(request('post', 'publish', {})).rejects.toMatchObject({
      message: '合集未能确认已加入的内容：马大帅 · 第三季。', status: 'partial',
      detail: expect.objectContaining({ error_code: 'membership_unverified' }),
    })
  })
})
