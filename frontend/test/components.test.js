import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, h, reactive } from 'vue'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import Page from '../src/Page.vue'
import Config from '../src/Config.vue'
import AddMedia from '../src/AddMedia.vue'
import TargetEditor from '../src/TargetEditor.vue'
import MediaActions from '../src/MediaActions.vue'
import SeasonScope from '../src/SeasonScope.vue'
import RunResult from '../src/RunResult.vue'
import RunResults from '../src/RunResults.vue'
import { editableList, mediaKey, newList } from '../src/domain'
const movie = { tmdb_id: 10, media_type: 'movie', title: '电影名称' }
const series = { tmdb_id: 10, media_type: 'tv', title: '电视剧名称' }
const testTarget = {id:'test-target',server_id:'Emby',kind:'emby',library_ids:[],collection:{enabled:false},playlists:[]}
const wrappers = []
const Dialog = defineComponent({ props: ['modelValue'], setup(props, { slots }) { return () => props.modelValue ? h('div', { role: 'dialog' }, slots.default?.()) : null } })
// The host registers this component globally; verify our model/disabled contract here.
// The browser fixture uses MP's actual component and popup rather than this stand-in.
const CronField = defineComponent({ name: 'VCronField', props: ['modelValue', 'disabled'], emits: ['update:modelValue'],
  setup(props, { attrs, emit }) { return () => h(components.VTextField, { ...attrs, modelValue: props.modelValue, disabled: props.disabled,
    'onUpdate:modelValue': value => emit('update:modelValue', value) }) } })
function render(component, props) {
  const vuetify = createVuetify({ components, directives })
  const wrapper = mount(component, { props, attachTo: document.body, global: { plugins: [vuetify], components: { VCronField: CronField }, stubs: { VDialog: Dialog } } })
  wrappers.push(wrapper)
  return wrapper
}
function button(wrapper, text) {
  const found = wrapper.findAll('button').find(node => node.text() === text)
  if (!found) throw new Error(`No button: ${text}`)
  return found
}
function apiFor(saved) {
  return {
    get: vi.fn(async path => path.endsWith('/lists') ? { success: true, lists: [saved] } : path.endsWith('/settings') ? { success: true, enabled: false, cron: '*/30 * * * *' } : { success: true, items: [] }),
    post: vi.fn(async () => ({ success: false, status: 'error', error_code: 'conflict', error: '片单已更新，请刷新后重试' })),
  }
}
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers(); document.body.innerHTML = '' })
describe('management components', () => {
  it('shows and pauses safely when saved lists contain duplicate remote bindings', async () => {
    const first = { ...newList('旧片单一'), targets: [{ ...testTarget, collection: { enabled: true, name: '合集一', remote_id: '7473' } }] }
    const second = { ...newList('旧片单二'), targets: [{ ...testTarget, id: 'target-2', collection: { enabled: true, name: '合集二', remote_id: '7473' } }] }
    const api = apiFor(first)
    api.get.mockImplementation(async path => path.endsWith('/lists')
      ? { success: true, lists: [first, second] }
      : path.endsWith('/settings')
        ? { success: true, enabled: false, cron: '*/30 * * * *', binding_error: '服务器“Emby”的远端输出（ID：7473）重复绑定。' }
        : { success: true, items: [] })
    const page = render(Page, { api }); await flushPromises()
    expect(page.vm.lists.map(list => list.name)).toEqual(['旧片单一', '旧片单二'])
    expect(page.text()).toContain('同步和自动任务已暂停')
    expect(page.find('[aria-label="同步 旧片单一"]').attributes('disabled')).toBeDefined()
    expect(button(page, '同步全部').attributes('disabled')).toBeDefined()
    expect(page.find('[aria-label="删除 旧片单二"]').attributes('disabled')).toBeUndefined()
  })
  it('clears an import error when closing and reopening the import dialog', async () => {
    const api = apiFor(newList('已有片单'))
    api.post.mockRejectedValue(Error('Trakt 片单无法读取'))
    const page = render(Page,{api}); await flushPromises()
    await button(page,'导入片单').trigger('click'); await flushPromises()
    await page.find('.import-dialog input').setValue('https://app.trakt.tv/users/alice/lists/favorites')
    await button(page,'读取片单').trigger('click'); await flushPromises()
    expect(page.find('.import-dialog [role="alert"]').text()).toContain('Trakt 片单无法读取')
    await button(page,'取消').trigger('click'); await flushPromises()
    expect(page.find('.import-dialog').exists()).toBe(false)
    await button(page,'导入片单').trigger('click'); await flushPromises()
    expect(page.find('.import-dialog [role="alert"]').text()).not.toContain('Trakt 片单无法读取')
  })
  it('shows each imported list as a grouped card with compact output counts', async () => {
    const api = apiFor(newList('已有片单'))
    api.post.mockResolvedValue({success:true,source:'file',lists:[{
      id:'imported-list',name:'漫威片单',members:[movie],unsupported:[],targets:[{
        collection:{enabled:true,name:'不展开的合集名称'},
        playlists:[
          {id:'playlist-1',enabled:true,name:'不展开的播放列表名称',order:['movie:10']},
          {id:'playlist-2',enabled:false,name:'另一个不展开的播放列表',order:['movie:10']},
        ],
      }],
    }]})
    const page = render(Page,{api}); await flushPromises()
    await button(page,'导入片单').trigger('click'); await flushPromises()
    await page.find('.import-dialog input').setValue('https://app.trakt.tv/users/alice/lists/favorites')
    await button(page,'读取片单').trigger('click'); await flushPromises()
    const card = page.find('.import-list-preview')
    expect(card.element.tagName).toBe('ARTICLE')
    expect(card.text()).toContain('1 个合集 · 2 个播放列表')
    expect(card.text()).not.toContain('不展开的合集名称')
    expect(card.text()).not.toContain('不展开的播放列表名称')
    expect(card.text()).toContain('查看片单内容（1 项）')
    const help = page.find('[aria-label="查看导入说明"]')
    expect(help.exists()).toBe(true)
    await help.trigger('mouseenter'); await flushPromises()
    expect(document.body.textContent).toContain('导入片单内容、合集信息、播放列表，需自行设置媒体服务器信息。片单默认停用，自动订阅、自动同步默认关闭。')
    expect(page.find('.import-preview .v-alert--info').exists()).toBe(false)
  })
  it('places the list enable switch before edit and outlines every list action button', async () => {
    const saved = newList('操作顺序')
    const api = apiFor(saved), page = render(Page,{api}); await flushPromises()
    const headerButtons = page.find('.lists-section .section-heading .action-row').findAll('button')
    expect(headerButtons.map(item => item.text().trim())).toEqual(['导入片单','导出全部','同步全部','新建片单'])
    headerButtons.forEach(item => expect(item.classes()).toContain('v-btn--variant-outlined'))

    const children = Array.from(page.find('.list-actions').element.children)
    const actionButton = child => child.matches('button') ? child : child.querySelector('button')
    expect(children[0].classList.contains('list-state')).toBe(true)
    expect(actionButton(children[1]).getAttribute('aria-label')).toBe('编辑 操作顺序')
    expect(actionButton(children[2]).getAttribute('aria-label')).toBe('同步 操作顺序')
    expect(actionButton(children[3]).getAttribute('aria-label')).toBe('导出 操作顺序')
    expect(actionButton(children[4]).getAttribute('aria-label')).toBe('删除 操作顺序')
    children.filter(child => actionButton(child)).forEach(child => {
      expect(actionButton(child).classList.contains('v-btn--variant-outlined')).toBe(true)
    })
  })
  it.each(['single','all'])('shows saved media and library-setting counts before a %s sync and cancels without a request', async scope => {
    const saved = {...newList('准备同步'),members:[movie,{...movie,tmdb_id:11},series],
      targets:[{id:'t',server_id:'Emby',kind:'emby',collection:{enabled:true,remote_id:'c'},playlists:[]}]}
    const api = apiFor(saved), page = render(Page,{api}); await flushPromises()
    if (scope === 'all') await button(page,'同步全部').trigger('click')
    else await page.find('[aria-label="同步 准备同步"]').trigger('click')
    await flushPromises()
    expect(page.find('.sync-confirm-name').text()).toBe('准备同步')
    expect(page.find('.sync-confirm-counts').text()).toBe('2 部电影 · 1 部电视剧 · 1 组媒体库设置')
    expect(page.find('.sync-empty-warning').text()).toBe('空片单同步会清空对应合集和播放列表中的内容。')
    expect(page.text()).not.toContain('仅预览，不同步')
    expect(page.find('.publish-progress').exists()).toBe(false)
    expect(api.post).not.toHaveBeenCalled()
    await button(page,'取消').trigger('click'); await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    expect(page.find('.sync-confirm-dialog').exists()).toBe(false)
  })
  it.each(['collection', 'playlist'])('confirms an empty list with an enabled %s and cancels without submitting', async kind => {
    const saved = {...newList('空片单'), targets:[{id:'t',server_id:'Emby',kind:'emby',
      collection:{enabled:kind==='collection',remote_id:'collection'},
      playlists:[{id:'p',enabled:kind==='playlist',remote_id:'playlist',name:'播放列表',order:[]}]}]}
    const api = apiFor(saved), page = render(Page,{api}); await flushPromises()
    await page.find('[aria-label="同步 空片单"]').trigger('click'); await flushPromises()
    expect(page.find('.sync-confirm-name').text()).toContain('空片单')
    expect(page.find('.sync-confirm-counts').text()).toBe('0 部电影 · 0 部电视剧 · 1 组媒体库设置')
    expect(page.find('.sync-confirm-dialog').text()).toContain('清空')
    expect(page.find('.publish-progress').exists()).toBe(false)
    expect(api.post).not.toHaveBeenCalled()
    await button(page,'取消').trigger('click'); await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    expect(page.find('.sync-confirm-dialog').exists()).toBe(false)
  })
  it('sends explicit empty permission only after confirmation and prevents duplicate submissions', async () => {
    const saved = {...newList('确认清空'), targets:[{id:'t',server_id:'Emby',kind:'emby',collection:{enabled:true,remote_id:'c'},playlists:[]}]}
    const api = apiFor(saved), page = render(Page,{api}); await flushPromises()
    api.post.mockImplementation(() => new Promise(() => {}))
    await page.find('[aria-label="同步 确认清空"]').trigger('click'); await flushPromises()
    const confirm = button(page,'确认同步')
    await confirm.trigger('click'); await confirm.trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post.mock.calls[0][1]).toEqual({list_id:saved.id,dry_run:false,allow_empty:true})
    expect(page.find('.sync-confirm-dialog').exists()).toBe(false)
    expect(page.find('.publish-progress').exists()).toBe(true)
  })
  it('summarizes all enabled lists and warns when an empty list has active outputs', async () => {
    const activeTarget = {id:'t',server_id:'Emby',kind:'emby',collection:{enabled:true,remote_id:'c'},playlists:[]}
    const full = {...newList('有内容'),members:[movie],targets:[activeTarget]}
    const empty = {...newList('会清空'),targets:[activeTarget]}
    const stopped = {...newList('已停用'),enabled:false,targets:[activeTarget]}
    const unused = {...newList('没有启用输出'),targets:[{...activeTarget,collection:{enabled:false,remote_id:'c'}}]}
    const api = apiFor(full), originalGet = api.get.getMockImplementation()
    api.get.mockImplementation(path => path.endsWith('/lists') ? Promise.resolve({success:true,lists:[full,empty,stopped,unused]}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    await button(page,'同步全部').trigger('click'); await flushPromises()
    expect(page.findAll('.sync-confirm-name').map(row => row.text())).toEqual(['有内容','会清空空片单','没有启用输出空片单'])
    expect(page.find('.sync-confirm-scope').text()).toBe('本次同步 3 个已启用片单')
    expect(page.find('.sync-empty-warning').text()).toContain('本次包含空片单')
    expect(api.post).not.toHaveBeenCalled()
    api.post.mockImplementation(() => new Promise(() => {}))
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(api.post.mock.calls[0][1]).toEqual({dry_run:false,allow_empty:true})
  })
  it('does not reuse cancelled empty permission for a normal sync', async () => {
    const empty = {...newList('空片单'),targets:[{id:'t',server_id:'Emby',kind:'emby',collection:{enabled:true,remote_id:'c'},playlists:[]}]}
    const full = {...newList('有内容'),members:[movie],targets:[testTarget]}
    const api = apiFor(full), originalGet = api.get.getMockImplementation()
    api.get.mockImplementation(path => path.endsWith('/lists') ? Promise.resolve({success:true,lists:[empty,full]}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    await page.find('[aria-label="同步 空片单"]').trigger('click'); await flushPromises()
    await button(page,'取消').trigger('click'); await flushPromises()
    await page.find('[aria-label="同步 有内容"]').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(api.post.mock.calls[0][1]).toEqual({list_id:full.id,dry_run:false,allow_empty:false})
  })
  it('syncs without empty permission when only stopped lists are empty or outputs are disabled', async () => {
    const full = {...newList('有内容'),members:[movie],targets:[testTarget]}
    const stopped = {...newList('已停用'),enabled:false,targets:[{id:'t',collection:{enabled:true},playlists:[]}]}
    const unused = {...newList('无输出'),targets:[{id:'u',collection:{enabled:false},playlists:[{enabled:false}]}]}
    const api = apiFor(full), originalGet = api.get.getMockImplementation()
    api.get.mockImplementation(path => path.endsWith('/lists') ? Promise.resolve({success:true,lists:[full,stopped,unused]}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    await button(page,'同步全部').trigger('click'); await flushPromises()
    expect(page.find('.sync-confirm-dialog').exists()).toBe(true)
    expect(page.find('.sync-empty-warning').text()).not.toContain('本次包含空片单')
    expect(api.post).not.toHaveBeenCalled()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(api.post.mock.calls[0][1]).toEqual({dry_run:false,allow_empty:false})
  })
  it('shows measured completion counts and the current media server stage inside the blocking layer', async () => {
    vi.useFakeTimers()
    const saved = {...newList('进度检查'),targets:[testTarget]}, api = apiFor(saved), originalGet = api.get.getMockImplementation()
    let progress = {completed: 1, total: 3, stage: 'inventory', list_name: '进度检查', server_id: '家庭影院'}
    api.post.mockResolvedValue({success:true,job_id:'progress-job'})
    api.get.mockImplementation(path => path.endsWith('/job') ? Promise.resolve({success:true,status:'running',progress}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    await page.find('[aria-label="同步 进度检查"]').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    const layer = () => page.find('.publish-progress')
    expect(layer().text()).toContain('已完成 1 / 3 项任务')
    expect(layer().text()).toContain('家庭影院')
    expect(layer().text()).toContain('正在读取媒体库')
    const bar = page.findAllComponents(components.VProgressLinear).find(component => component.attributes('aria-label') === '已完成 1 / 3 项任务')
    expect(bar.props('modelValue')).toBeCloseTo(100/3)
    progress = {...progress, completed:2, stage:'verify'}
    await vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(layer().text()).toContain('已完成 2 / 3 项任务')
    expect(layer().text()).toContain('正在核对同步结果')
    expect(page.find('.playlist-plugin').attributes('inert')).toBeDefined()
    expect(api.post).toHaveBeenCalledTimes(1)
  })
  it.each([{completed:4,total:3}, {completed:-1,total:3}, {completed:1,total:0}, {completed:'1',total:3}])('keeps the loading animation when completion counts are invalid: %j', async progress => {
    const saved = {...newList('兼容旧任务'),targets:[testTarget]}, api = apiFor(saved), originalGet = api.get.getMockImplementation()
    api.post.mockResolvedValue({success:true,job_id:'legacy-job'})
    api.get.mockImplementation(path => path.endsWith('/job') ? Promise.resolve({success:true,status:'running',progress}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    await page.find('[aria-label="同步 兼容旧任务"]').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(page.find('.publish-progress').exists()).toBe(true)
    expect(page.find('.publish-progress-count').exists()).toBe(false)
    expect(page.find('.playlist-plugin').attributes('inert')).toBeDefined()
  })
  it.each(['single', 'all'])('blocks the page throughout a %s sync, including submission and background execution', async scope => {
    vi.useFakeTimers()
    const saved = {...newList('同步检查'),members:[movie],targets:[testTarget]}, api = apiFor(saved)
    const originalGet = api.get.getMockImplementation()
    let submit, finishLists, terminal = false
    api.post.mockImplementation(() => new Promise(resolve => { submit = resolve }))
    api.get.mockImplementation(path => path.endsWith('/job') ? Promise.resolve({success:true,status:terminal?'success':'running'})
      : terminal && path.endsWith('/lists') ? new Promise(resolve => { finishLists = resolve }) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    const root = () => page.find('.playlist-plugin')
    const start = scope === 'all' ? button(page,'同步全部') : page.find('[aria-label="同步 同步检查"]')
    await start.trigger('click'); await start.trigger('click'); await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    expect(page.find('.publish-progress').exists()).toBe(false)
    const confirm = button(page,'确认同步')
    await confirm.trigger('click'); await confirm.trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(page.find('.publish-progress').exists()).toBe(true)
    expect(root().attributes('inert')).toBeDefined()
    expect(root().attributes('aria-busy')).toBe('true')
    submit({success:true,job_id:'sync-job',status:'queued'}); await flushPromises()
    expect(page.find('.publish-progress').exists()).toBe(true)
    const payload = api.post.mock.calls[0][1]
    expect(payload.list_id).toBe(scope==='single'?saved.id:undefined)
    expect(payload.dry_run).toBe(false)
    expect(payload.allow_empty).toBe(false)
    expect(page.text()).not.toContain('仅预览，不同步')
    expect(page.find('.sync-confirm-dialog').exists()).toBe(false)
    terminal = true
    const tick = vi.advanceTimersByTimeAsync(2000); await flushPromises()
    expect(finishLists).toBeTypeOf('function')
    expect(page.find('.publish-progress').exists()).toBe(true)
    finishLists({success:true,lists:[saved]}); await tick; await flushPromises()
    expect(page.find('.publish-progress').exists()).toBe(false)
    expect(root().attributes('inert')).toBeUndefined()
    expect(root().attributes('aria-busy')).toBe('false')
  })
  it('releases the blocking layer after submission fails and keeps the concrete error', async () => {
    const saved = {...newList('提交失败'),targets:[testTarget]}, api = apiFor(saved)
    api.post.mockRejectedValue(Error('无法连接服务器'))
    const page = render(Page,{api}); await flushPromises()
    await page.find('[aria-label="同步 提交失败"]').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(page.find('.publish-progress').exists()).toBe(false)
    expect(page.find('.playlist-plugin').attributes('inert')).toBeUndefined()
    expect(page.text()).toContain('无法连接服务器')
  })
  it.each(['partial','error','cancelled'])('keeps a failed progress query blocked and allows retry to a %s completion', async status => {
    const saved = {...newList('查询重试'),targets:[testTarget]}, api = apiFor(saved), originalGet = api.get.getMockImplementation()
    let failed = true
    api.post.mockResolvedValue({success:true,job_id:'retry-job'})
    api.get.mockImplementation(path => path.endsWith('/job') ? failed ? Promise.reject(Error('网络中断'))
      : Promise.resolve({success:true,status}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    await page.find('[aria-label="同步 查询重试"]').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(page.find('.publish-progress').text()).toContain('网络中断')
    expect(page.find('.playlist-plugin').attributes('inert')).toBeDefined()
    failed = false
    await button(page,'刷新进度').trigger('click'); await flushPromises()
    expect(page.find('.publish-progress').exists()).toBe(false)
    expect(page.find('.playlist-plugin').attributes('inert')).toBeUndefined()
    expect(api.post).toHaveBeenCalledTimes(1)
  })
  it('keeps waiting or unrecognized task states blocked until a verified completion', async () => {
    vi.useFakeTimers()
    const saved = {...newList('等待识别'),targets:[testTarget]}, api = apiFor(saved), originalGet = api.get.getMockImplementation()
    let status = 'waiting'
    api.post.mockResolvedValue({success:true,job_id:'waiting-job'})
    api.get.mockImplementation(path => path.endsWith('/job') ? Promise.resolve({success:true,status}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    await page.find('[aria-label="同步 等待识别"]').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(page.find('.publish-progress').text()).toContain('等待媒体库识别')
    status = 'unrecognized'
    await vi.advanceTimersByTimeAsync(30000); await flushPromises()
    expect(page.find('.publish-progress').text()).toContain('任务状态')
    expect(page.find('.playlist-plugin').attributes('inert')).toBeDefined()
    status = 'success'
    await button(page,'刷新进度').trigger('click'); await flushPromises()
    expect(page.find('.publish-progress').exists()).toBe(false)
  })
  it('uses concise generic media-library labels in every editor tab and keeps footer actions at the end', async () => {
    const saved = {...newList('通用片单'),members:[movie]}
    const page = render(Page,{api:apiFor(saved)}); await flushPromises(); page.vm.open(saved); await flushPromises()
    expect(page.findAllComponents(components.VTab).map(tab => tab.text())).toEqual(['片单内容','媒体库同步','自动任务'])
    expect(page.text()).not.toContain('播放顺序可在')
    expect(page.text()).not.toContain('修改后请保存片单')
    await button(page,'媒体库同步').trigger('click'); await flushPromises()
    expect(page.text()).toContain('添加媒体服务器设置')
    expect(page.text()).not.toContain('移除这里的设置')
    expect(page.text()).not.toContain('暂不支持 Jellyfin')
    await button(page,'自动任务').trigger('click'); await flushPromises()
    expect(page.text()).toContain('入库后自动同步到媒体库')
    expect(page.text()).not.toContain('这两项默认关闭')
    expect(page.text()).not.toContain('文件整理和分类')
    expect(page.text()).not.toContain('Emby')
    expect(page.find('.editor-actions').text()).toBe('取消保存片单')
  })
  it('keeps unrelated controls stable while saving one list state and does not request a host refresh', async () => {
    const first = {...newList('第一份'),members:[movie]}, second = {...newList('第二份'),members:[series]}
    const api = apiFor(first), originalGet = api.get.getMockImplementation()
    api.get.mockImplementation(async path => path.endsWith('/lists') ? {success:true,lists:[first,second]} : originalGet(path))
    let finish
    api.post.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const page = render(Page,{api}); await flushPromises()
    const otherList = page.vm.lists[1]
    const state = page.findAllComponents(components.VSwitch).find(node => node.props('label') === '已启用')
    state.vm.$emit('update:modelValue',false); await flushPromises()
    expect(button(page,'刷新').attributes('disabled')).toBeUndefined()
    expect(button(page,'新建片单').attributes('disabled')).toBeUndefined()
    expect(page.findAll('[aria-label="编辑 第二份"]')[0].attributes('disabled')).toBeUndefined()
    finish({success:true,list:{...first,enabled:false,revision:1},lists:[{...first,enabled:false,revision:1},{...second}]}); await flushPromises()
    expect(page.emitted('action')).toBeUndefined()
    expect(page.vm.lists[1]).toBe(otherList)
  })
  it('disables sync for stopped lists and disables sync all when every list is stopped', async () => {
    const saved = {...newList('已停用的片单'),enabled:false}, page = render(Page,{api:apiFor(saved)})
    await flushPromises()
    expect(page.find('[aria-label="同步 已停用的片单"]').attributes('disabled')).toBeDefined()
    expect(button(page,'同步全部').attributes('disabled')).toBeDefined()
  })
  it('prevents enabling or syncing lists without media server settings and skips them in sync all', async () => {
    const unconfigured = {...newList('未配置'), enabled:false, targets:[]}
    const legacyEnabled = {...newList('旧未配置'), enabled:true, targets:[]}
    const configured = {...newList('已配置'), targets:[{id:'t',server_id:'Emby',kind:'emby',library_ids:[],collection:{enabled:true},playlists:[]}]}
    const api = apiFor(configured), originalGet = api.get.getMockImplementation()
    api.get.mockImplementation(path => path.endsWith('/lists') ? Promise.resolve({success:true,lists:[unconfigured,legacyEnabled,configured]}) : originalGet(path))
    const page = render(Page,{api}); await flushPromises()
    const unavailableToggle = page.findAllComponents(components.VSwitch).find(item => item.props('label') === '已停用')
    expect(unavailableToggle.props('disabled')).toBe(true)
    expect(page.find('[aria-label="同步 未配置"]').attributes('disabled')).toBeDefined()
    await button(page,'同步全部').trigger('click'); await flushPromises()
    expect(page.findAll('.sync-confirm-name').map(row => row.text())).toEqual(['已配置空片单'])
    expect(page.find('.sync-skip-unconfigured').text()).toContain('1 个未设置媒体服务器的片单已跳过')
    await button(page,'取消').trigger('click'); await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
  })
  it('opens new lists disabled until a media server is configured', async () => {
    const page = render(Page,{api:apiFor(newList('已有'))}); await flushPromises()
    await button(page,'新建片单').trigger('click'); await flushPromises()
    expect(page.vm.editing.enabled).toBe(false)
    const enabled = page.findAllComponents(components.VSwitch).find(item => item.props('label') === '启用此片单')
    expect(enabled.props('disabled')).toBe(true)
  })
  it('keeps concurrent row toggles independent when responses contain stale snapshots of other rows', async () => {
    const first = newList('第一份'), second = newList('第二份'), api = apiFor(first)
    const originalGet = api.get.getMockImplementation(), finish = {}
    api.get.mockImplementation(async path => path.endsWith('/lists') ? {success:true,lists:[first,second]} : originalGet(path))
    api.post.mockImplementation((path,body) => new Promise(resolve => { finish[body.list_id] = resolve }))
    const page = render(Page,{api}); await flushPromises()
    const toggles = page.findAllComponents(components.VSwitch).filter(node => node.props('label') === '已启用')
    toggles[0].vm.$emit('update:modelValue',false); await flushPromises()
    expect(toggles[1].props('disabled')).toBe(false)
    toggles[1].vm.$emit('update:modelValue',false); await flushPromises()
    finish[second.id]({success:true,list:{...second,enabled:false,revision:1},lists:[first,{...second,enabled:false,revision:1}]}); await flushPromises()
    finish[first.id]({success:true,list:{...first,enabled:false,revision:1},lists:[{...first,enabled:false,revision:1},second]}); await flushPromises()
    expect(page.vm.lists.map(list => list.enabled)).toEqual([false,false])
    expect(api.post).toHaveBeenCalledTimes(2)
    expect(page.emitted('action')).toBeUndefined()
  })
  it.each(['refresh-last','toggle-last'])('does not roll back a newer state when %s returns an old revision', async order => {
    const saved = newList('版本检查'), api = apiFor(saved)
    let finishToggle, finishRefresh
    api.post.mockImplementation(() => new Promise(resolve => { finishToggle = resolve }))
    const page = render(Page,{api}); await flushPromises()
    page.findAllComponents(components.VSwitch).find(node => node.props('label') === '已启用').vm.$emit('update:modelValue',false); await flushPromises()
    const originalGet = api.get.getMockImplementation()
    api.get.mockImplementation(path => path.endsWith('/lists') ? new Promise(resolve => { finishRefresh = resolve }) : originalGet(path))
    const refresh = page.vm.refresh(); await flushPromises()
    if (order === 'refresh-last') {
      finishToggle({success:true,list:{...saved,enabled:false,revision:1}}); await flushPromises()
      finishRefresh({success:true,lists:[saved]})
    } else {
      finishRefresh({success:true,lists:[{...saved,enabled:true,revision:2}]}); await flushPromises()
      finishToggle({success:true,list:{...saved,enabled:false,revision:1}})
    }
    await refresh; await flushPromises()
    expect(page.vm.lists[0].revision).toBe(order === 'refresh-last' ? 1 : 2)
    expect(page.vm.lists[0].enabled).toBe(order === 'refresh-last' ? false : true)
  })
  it('shows each list state and saves a quick toggle without opening the editor or changing content', async () => {
    const saved = {...newList('快捷操作'),members:[series],targets:[testTarget]}, api = apiFor(saved)
    let finish
    api.post.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const page = render(Page,{api}); await flushPromises()
    const toggle = () => page.findAllComponents(components.VSwitch).find(node => node.props('label') === (page.vm.lists[0].enabled ? '已启用' : '已停用'))
    expect(toggle().props('modelValue')).toBe(true)
    toggle().vm.$emit('update:modelValue',false); await flushPromises()
    expect(toggle().props('disabled')).toBe(true)
    toggle().vm.$emit('update:modelValue',false); await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/lists',{action:'set_enabled',list_id:saved.id,enabled:false,revision:saved.revision},expect.anything())
    expect(page.vm.lists[0].enabled).toBe(true)
    finish({success:true,list:{...saved,enabled:false,revision:1}}); await flushPromises()
    expect(toggle().props('modelValue')).toBe(false)
    expect(page.vm.lists[0].members).toEqual([series])
    expect(page.find('[role="dialog"]').exists()).toBe(false)
    api.post.mockResolvedValue({success:false,error_code:'conflict',error:'片单已更新，请刷新后重试'})
    toggle().vm.$emit('update:modelValue',true); await flushPromises()
    expect(toggle().props('modelValue')).toBe(false)
    expect(toggle().props('disabled')).toBe(false)
    expect(page.text()).toContain('片单已更新，请刷新后重试')
  })
  it('labels season subscriptions with the work title and explicit season', () => {
    const result = render(RunResult, {members:[series],result:{operation:'subscribe',key:'tv:10:season:4',status:'subscribed',success:true}})
    expect(result.text()).toContain('订阅内容：电视剧名称 · 第 4 季')
    expect(result.find('.result-state').classes()).toContain('result-state-success')
  })
  it('hides empty output plans when no list member matched the media library', () => {
    const result = render(RunResult, {result:{status:'error',success:false,matched_count:0,configured_count:2,plan:[
      {kind:'collection',name:'合集',add:[],remove:[]}, {kind:'playlist',name:'播放列表',add:[],remove:[],order:[]},
    ]}})
    expect(result.findAll('.publish-plan')).toHaveLength(0)
    const emptyList = render(RunResult, {result:{status:'success',success:true,matched_count:0,configured_count:0,plan:[
      {kind:'collection',name:'合集',add:[],remove:[]},
    ]}})
    expect(emptyList.findAll('.publish-plan')).toHaveLength(1)
  })
  it('collapses results by list, summarizes outcome, then shows libraries and subscriptions', async () => {
    const results = [
      {list_id:'marvel',list_name:'漫威片单',server_id:'Jellyfin',target_name:'客厅媒体库',library_names:['电影'],status:'partial',success:false,
        error:'媒体库操作未完成', matched_count:24,configured_count:26,added:['movie:11'],removed:['movie:12'],pending:['movie:13'],
        plan:[{kind:'playlist',name:'时间线',add:['movie:11'],remove:['movie:12'],order:['movie:11']}]},
      {list_id:'marvel',list_name:'漫威片单',operation:'subscribe',key:'movie:13',status:'subscribed',success:true},
      {list_id:'marvel',list_name:'漫威片单',operation:'subscribe',key:'tv:20:season:2',status:'error',success:false,error:'订阅失败'},
    ]
    const members = [
      {...movie,tmdb_id:11,title:'新增电影'}, {...movie,tmdb_id:12,title:'移除电影'},
      {...movie,tmdb_id:13,title:'待订阅电影'}, {...series,tmdb_id:20,title:'目标电视剧'},
    ]
    const view = render(RunResults,{results,membersFor:() => members})
    expect(view.findAll('.run-list-result')).toHaveLength(1)
    const list = view.find('.run-list-result')
    expect(list.element.open).toBe(false)
    expect(view.find('.run-list-title').text()).toBe('漫威片单')
    expect(view.find('.run-list-status').text()).toBe('部分成功')
    expect(view.find('.run-list-status').classes()).toContain('run-status-error')
    await list.find('summary').trigger('click')
    expect(list.element.open).toBe(true)
    expect(view.findAll('.result-module-title').map(item => item.text())).toEqual(['媒体库','订阅'])
    expect(view.find('.result-state').text()).toContain('部分成功')
    expect(view.find('.result-state').classes()).toContain('result-state-error')
    expect(view.text()).toContain('客厅媒体库')
    expect(view.text()).toContain('媒体库：电影')
    expect(view.text()).toContain('影响范围')
    expect(view.text()).toContain('新增电影')
    expect(view.text()).toContain('移除电影')
    expect(view.text()).toContain('待订阅电影')
    expect(view.text()).toContain('目标电视剧 · 第 2 季')
    expect(view.text()).toContain('电影')
    expect(view.text()).toContain('电视剧')
    expect(view.findAll('.subscription-category')).toHaveLength(2)
    expect(view.text()).toContain('电影订阅 · 1 项')
    expect(view.text()).toContain('已订阅：待订阅电影')
    expect(view.text()).toContain('电视剧订阅 · 1 项')
    expect(view.text()).toContain('失败：目标电视剧 · 第 2 季（订阅失败）')
    expect(view.findAll('.subscription-summary').map(item => item.classes().some(name => ['subscription-success','subscription-error'].includes(name)))).toEqual([true,true])
  })
  it('summarizes all-success and failed lists independently', () => {
    const results = [
      {list_id:'ok',list_name:'成功片单',status:'success',success:true},
      {list_id:'failed',list_name:'失败片单',status:'error',success:false},
    ]
    const view = render(RunResults,{results,membersFor:() => []})
    expect(view.findAll('.run-list-status').map(item => item.text())).toEqual(['成功','失败'])
    expect(view.findAll('.run-list-status').map(item => item.classes().includes('run-status-success'))).toEqual([true,false])
    expect(view.findAll('.run-list-status').map(item => item.classes().includes('run-status-error'))).toEqual([false,true])
    expect(view.findAll('.run-list-result').every(item => !item.element.open)).toBe(true)
  })
  it('treats a zero-match library check as complete when subscriptions succeed', async () => {
    const results = [
      { list_id:'pending', list_name:'待入库片单', status:'skipped', success:false, error_code:'no_matches',
        error:'媒体库中未找到片单内容，本次未作修改', matched_count:0, configured_count:2, pending:['movie:10','movie:11'] },
      { list_id:'pending', list_name:'待入库片单', operation:'subscribe', key:'movie:10', status:'subscribed', success:true },
    ]
    const view = render(RunResults,{results,membersFor:() => [movie]})
    expect(view.find('.run-list-status').text()).toBe('成功')
    expect(view.find('.run-list-status').classes()).toContain('run-status-success')
    await view.find('summary').trigger('click')
    expect(view.findAll('.result-state').map(item => item.text())).toEqual(['完成'])
    expect(view.findAll('.result-state-success')).toHaveLength(1)
    expect(view.text()).toContain('匹配到 0 / 2 项 · 未匹配 2 项')
    expect(view.find('.subscription-success').text()).toContain('已订阅：')
    expect(view.find('.pending-content').exists()).toBe(false)
    expect(view.find('.error-text').exists()).toBe(false)
  })
  it('shows a zero-match-only list as successful and completed', async () => {
    const view = render(RunResults,{results:[
      { list_id:'pending', list_name:'待入库片单', status:'skipped', success:false, error_code:'no_matches',
        error:'媒体库中未找到片单内容，本次未作修改' },
    ],membersFor:() => []})
    expect(view.find('.run-list-status').text()).toBe('成功')
    expect(view.find('.run-list-status').classes()).toContain('run-status-success')
    await view.find('summary').trigger('click')
    expect(view.find('.result-state').text()).toBe('完成')
    expect(view.find('.result-state-success').exists()).toBe(true)
    expect(view.find('.run-list-status').classes()).not.toContain('run-status-error')
  })
  it('treats a partial label without an error as successful completion', async () => {
    const view = render(RunResults,{results:[
      { list_id:'complete', list_name:'已完成片单', status:'partial', success:false, matched_count:1, configured_count:2, pending:['movie:11'] },
    ],membersFor:() => [movie]})
    expect(view.find('.run-list-status').text()).toBe('成功')
    await view.find('summary').trigger('click')
    expect(view.find('.result-state').text()).toBe('完成')
    expect(view.find('.result-state-success').exists()).toBe(true)
  })
  it('shows a cached MoviePilot subscription as already present', async () => {
    const view = render(RunResults,{results:[
      { list_id:'cached', list_name:'已有订阅', operation:'subscribe', key:'movie:10', status:'subscribed', success:true, already_exists:true },
    ],membersFor:() => []})
    await view.find('summary').trigger('click')
    expect(view.find('.subscription-success').text()).toContain('已在订阅中：')
  })
  it('does not show a duplicate warning for unmapped search candidates', async () => {
    const candidate = {media_source:'douban',media_id:'9',media_type:'movie',title:'蜘蛛侠5'}
    const request = vi.fn(async () => ({success:true,status:'success',items:[candidate],errors:[]}))
    const view = render(AddMedia,{request,members:[]})
    view.findAllComponents(components.VTextField).find(item => item.props('label') === '电影或电视剧名称').vm.$emit('update:modelValue','蜘蛛侠5')
    await button(view,'查询内容').trigger('click'); await flushPromises()
    expect(view.text()).toContain('豆瓣 9 · 未关联 TMDB，暂不可加入')
    expect(view.findAllComponents(components.VAlert).some(alert => alert.props('type') === 'warning')).toBe(false)
  })
  it('saves selected seasons on one work row and shows the same scope in playlist sorting', async () => {
    const saved = { ...newList('只看后两季'), members:[series], targets:[{ id:'t', server_id:'Emby', kind:'emby', library_ids:[], collection:{enabled:true}, playlists:[{id:'p',name:'观看顺序',enabled:true,order:['tv:10']}]}] }
    const api = apiFor(saved)
    api.post.mockImplementation(async (path, body) => path.endsWith('/seasons') ? {success:true, items:[1,2,3,4].map(season_number => ({season_number,episode_count:24}))} : {success:true,list:body.list,lists:[body.list]})
    const page = render(Page, {api}); await flushPromises(); page.vm.open(saved); await flushPromises()
    const scope = page.findComponent(SeasonScope)
    scope.findComponent(components.VSelect).vm.$emit('update:modelValue','selected'); await flushPromises()
    await page.vm.saveList(); await flushPromises()
    expect(page.text()).toContain('至少选择一季')
    expect(api.post.mock.calls.filter(([path]) => path.endsWith('/lists'))).toHaveLength(0)
    scope.findAllComponents(components.VSelect)[1].vm.$emit('update:modelValue',[4,3]); await flushPromises()
    expect(page.vm.editing.members).toEqual([{...series,seasons:[3,4]}])
    page.vm.tab = 'targets'; await flushPromises()
    // Scope is metadata of the work, never an extra sortable row.
    expect(page.vm.editing.targets[0].playlists[0].order).toEqual(['tv:10'])
    await page.vm.saveList(); await flushPromises()
    const payload = api.post.mock.calls.find(([path]) => path.endsWith('/lists'))[1]
    expect(payload.list.members).toEqual([{...series,seasons:[3,4]}])
  })
  it('keeps an existing season selection when metadata fails and ignores stale season responses', async () => {
    let finish
    const request = vi.fn(() => new Promise(resolve => { finish = resolve }))
    const scope = render(SeasonScope, {tmdbId:10,modelValue:[3,4],request})
    await scope.setProps({tmdbId:11,modelValue:null}); await flushPromises()
    finish({items:[{season_number:8,episode_count:1}]}); await flushPromises()
    expect(scope.emitted('update:modelValue')).toBeUndefined()
    request.mockRejectedValue(new Error('读取失败'))
    await scope.setProps({modelValue:[3,4]}); await flushPromises()
    expect(scope.text()).toContain('读取失败')
    expect(scope.findAllComponents(components.VSelect)[1].props('modelValue')).toEqual([3,4])
    expect(scope.findAllComponents(components.VSelect)[1].props('items').map(item => item.value)).toEqual([3,4])
    expect(scope.emitted('update:modelValue')).toBeUndefined()
  })
  it('lets a searched TV result choose seasons before adding it', async () => {
    const request = vi.fn(async (_, endpoint) => ({success:true,items:endpoint === 'seasons' ? [1,2,3,4].map(season_number => ({season_number,episode_count:24})) : [series]}))
    const add = render(AddMedia, {request,members:[]})
    add.findAllComponents(components.VTextField).find(field => field.props('label') === '电影或电视剧名称').vm.$emit('update:modelValue','剧')
    await button(add,'查询内容').trigger('click'); await flushPromises()
    await button(add,'全选').trigger('click'); await flushPromises()
    const scope = add.findComponent(SeasonScope)
    scope.findComponent(components.VSelect).vm.$emit('update:modelValue','selected'); await flushPromises()
    expect(add.vm.scopes).toEqual({'tv:10':[]})
    expect(scope.props('modelValue')).toEqual([])
    expect(add.vm.scopeValid).toBe(false)
    expect(button(add,'添加所选 1 项').attributes('disabled')).toBeDefined()
    scope.findAllComponents(components.VSelect)[1].vm.$emit('update:modelValue',[3,4]); await flushPromises()
    await button(add,'添加所选 1 项').trigger('click')
    expect(add.emitted('add')[0][0]).toEqual([{...series,seasons:[3,4]}])
  })
  it('uses exactly the selected IDs for playlist editing without querying a media server', async () => {
    const selected = [
      { tmdb_id:111697, media_type:'tv', title:'马大帅' },
      { tmdb_id:286679, media_type:'tv', title:'马大帅 II' },
      { tmdb_id:286680, media_type:'tv', title:'马大帅 III' },
    ]
    const list = editableList({ ...newList('同一份内容'), members:selected, targets:[{ id:'t', server_id:'', kind:'emby', collection:{},
      playlists:[{ id:'p', enabled:true, order:selected.map(mediaKey) }] }] })
    const request = vi.fn(), editor = render(TargetEditor, { target:list.targets[0], members:list.members, servers:[], request })
    await flushPromises()
    expect(editor.findAll('[data-order-key]').map(row => row.attributes('data-order-key'))).toEqual(selected.map(mediaKey))
    expect(editor.text()).not.toContain('待匹配')
    expect(editor.text()).not.toContain('未匹配到已入库剧集')
    expect(editor.find('[aria-label="上移 马大帅 II"]').attributes('disabled')).toBeUndefined()
    expect(request).not.toHaveBeenCalled()
    await editor.find('[aria-label="上移 马大帅 II"]').trigger('click')
    expect(list.targets[0].playlists[0].order).toEqual(['tv:286679','tv:111697','tv:286680'])
  })
  it('uses the host cron chooser, keeps selection in the draft, and saves through scoped settings', async () => {
    const api = apiFor(newList('已有片单'))
    let finishSave, finishLoad
    const get = api.get.getMockImplementation()
    api.get.mockImplementation(path => path.endsWith('/settings') ? new Promise(resolve => { finishLoad = resolve }) : get(path))
    api.post.mockImplementation(() => new Promise(resolve => { finishSave = resolve }))
    const page = render(Page, { api, pluginId: 'MediaLibraryPlaylistManager_2' })
    await page.vm.$nextTick()
    expect(page.findComponent(CronField).props('disabled')).toBe(true)
    finishLoad({ success: true, settings: { enabled: false, cron: '*/30 * * * *' } })
    await flushPromises()
    const field = page.findComponent(CronField)
    expect(field.props('modelValue')).toBe('*/30 * * * *')
    expect(field.props('disabled')).toBe(false)
    field.vm.$emit('update:modelValue', '0 2 * * *'); await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    await button(page, '保存设置').trigger('click')
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager_2/settings', { enabled: false, cron: '0 2 * * *' }, expect.anything())
    expect(field.props('disabled')).toBe(true)
    finishSave({ success: true, settings: { enabled: false, cron: '0 2 * * *' } }); await flushPromises()
    expect(field.props('modelValue')).toBe('0 2 * * *')
    expect(field.props('disabled')).toBe(false)
  })
  it('Config uses scoped internal saving without forwarding an empty host save payload', async () => {
    const saved = { ...newList('已有片单'), members: [movie], targets:[testTarget], revision: 4 }
    const initialConfig = { enabled: true, lists: [saved], servers: { private: 'preserve' } }
    const original = JSON.stringify(initialConfig), hostSave = vi.fn()
    const api = apiFor(saved)
    api.post.mockImplementation(async (path, body) => path.endsWith('/lists') ? { success: true, list: { ...body.list, revision: 5 }, lists: [{ ...body.list, revision: 5 }] } : { success: true, enabled: false, cron: body.cron })
    const config = render(Config, { api, pluginId: 'MediaLibraryPlaylistManager_2', initialConfig, onSave: hostSave })
    await flushPromises()
    expect(config.emitted('layout')[0]).toEqual([{ maxWidth: '78rem' }])
    await button(config, '保存设置').trigger('click'); await flushPromises()
    const page = config.findComponent(Page)
    page.vm.open(saved); await flushPromises(); page.vm.editing.name = '通过 Config 保存'
    await page.vm.saveList(); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager_2/settings', { enabled: false, cron: '*/30 * * * *' }, expect.anything())
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager_2/lists', expect.objectContaining({ action: 'save', revision: 4, list: expect.objectContaining({ name: '通过 Config 保存', members: [movie] }) }), expect.anything())
    expect(hostSave).not.toHaveBeenCalled(); expect(config.emitted('save')).toBeUndefined()
    expect(JSON.stringify(initialConfig)).toBe(original)
    await button(config, '查看详情').trigger('click'); await button(config, '完成').trigger('click')
    expect(config.emitted('switch')).toHaveLength(1); expect(config.emitted('close')).toHaveLength(1)
  })
  it('renders actual publisher pending keys and concrete dry-run change counts', () => {
    const result = render(RunResult, { members: [movie, series], result: { list_name: '片单', server_id: 'Emby', success: true, status: 'success', dry_run: true, matched_count: 1, configured_count: 2, pending: ['tv:10'], plan: [{ id: 'p', kind: 'playlist', name: '观影', create: true, add: ['1','2'], remove: ['3'], order: ['1','2'] }] } })
    expect(result.text()).toContain('电视剧名称'); expect(result.text()).toContain('新增 2 项'); expect(result.text()).toContain('移除 1 项'); expect(result.text()).toContain('播放顺序 2 项')
  })
  it('creates only a local draft until Save and keeps conflict drafts intact', async () => {
    const saved = { ...newList('已保存'), revision: 3, members: [movie], targets:[testTarget] }
    const api = apiFor(saved), page = render(Page, { api, pluginId: 'MediaLibraryPlaylistManager_2' })
    await flushPromises(); page.vm.open(saved); await flushPromises()
    page.vm.editing.name = '草稿改名'; page.vm.editing.members.push(series)
    expect(page.vm.lists[0].name).toBe('已保存'); expect(page.vm.lists[0].members).toHaveLength(1)
    expect(api.post).not.toHaveBeenCalled()
    expect(page.findAll('.member-list [draggable]')).toHaveLength(0)
    await page.vm.saveList(); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager_2/lists', expect.objectContaining({ action: 'save', revision: 3, list: expect.objectContaining({ name: '草稿改名', members: [movie, series] }) }), expect.anything())
    expect(page.text()).toContain('片单已更新'); expect(page.vm.editing.name).toBe('草稿改名')
  })
  it('publishes asynchronously using a job id and displays the returned sync result', async () => {
    const saved = { ...newList('已保存'), members: [movie], targets:[testTarget] }
    const api = apiFor(saved)
    api.post.mockResolvedValue({ success: true, job_id: 'job-1', status: 'queued' })
    api.get.mockImplementation(async path => path.endsWith('/job') ? { success: true, job_id: 'job-1', status: 'success', results: [{ success: true, list_id: saved.id, list_name: saved.name, matched_count: 1, configured_count: 1, dry_run: false, plan: [{ kind: 'collection', id: 'collection', name: '合集', create: true, add: ['1'], remove: [], order: [] }] }] } : path.endsWith('/lists') ? { success: true, lists: [saved] } : { success: true, items: [] })
    const page = render(Page, { api }); await flushPromises()
    await button(page, '同步').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/publish', { list_id: saved.id, dry_run: false, allow_empty: false }, expect.anything())
    expect(api.get).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/job', expect.objectContaining({ params: { job_id: 'job-1' } }))
    expect(page.text()).toContain('新增 1 项'); expect(page.text()).toContain('任务完成')
  })
  it('keeps a waiting retry job active and renders one grouped history row until completion', async () => {
    const saved = {...newList('等待入库'),members:[movie],targets:[testTarget]}, api = apiFor(saved)
    let current = {id:'job-1',status:'waiting',attempt_count:1,next_retry_at:'2026-10-15T08:00:00Z',results:[]}
    api.post.mockResolvedValue({success:true,job_id:'job-1',status:'queued'})
    api.get.mockImplementation(async path => path.endsWith('/job') ? {success:true,job:current} : path.endsWith('/lists') ? {success:true,lists:[saved]} : path.endsWith('/history') ? {success:true,items:[{...current,history_id:'h1',list_name:saved.name}]} : {success:true,items:[]})
    const page = render(Page,{api}); await flushPromises()
    await button(page,'同步').trigger('click'); await flushPromises()
    await button(page,'确认同步').trigger('click'); await flushPromises()
    expect(page.text()).toContain('当前任务 · 等待媒体库识别')
    expect(page.text()).toContain('已检查 1 次 · 下次重试 16:00:00')
    expect(page.text()).not.toContain('任务已结束')
    expect(button(page,'同步全部').attributes('disabled')).toBeDefined()
    expect(page.find('.publish-progress').exists()).toBe(true)
    expect(page.findAll('.history-row')).toHaveLength(1)
    expect(page.find('.history-record-status').text()).toContain('等待媒体库识别')
    expect(page.find('.history-record-status').classes()).toContain('history-status-neutral')
    expect(page.emitted('action')).toBeUndefined()
    current = {...current,status:'success',attempt_count:2,next_retry_at:null}
    await button(page,'刷新任务').trigger('click'); await flushPromises()
    expect(page.text()).toContain('任务完成')
    expect(button(page,'同步全部').attributes('disabled')).toBeUndefined()
    expect(page.findAll('.history-row')).toHaveLength(1)
  })
  it('ID source resolves names before emitting candidates and does not mutate existing members', async () => {
    const request = vi.fn(async () => ({ success: true, items: [series] }))
    const add = render(AddMedia, { request, members: [movie] })
    const selects = add.findAllComponents(components.VSelect)
    selects[0].vm.$emit('update:modelValue', 'ids'); await flushPromises()
    add.findAllComponents(components.VSelect)[1].vm.$emit('update:modelValue', 'tv')
    await flushPromises(); add.findComponent(components.VTextarea).vm.$emit('update:modelValue', '10')
    await button(add, '解析片名').trigger('click'); await flushPromises()
    expect(request).toHaveBeenCalledWith('post', 'resolve', expect.objectContaining({ tmdb_ids: [10], media_type: 'tv', exclude_keys: ['movie:10'] }), expect.anything())
    expect(add.text()).toContain('电视剧名称'); expect(add.emitted('add')).toBeUndefined()
    await button(add, '全选').trigger('click'); await flushPromises()
    await button(add, '添加所选 1 项').trigger('click')
    expect(add.emitted('add')[0][0]).toEqual([series])
  })
  it('name search defaults to all types and offers an optional movie filter', async () => {
    const feature = { tmdb_id: 634649, media_type: 'movie', title: 'Spider-Man: No Way Home', year: 2021 }
    const request = vi.fn(async () => ({ success: true, items: [feature, series] }))
    const add = render(AddMedia, { request, members: [] })
    const filter = add.findAllComponents(components.VSelect)[1]
    expect(filter.props('modelValue')).toBe('all')
    expect(filter.props('items').map(item => item.value)).toEqual(['all', 'movie', 'tv'])
    add.findAllComponents(components.VTextField).find(field => field.props('label') === '电影或电视剧名称').vm.$emit('update:modelValue', '英雄无归'); await flushPromises()
    await button(add, '查询内容').trigger('click'); await flushPromises()
    expect(request).toHaveBeenLastCalledWith('post', 'search', expect.objectContaining({ query: '英雄无归', media_type: 'all' }), expect.anything())
    expect(add.text()).toContain('Spider-Man: No Way Home'); expect(add.text()).toContain('电视剧名称')
    filter.vm.$emit('update:modelValue', 'movie'); await flushPromises()
    await button(add, '查询内容').trigger('click'); await flushPromises()
    expect(request).toHaveBeenLastCalledWith('post', 'search', expect.objectContaining({ media_type: 'movie' }), expect.anything())
  })
  it('numeric ID lookup defaults to both types and shows equal-id movie and Series', async () => {
    const request = vi.fn(async () => ({ success: true, items: [movie, series] }))
    const add = render(AddMedia, { request, members: [] })
    add.findComponent(components.VSelect).vm.$emit('update:modelValue', 'ids'); await flushPromises()
    const idType = add.findAllComponents(components.VSelect)[1]
    expect(idType.props('modelValue')).toBe('all')
    expect(idType.props('items').map(item => item.value)).toEqual(['all', 'movie', 'tv'])
    add.findComponent(components.VTextarea).vm.$emit('update:modelValue', '10'); await flushPromises()
    await button(add, '解析片名').trigger('click'); await flushPromises()
    expect(request).toHaveBeenLastCalledWith('post', 'resolve', expect.objectContaining({ tmdb_ids: [10], media_type: 'all' }), expect.anything())
    expect(add.text()).toContain('电影名称'); expect(add.text()).toContain('电视剧名称')
    add.findComponent(components.VSelect).vm.$emit('update:modelValue', 'search'); await flushPromises()
    expect(add.findAllComponents(components.VSelect)[1].props('modelValue')).toBe('all')
  })
  it('shows unmapped host results with their source and prevents selecting them as TMDB members', async () => {
    const unresolved = { media_source: 'douban', media_id: '9', media_type: 'movie', title: '未关联影片', year: 2025, mapping_error: '尚未关联' }
    const feature = { ...movie, title: '正片', source_refs: [{ media_source: 'themoviedb', media_id: '10' }, { media_source: 'douban', media_id: '8' }] }
    const request = vi.fn(async () => ({ success: true, items: [feature, unresolved] }))
    const add = render(AddMedia, { request, members: [] })
    add.findAllComponents(components.VTextField).find(field => field.props('label') === '电影或电视剧名称').vm.$emit('update:modelValue', '片'); await flushPromises()
    await button(add, '查询内容').trigger('click'); await flushPromises()
    expect(add.text()).toContain('未关联影片'); expect(add.text()).toContain('豆瓣'); expect(add.text()).toContain('TMDB')
    const blocked = add.findAllComponents(components.VListItem).find(item => item.props('title') === '未关联影片')
    expect(blocked.props('disabled')).toBe(true)
    await button(add, '全选').trigger('click'); await flushPromises()
    await button(add, '添加所选 1 项').trigger('click')
    expect(add.emitted('add')[0][0]).toEqual([feature])
  })
  it('library lookup also defaults to all media types without a required type choice', async () => {
    const request = vi.fn(async () => ({ success: true, items: [movie, series] }))
    const add = render(AddMedia, { request, members: [], servers: [{ id: 'Emby', kind: 'emby', name: 'Emby' }] })
    add.findComponent(components.VSelect).vm.$emit('update:modelValue', 'library'); await flushPromises()
    const selects = add.findAllComponents(components.VSelect)
    expect(selects.find(select => select.props('label') === '类型筛选（可选）').props('modelValue')).toBe('all')
    selects.find(select => select.props('label') === '媒体服务器').vm.$emit('update:modelValue', 'Emby'); await flushPromises()
    await button(add, '查询内容').trigger('click'); await flushPromises()
    expect(request).toHaveBeenCalledWith('post', 'library-media', expect.objectContaining({ server_id: 'Emby', library_ids: [], media_type: 'all' }), expect.anything())
    expect(add.text()).toContain('电影名称'); expect(add.text()).toContain('电视剧名称')
  })
  it('TMDB collection query requires a mode and passes once explicitly', async () => {
    const request = vi.fn(async () => ({ success: true, items: [movie], source: { tmdb_id: 33, mode: 'once' } }))
    const add = render(AddMedia, { request, members: [] })
    add.findComponent(components.VSelect).vm.$emit('update:modelValue', 'tmdb'); await flushPromises()
    add.findAllComponents(components.VTextField).find(field => field.props('label') === 'TMDB 合集 ID').vm.$emit('update:modelValue', '33'); await flushPromises()
    await button(add, '查询内容').trigger('click'); await flushPromises()
    expect(request).toHaveBeenCalledWith('post', 'tmdb-collection', expect.objectContaining({ tmdb_id: 33, mode: 'once' }), expect.anything())
    expect(add.text()).toContain('以后不再检查')
  })
  it('supports keyboard buttons for independent typed playlist order', async () => {
    const list = editableList({ ...newList('test'), members: [movie, series], targets: [{ id: 't', server_id: 'Emby', kind: 'emby', collection: {}, playlists: [{ id: 'p', name: '顺序一', enabled: true, order: ['movie:10', 'tv:10'] }, { id: 'p2', name: '顺序二', enabled: true, order: ['movie:10', 'tv:10'] }] }] })
    const target = render(TargetEditor, { target: list.targets[0], members: list.members, servers: [{ id: 'Emby', kind: 'emby', name: 'Emby' }], request: vi.fn(async () => ({ items: [] })) })
    await flushPromises()
    await target.find('[aria-label="上移 电视剧名称"]').trigger('click')
    expect(list.targets[0].playlists[0].order).toEqual(['tv:10', 'movie:10'])
    expect(list.targets[0].playlists[1].order).toEqual(['movie:10', 'tv:10'])
  })

  it('keeps playlist editing available even when the media library directory is offline', async () => {
    const list = editableList({ ...newList('季排序'), members: [movie, series], targets: [{ id:'t', server_id:'Emby', kind:'emby', collection:{},
      playlists:[{ id:'p', enabled:true, order:['tv:10:season:2','movie:10','tv:10:season:1'] }] }] })
    const request = vi.fn(async () => { throw Error('媒体服务器离线') })
    const before = JSON.stringify(list), target = render(TargetEditor, { target:list.targets[0], members:list.members,
      servers:[{ id:'Emby', kind:'emby' }], request })
    await flushPromises()
    expect(target.text()).toContain('媒体服务器离线')
    expect(JSON.stringify(list)).toBe(before)
    expect(target.find('[aria-label="上移 电影名称"]').attributes('disabled')).toBeUndefined()
    expect(target.findAll('[data-order-key]').map(row => row.attributes('data-order-key'))).toEqual(['tv:10','movie:10'])
    expect(request.mock.calls.every(([_method,endpoint]) => endpoint === 'libraries')).toBe(true)
    await target.find('[aria-label="上移 电影名称"]').trigger('click')
    expect(list.targets[0].playlists[0].order).toEqual(['movie:10','tv:10'])
  })

  it('switches to Jellyfin without changing membership, season scope or order', async () => {
    const list = editableList({ ...newList('季目录'), members:[series], targets:[{ id:'t', server_id:'Emby', kind:'emby',
      collection:{remote_id:'old-collection'}, playlists:[{ id:'p', enabled:true, remote_id:'old-playlist', order:['tv:10'] }] }] })
    list.members[0].seasons = [3,4]
    const request = vi.fn(async () => ({ items:[] }))
    const editor = render(TargetEditor, { target:list.targets[0], members:list.members,
      servers:[{ id:'Emby', kind:'emby' },{ id:'Other', kind:'jellyfin' }], request })
    await flushPromises()
    editor.findAllComponents(components.VSelect).find(select => select.props('label') === '媒体服务器').vm.$emit('update:modelValue','Other')
    await flushPromises()
    expect(editor.findAll('[data-order-key]').map(row => row.attributes('data-order-key'))).toEqual(['tv:10'])
    expect(list.targets[0].playlists[0].order).toEqual(['tv:10'])
    expect(list.members[0].seasons).toEqual([3,4])
    expect(list.targets[0].kind).toBe('jellyfin')
    expect(list.targets[0].collection.remote_id).toBeNull()
    expect(list.targets[0].playlists[0].remote_id).toBeNull()
    expect(request.mock.calls.every(([_method,endpoint]) => endpoint === 'libraries')).toBe(true)
  })
})
describe('run history management', () => {
  function historyApi(count = 21) {
    let revision = 0
    let records = Array.from({ length: count }, (_, index) => ({ history_id: `h${index + 1}`, job_id: `j${index + 1}`, list_name: `记录${index + 1}`, status: 'success', created_at: '2026-10-14T12:00:00Z', results: [] }))
    const api = {
      get: vi.fn(async path => path.endsWith('/history') ? { success: true, items: [...records], revision } : path.endsWith('/lists') ? { success: true, lists: [] } : { success: true, items: [] }),
      post: vi.fn(async (path, payload) => {
        if (!path.endsWith('/history')) throw new Error('unexpected write')
        const before = records.length
        records = payload.action === 'clear' ? [] : records.filter(record => !payload.ids.includes(record.history_id))
        return { success: true, items: [...records], revision: ++revision, deleted_count: before - records.length }
      }),
    }
    return api
  }
  it('paginates by ten and clamps the page after deleting the last row', async () => {
    const api = historyApi(), page = render(Page, { api }); await flushPromises()
    expect(page.findAllComponents(components.VExpansionPanel)).toHaveLength(10)
    page.findComponent(components.VPagination).vm.$emit('update:modelValue', 3); await flushPromises()
    expect(page.findAllComponents(components.VExpansionPanel)).toHaveLength(1)
    await page.find('button[aria-label^="删除记录："]').trigger('click'); await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    await button(page, '确认删除').trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/history', { action: 'delete', ids: ['h21'] }, expect.anything())
    expect(page.findComponent(components.VPagination).props('modelValue')).toBe(2)
    expect(page.findAllComponents(components.VExpansionPanel)).toHaveLength(10)
  })
  it('keeps selections across pages for a single confirmed batch delete', async () => {
    const api = historyApi(23), page = render(Page, { api }); await flushPromises()
    page.findAllComponents(components.VCheckboxBtn)[1].vm.$emit('update:modelValue', true); await flushPromises()
    page.findComponent(components.VPagination).vm.$emit('update:modelValue', 3); await flushPromises()
    page.findAllComponents(components.VCheckboxBtn)[1].vm.$emit('update:modelValue', true); await flushPromises()
    await button(page, '删除所选（2）').trigger('click'); await flushPromises()
    await button(page, '确认删除').trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/history', { action: 'delete', ids: ['h1', 'h21'] }, expect.anything())
  })
  it('does not clear when confirmation is cancelled, and clears after confirmation', async () => {
    const api = historyApi(5), page = render(Page, { api }); await flushPromises()
    await button(page, '清空全部').trigger('click'); await flushPromises()
    await button(page, '取消').trigger('click'); await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
    await button(page, '清空全部').trigger('click'); await flushPromises()
    await button(page, '确认清空').trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/history', { action: 'clear' }, expect.anything())
    expect(page.text()).toContain('暂无运行记录')
  })
  it('preserves records and the page when deletion fails', async () => {
    const api = historyApi(), page = render(Page, { api }); await flushPromises()
    api.post.mockResolvedValue({ success: false, status: 'error', error_code: 'persistence_error', error: '记录未删除，请重试' })
    page.findComponent(components.VPagination).vm.$emit('update:modelValue', 3); await flushPromises()
    await page.find('button[aria-label^="删除记录："]').trigger('click'); await flushPromises()
    await button(page, '确认删除').trigger('click'); await flushPromises()
    expect(page.text()).toContain('记录未删除，请重试')
    expect(page.findComponent(components.VPagination).props('modelValue')).toBe(3)
    expect(page.findAllComponents(components.VExpansionPanel)).toHaveLength(1)
  })
  it('ignores a stale history refresh after successful deletion', async () => {
    const api = historyApi(5), old = await api.get('plugin/MediaLibraryPlaylistManager/history')
    const page = render(Page, { api }); await flushPromises()
    await button(page, '清空全部').trigger('click'); await flushPromises()
    await button(page, '确认清空').trigger('click'); await flushPromises()
    const currentGet = api.get.getMockImplementation()
    api.get.mockImplementation(path => path.endsWith('/history') ? Promise.resolve(old) : currentGet(path))
    await page.vm.refresh(); await flushPromises()
    expect(page.findAllComponents(components.VExpansionPanel)).toHaveLength(0)
    expect(page.text()).toContain('暂无运行记录')
  })
})

describe('saved target library names', () => {
  it('disables mixed Plex collections, preserves bindings and only permits manual reenable after restriction lifts', async () => {
    const target = reactive({id:'p',server_id:'Plex',kind:'plex',library_ids:[],collection:{enabled:true,remote_id:'500'},
      playlists:[{id:'pp',name:'观看',enabled:true,remote_id:'501',order:['tv:10','movie:10']}]})
    const editor = render(TargetEditor,{target,members:[movie,series],servers:[{id:'Plex',kind:'plex'}],request:vi.fn(async()=>({items:[]}))})
    await flushPromises()
    const toggle = () => editor.findAllComponents(components.VSwitch).find(node => node.props('label') === '同步为合集')
    expect(toggle().props('disabled')).toBe(true)
    expect(target.collection).toMatchObject({enabled:false,remote_id:'500'})
    expect(editor.text()).toContain('不能混合电影和电视剧')
    expect(target.playlists[0]).toMatchObject({enabled:true,remote_id:'501',order:['tv:10','movie:10']})
    await editor.setProps({members:[series]})
    expect(toggle().props('disabled')).toBe(false)
    expect(target.collection.enabled).toBe(false)
    toggle().vm.$emit('update:modelValue',true); await flushPromises()
    expect(target.collection.enabled).toBe(true)
    await editor.setProps({members:[{...series,seasons:[3,4]}]})
    expect(toggle().props('disabled')).toBe(true)
    expect(target.collection.enabled).toBe(false)
    expect(editor.text()).toContain('暂不支持指定季')
  })

  function savedTarget(server = 'Emby') {
    return reactive({ id: 't', server_id: server, kind: 'emby', library_ids: ['3'],
      collection: { enabled: true, name: '合集', remote_id: 'c' }, playlists: [] })
  }
  const servers = [{ id: 'Emby', name: 'Emby', kind: 'emby', configured: true }, { id: 'Other', name: 'Other', kind: 'emby', configured: true }]
  const librarySelect = editor => editor.findAllComponents(components.VSelect).find(select => select.props('label') === '全部')

  it('automatically restores saved library names each time the editor opens without changing the saved target', async () => {
    const target = savedTarget(), original = JSON.stringify(target)
    const request = vi.fn(async () => ({ success: true, items: [{ id: 3, name: '电影库' }] }))
    const props = { target, members: [], servers, request }
    const first = render(TargetEditor, props); await flushPromises()
    expect(librarySelect(first).props('items')).toContainEqual({ title: '电影库', value: '3' })
    expect(librarySelect(first).find('.v-chip').text()).toContain('电影库')
    first.unmount()
    const reopened = render(TargetEditor, props); await flushPromises()
    expect(librarySelect(reopened).find('.v-chip').text()).toContain('电影库')
    expect(request).toHaveBeenCalledTimes(2)
    expect(request.mock.calls.every(([method, endpoint, body]) => method === 'post' && endpoint === 'libraries' && body.server_id === 'Emby')).toBe(true)
    expect(JSON.stringify(target)).toBe(original)
    expect(reopened.text()).toContain('匹配范围')
  })

  it('keeps the saved scope after a failed automatic read and allows manual retry', async () => {
    const target = savedTarget(), original = JSON.stringify(target)
    const request = vi.fn().mockRejectedValueOnce(new Error('媒体服务器离线')).mockResolvedValueOnce({ success: true, items: [{ id: '3', name: '电影库' }] })
    const editor = render(TargetEditor, { target, members: [], servers, request }); await flushPromises()
    expect(editor.text()).toContain('媒体服务器离线')
    expect(target.library_ids).toEqual(['3'])
    await button(editor, '刷新媒体库').trigger('click'); await flushPromises()
    expect(librarySelect(editor).find('.v-chip').text()).toContain('电影库')
    expect(editor.text()).not.toContain('媒体服务器离线')
    expect(JSON.stringify(target)).toBe(original)
  })

  it.each([true, false])('does not let an old server completion overwrite the current libraries, failed=%s', async failed => {
    let finishOld, resolveNew
    const request = vi.fn((method, endpoint, body) => new Promise((resolve, reject) => {
      if (body.server_id === 'Emby') finishOld = () => failed ? reject(new Error('旧服务器请求失败')) : resolve({ success: true, items: [{ id: '3', name: '旧服务器的电影库' }] })
      else resolveNew = resolve
    }))
    const target = savedTarget(), editor = render(TargetEditor, { target, members: [], servers, request }); await flushPromises()
    editor.findAllComponents(components.VSelect).find(select => select.props('label') === '媒体服务器').vm.$emit('update:modelValue', 'Other'); await flushPromises()
    expect(target.library_ids).toEqual([])
    resolveNew({ success: true, items: [{ id: '3', name: '另一个服务器的电影库' }] }); await flushPromises()
    finishOld(); await flushPromises()
    expect(librarySelect(editor).props('items')).toEqual([{ title: '另一个服务器的电影库', value: '3' }])
    expect(editor.text()).not.toContain('旧服务器请求失败')
  })
  it('loads names when the server configuration arrives after the target editor', async () => {
    const target = savedTarget(), request = vi.fn(async () => ({ success: true, items: [{ id: '3', name: '电影库' }] }))
    const editor = render(TargetEditor, { target, members: [], servers: [], request }); await flushPromises()
    expect(request).not.toHaveBeenCalled()
    await editor.setProps({ servers }); await flushPromises()
    expect(request).toHaveBeenCalledOnce()
    expect(librarySelect(editor).find('.v-chip').text()).toContain('电影库')
    expect(target.library_ids).toEqual(['3'])
  })
})

describe('subscription outcomes', () => {
  it.each([
    ['movie:10', 'subscribed', true, movie.title, ''],
    ['tv:10', 'error', false, series.title, '宿主订阅未成功'],
  ])('identifies the typed member for %s', (key, status, success, expectedTitle, error) => {
    const result = render(RunResult, { members: [movie, series], result: { key, status, success, error, operation: 'subscribe' } })
    expect(result.text()).toContain('订阅内容：')
    expect(result.text()).toContain(expectedTitle)
    if (error) expect(result.text()).toContain(error)
  })
})

describe('output binding recovery', () => {
  it.each([true, false])('shows the missing-output plan with create=%s', create => {
    const result = render(RunResult, { members: [movie], result: { status: 'success', dry_run: true,
      plan: [{ kind: 'collection', name: '合集', missing_remote_id: 'gone', create, add: [] }] } })
    expect(result.text()).toContain(create ? '原合集已从媒体库中删除，将重新创建' : '原合集已从媒体库中删除，目前没有可同步的内容')
  })
})

describe('lightweight host actions', () => {
  it('treats a collection as navigation only and never sends it as a movie', async () => {
    const api = { get: vi.fn(), post: vi.fn() }
    const action = render(MediaActions, { api, context: { version: 1, surface: 'tmdb_collection', media: { tmdb_id: 33, media_type: 'collection', title: '合集' } } })
    await flushPromises(); expect(api.get).not.toHaveBeenCalled()
    expect(action.text()).toContain('一次性导入')
    await button(action, '管理片单').trigger('click')
    expect(action.emitted('navigate')[0]).toEqual([{ navKey: 'main' }]); expect(api.post).not.toHaveBeenCalled()
  })
  it('adds a Series atomically without changing automatic policies', async () => {
    const saved = { ...newList('片单'), members: [movie] }
    const api = apiFor(saved)
    api.post.mockResolvedValue({ success: true, list: { ...saved, members: [movie, series] } })
    const action = render(MediaActions, { api, context: { version: 1, surface: 'media_detail', media: series } })
    await flushPromises(); await button(action, '加入').trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/members', { list_id: saved.id, action: 'add', items: [expect.objectContaining({ tmdb_id: 10, media_type: 'tv', title: '电视剧名称' })] }, expect.anything())
    expect(button(action, '移除').exists()).toBe(true)
  })
  it('quick add applies the chosen season scope without enabling subscription', async () => {
    const saved = newList('后两季'), api = apiFor(saved)
    api.post.mockImplementation(async (path,body) => path.endsWith('/seasons') ? {success:true,items:[3,4].map(season_number => ({season_number,episode_count:24}))} : {success:true,list:{...saved,members:body.items}})
    const action = render(MediaActions, {api,context:{media:series}}); await flushPromises()
    const scope = action.findComponent(SeasonScope)
    scope.findComponent(components.VSelect).vm.$emit('update:modelValue','selected'); await flushPromises()
    scope.findAllComponents(components.VSelect)[1].vm.$emit('update:modelValue',[3,4]); await flushPromises()
    await button(action,'加入').trigger('click'); await flushPromises()
    const payload = api.post.mock.calls.find(([path]) => path.endsWith('/members'))[1]
    expect(payload.items).toEqual([expect.objectContaining({...series,seasons:[3,4]})])
    expect(saved.policy.auto_subscribe).toBe(false)
  })
  it('quick create resolves a missing title and leaves policies and targets off', async () => {
    const saved = newList('已有')
    const api = apiFor(saved)
    api.post.mockImplementation(async (path, body) => path.endsWith('/resolve') ? { success: true, items: [series] } : { success: true, list: body.list })
    const action = render(MediaActions, { api, context: { media: { tmdb_id: 10, media_type: 'tv' } } }); await flushPromises()
    action.findAllComponents(components.VTextField).find(field => field.props('label') === '新片单名称').vm.$emit('update:modelValue', '新片单'); await flushPromises()
    await button(action, '新建并加入').trigger('click'); await flushPromises()
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/resolve', { tmdb_ids: [10], media_type: 'tv' }, expect.anything())
    expect(api.post).toHaveBeenCalledWith('plugin/MediaLibraryPlaylistManager/lists', expect.objectContaining({ action: 'save', list: expect.objectContaining({ name: '新片单', members: [series], targets: [], policy: { auto_subscribe: false, organize_category: '', publish_on_library: false } }) }), expect.anything())
  })
  it('rejects Episode contexts and keeps the management escape route', async () => {
    const api = { get: vi.fn(), post: vi.fn() }
    const action = render(MediaActions, { api, context: { media: { ...series, type: 'Episode' } } })
    await flushPromises(); expect(api.get).not.toHaveBeenCalled(); expect(action.text()).toContain('不是电影/整部电视剧')
    expect(button(action, '管理片单').exists()).toBe(true)
  })
})
