import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { reactive } from 'vue'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import PlaylistOrder from '../src/PlaylistOrder.vue'

const members = [
  { tmdb_id: 10, media_type: 'movie', title: '英雄无归 / No Way Home', year: 2021 },
  { tmdb_id: 10, media_type: 'tv', title: '同编号电视剧', year: 2000 },
  { tmdb_id: 11, media_type: 'movie', title: '星球大战', year: 1977 },
  { tmdb_id: 12, media_type: 'movie', title: '沙丘', year: 2024 },
]
const initial = ['movie:10', 'tv:10', 'movie:11', 'movie:12']
const wrappers = []
function render(extra = {}) {
  const playlist = reactive({ id: 'one', name: '测试顺序', order: [...initial] })
  const wrapper = mount(PlaylistOrder, { props: { playlist, members, ...extra }, attachTo: document.body,
    global: { plugins: [createVuetify({ components, directives })] } })
  wrappers.push(wrapper)
  return { wrapper, playlist }
}
const rows = wrapper => wrapper.findAll('[data-order-key]')
const button = (wrapper, label) => wrapper.findAll('button').find(node => node.text() === label)
const field = (wrapper, label) => wrapper.findAllComponents(components.VTextField).find(node => node.props('label') === label)
async function input(wrapper, label, value) { field(wrapper, label).vm.$emit('update:modelValue', value); await flushPromises() }
async function select(wrapper, key) { await wrapper.find(`[data-order-key="${key}"] .sort-item-select`).trigger('click'); await flushPromises() }
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.unstubAllGlobals(); document.body.innerHTML = '' })

describe('long playlist ordering', () => {
  it('moves a selected TV identity exactly like a movie without adding season rows', async () => {
    const { wrapper, playlist } = render()
    expect(wrapper.text()).not.toContain('筛选只影响显示')
    expect(rows(wrapper).map(row => row.attributes('data-order-key'))).toEqual(initial)
    await input(wrapper, '查找列表内影片', '同编号电视剧')
    expect(rows(wrapper)).toHaveLength(1)
    await select(wrapper, 'tv:10')
    await button(wrapper, '置顶').trigger('click'); await flushPromises()
    expect(playlist.order).toEqual(['tv:10','movie:10','movie:11','movie:12'])
    expect(playlist.order.every(key => !key.includes(':season:') && !key.includes(':episode:'))).toBe(true)
    expect(wrapper.text()).not.toContain('已移到完整列表')
  })
  it('filters title, year and ID without changing order, and displays full-list positions', async () => {
    const { wrapper, playlist } = render()
    await input(wrapper, '查找列表内影片', '  NO way 2021  ')
    expect(rows(wrapper).map(row => row.attributes('data-order-key'))).toEqual(['movie:10'])
    expect(rows(wrapper)[0].text()).toContain('1')
    await input(wrapper, '查找列表内影片', '1977')
    expect(rows(wrapper)[0].attributes('data-order-key')).toBe('movie:11')
    expect(rows(wrapper)[0].find('.sort-position').text()).toBe('3')
    await input(wrapper, '查找列表内影片', '10')
    expect(rows(wrapper).map(row => row.attributes('data-order-key'))).toEqual(['movie:10', 'tv:10'])
    expect(playlist.order).toEqual(initial)
    await input(wrapper, '查找列表内影片', '没有这个片名')
    expect(rows(wrapper)).toHaveLength(0)
    expect(wrapper.text()).toContain('没有找到匹配的影片')
    await button(wrapper, '显示全部').trigger('click')
    expect(rows(wrapper)).toHaveLength(4)
  })
  it('moves a filtered result to a full-list position and keeps the selected film', async () => {
    const { wrapper, playlist } = render()
    await input(wrapper, '查找列表内影片', '沙丘')
    await select(wrapper, 'movie:12')
    await input(wrapper, '移到第几位', '2')
    await button(wrapper, '移动').trigger('click'); await flushPromises()
    expect(playlist.order).toEqual(['movie:10', 'movie:12', 'tv:10', 'movie:11'])
    expect(rows(wrapper)).toHaveLength(1)
    expect(rows(wrapper)[0].find('.sort-position').text()).toBe('2')
    expect(rows(wrapper)[0].find('.sort-item-select').attributes('aria-pressed')).toBe('true')
    await button(wrapper, '显示全部').trigger('click'); await flushPromises()
    await button(wrapper, '下移').trigger('click'); await button(wrapper, '下移').trigger('click')
    expect(playlist.order.at(-1)).toBe('movie:12')
    expect(button(wrapper, '下移').attributes('disabled')).toBeDefined()
    await button(wrapper, '置顶').trigger('click')
    expect(playlist.order[0]).toBe('movie:12')
    await button(wrapper, '置底').trigger('click')
    expect(playlist.order.at(-1)).toBe('movie:12')
  })
  it.each(['', '0', '-1', '1.5', '5', '1e2'])('rejects invalid destination %j without changing the list', async value => {
    const { wrapper, playlist } = render()
    await select(wrapper, 'tv:10')
    await input(wrapper, '移到第几位', value)
    await button(wrapper, '移动').trigger('click')
    expect(playlist.order).toEqual(initial)
    expect(wrapper.find('[role="status"]').text()).toContain('1–4 之间的整数')
  })
  it('keeps playlists independent and clears a selection removed by filtering or membership changes', async () => {
    const first = render(), second = render({ playlist: { id: 'two', name: '第二组', order: [...initial] } })
    await select(first.wrapper, 'tv:10')
    await button(first.wrapper, '置顶').trigger('click')
    expect(first.playlist.order[0]).toBe('tv:10')
    expect(rows(second.wrapper)[0].attributes('data-order-key')).toBe('movie:10')
    await input(first.wrapper, '查找列表内影片', '沙丘')
    expect(button(first.wrapper, '上移').attributes('disabled')).toBeDefined()
    await select(first.wrapper, 'movie:12')
    await first.wrapper.setProps({ members: members.slice(0, 3) })
    expect(button(first.wrapper, '移动').attributes('disabled')).toBeDefined()
  })
  it('supports row arrows and refuses all changes while saving', async () => {
    const { wrapper, playlist } = render()
    await wrapper.find('[aria-label="上移 同编号电视剧"]').trigger('click')
    expect(playlist.order).toEqual(['tv:10', 'movie:10', 'movie:11', 'movie:12'])
    await wrapper.setProps({ disabled: true })
    await button(wrapper, '置底').trigger('click')
    expect(playlist.order[0]).toBe('tv:10')
    expect(wrapper.find('[data-drag-key]').attributes('draggable')).toBe('false')
  })
  it('drops against full-list anchors while filtering, preserving hidden films', async () => {
    const { wrapper, playlist } = render()
    await input(wrapper, '查找列表内影片', '电影')
    expect(rows(wrapper)).toHaveLength(3)
    const anchor = wrapper.find('[data-order-key="movie:11"]')
    anchor.element.getBoundingClientRect = () => ({ top: 100, bottom: 140, height: 40 })
    const transfer = { setData: vi.fn() }
    await wrapper.find('[data-drag-key="movie:12"]').trigger('dragstart', { dataTransfer: transfer })
    await anchor.trigger('dragover', { clientX: 50, clientY: 105, dataTransfer: transfer })
    await anchor.trigger('drop', { clientX: 50, clientY: 105, dataTransfer: transfer })
    expect(playlist.order).toEqual(['movie:10', 'tv:10', 'movie:12', 'movie:11'])
    expect(wrapper.attributes('data-dragging')).toBe('false')
  })
  it('handles a forward drop after an anchor and ignores a different playlist drag', async () => {
    const first = render(), second = render()
    const anchor = first.wrapper.find('[data-order-key="movie:11"]')
    anchor.element.getBoundingClientRect = () => ({ top: 100, bottom: 140, height: 40 })
    const transfer = { setData: vi.fn() }
    await first.wrapper.find('[data-drag-key="movie:10"]').trigger('dragstart', { dataTransfer: transfer })
    await anchor.trigger('dragover', { clientX: 50, clientY: 138, dataTransfer: transfer })
    await second.wrapper.find('[data-order-key="movie:12"]').trigger('drop', { clientX: 50, clientY: 138, dataTransfer: transfer })
    expect(second.playlist.order).toEqual(initial)
    await anchor.trigger('drop', { clientX: 50, clientY: 138, dataTransfer: transfer })
    expect(first.playlist.order).toEqual(['tv:10', 'movie:11', 'movie:10', 'movie:12'])
  })
  it('continues edge scrolling without repeated mouse movement and cancels it on unmount', async () => {
    let pending, cancelled = false
    vi.stubGlobal('requestAnimationFrame', callback => { pending = callback; return 1 })
    vi.stubGlobal('cancelAnimationFrame', () => { cancelled = true; pending = null })
    const { wrapper } = render()
    const scroller = wrapper.find('.sort-scroll').element
    scroller.getBoundingClientRect = () => ({ top: 100, bottom: 300, left: 0, right: 300, height: 200 })
    Object.defineProperties(scroller, { scrollHeight: { get: () => 1000 }, clientHeight: { get: () => 200 } })
    await wrapper.find('[data-drag-key="movie:10"]').trigger('dragstart', { dataTransfer: { setData: vi.fn() } })
    await wrapper.find('.sort-scroll').trigger('dragenter', { clientX: 50, clientY: 295, dataTransfer: {} })
    expect(pending).toBeTypeOf('function')
    pending(16); const first = scroller.scrollTop
    pending(32)
    expect(scroller.scrollTop).toBeGreaterThan(first)
    wrapper.unmount()
    expect(cancelled).toBe(true)
  })
  it('coalesces drag events and avoids repainting an unchanged insertion position', async () => {
    let pending
    const schedule = vi.fn(callback => { pending = callback; return 1 })
    vi.stubGlobal('requestAnimationFrame', schedule)
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    const { wrapper, playlist } = render()
    const scroller = wrapper.find('.sort-scroll').element
    scroller.getBoundingClientRect = () => ({ top: 0, bottom: 400, left: 0, right: 300, height: 400 })
    const anchor = wrapper.find('[data-order-key="movie:11"]')
    const measure = vi.fn(() => ({ top: 150, bottom: 210, height: 60 }))
    anchor.element.getBoundingClientRect = measure
    await wrapper.find('[data-drag-key="movie:12"]').trigger('dragstart', { dataTransfer: { setData: vi.fn() } })
    await flushPromises(); schedule.mockClear()
    const component = wrapper.vm.$, originalRender = component.render
    component.render = vi.fn(originalRender)
    for (let i = 0; i < 60; i++) await anchor.trigger('dragover', { clientX: 50, clientY: 155, dataTransfer: {} })
    expect(schedule).toHaveBeenCalledTimes(1)
    expect(measure).not.toHaveBeenCalled()
    pending(16); await flushPromises()
    expect(measure).toHaveBeenCalledTimes(1)
    expect(anchor.classes()).toContain('sort-drop-before')
    const renderCount = component.render.mock.calls.length
    for (let i = 0; i < 5; i++) {
      await anchor.trigger('dragover', { clientX: 55 + i, clientY: 156, dataTransfer: {} })
      pending(32 + i * 16); await flushPromises()
    }
    expect(component.render).toHaveBeenCalledTimes(renderCount)
    expect(playlist.order).toEqual(initial)
    // A drop must use its final coordinates, even before the pending frame.
    await anchor.trigger('drop', { clientX: 50, clientY: 205, dataTransfer: {} })
    expect(playlist.order).toEqual(initial)
    expect(wrapper.attributes('data-dragging')).toBe('false')
  })
})
