import { describe, expect, it } from 'vitest'
import { editableList, mediaKey, mergeCandidates, moveOrder, newList, normaliseOrder, parseIds, removeMember, uniqueMembers, supportsMediaServer, mediaServerTitle, retrySummary } from '../src/domain'
const movie = { tmdb_id: 10, media_type: 'movie', title: '电影' }
const series = { tmdb_id: 10, media_type: 'tv', title: '电视剧' }
describe('typed membership and draft isolation', () => {
  it('keeps server names and type visible while centralizing supported types', () => {
    expect(supportsMediaServer('Plex')).toBe(true)
    expect(supportsMediaServer('emby')).toBe(true)
    expect(supportsMediaServer('Jellyfin')).toBe(true)
    expect(mediaServerTitle({id:'j',name:'客厅',kind:'jellyfin'})).toBe('客厅 · Jellyfin')
    expect(supportsMediaServer({kind:'unsupported'})).toBe(false)
    expect(supportsMediaServer(undefined)).toBe(false)
    expect(mediaServerTitle({id:'server-1',name:'家庭影院',kind:'emby'})).toBe('家庭影院 · Emby')
    expect(mediaServerTitle({id:'server-1',name:'家庭影院 Emby',kind:'emby'})).toBe('家庭影院 Emby')
    expect(mediaServerTitle({id:'server-1',name:'客厅',kind:'unsupported'})).toBe('客厅 · unsupported · 暂不支持')
    expect(mediaServerTitle({id:'server-1',name:'家庭影院',kind:'emby',configured:false})).toBe('家庭影院 · Emby · 未配置')
  })
  it('disables incompatible Plex collections without changing members, order or binding', () => {
    const saved = { ...newList('混合'), members:[movie,series], targets:[{ id:'p',kind:'plex', collection:{enabled:true,remote_id:'500'},playlists:[{id:'pp',order:['tv:10','movie:10']}] },
      {id:'e',kind:'emby',collection:{enabled:true},playlists:[]}] }
    const draft = editableList(saved)
    expect(draft.targets[0].collection).toMatchObject({enabled:false,remote_id:'500'})
    expect(draft.targets[1].collection.enabled).toBe(true)
    expect(draft.members).toEqual(saved.members)
    expect(draft.targets[0].playlists[0].order).toEqual(['tv:10','movie:10'])
    removeMember(draft,'tv:10')
    expect(draft.targets[0].collection.enabled).toBe(false)
    expect(saved.targets[0].collection.enabled).toBe(true)
  })
  it('enforces Plex restrictions on a followed-source merge and selected-season save', () => {
    const draft = editableList({...newList('Plex'),members:[series],targets:[{id:'p',kind:'plex',collection:{enabled:true},playlists:[]}]})
    mergeCandidates(draft,[movie])
    expect(draft.targets[0].collection.enabled).toBe(false)
    const seasons = editableList({...newList('季'),members:[{...series,seasons:[3,4]}],targets:[{id:'p',kind:'plex',collection:{enabled:true},playlists:[]}]})
    expect(seasons.targets[0].collection.enabled).toBe(false)
  })
  it('keeps grouped retry progress compact and omits invalid retry times', () => {
    expect(retrySummary({status:'waiting',attempt_count:2,next_retry_at:'2026-10-15T08:00:00Z'})).toBe('已检查 2 次 · 下次重试 16:00:00')
    expect(retrySummary({status:'success',attempt_count:3,next_retry_at:'2026-10-15T08:00:00Z'})).toBe('已检查 3 次')
    expect(retrySummary({status:'waiting',next_retry_at:'invalid'})).toBe('')
  })
  it('distinguishes movies and Series with equal TMDB ids and rejects Episode', () => {
    expect(uniqueMembers([movie, series, movie, { ...series, media_type: 'episode' }])).toEqual([movie, series])
    expect(mediaKey({ ...movie, media_type: 'episode' })).toBe('')
  })
  it('does not mutate the committed list when candidates are added or removed', () => {
    const saved = { ...newList('片单'), members: [movie], targets: [{ id: 't', collection: {}, playlists: [{ id: 'p', order: ['movie:10'] }] }] }
    const draft = editableList(saved)
    expect(mergeCandidates(draft, [movie, series])).toBe(1)
    expect(draft.targets[0].playlists[0].order).toEqual(['movie:10', 'tv:10'])
    removeMember(draft, 'movie:10')
    expect(draft.targets[0].playlists[0].order).toEqual(['tv:10'])
    expect(saved.members).toEqual([movie])
    expect(saved.targets[0].playlists[0].order).toEqual(['movie:10'])
  })
  it('retains seen keys so removed and deselected followed-source members stay excluded', () => {
    const list = newList('关注')
    mergeCandidates(list, [movie], { tmdb_id: 33, mode: 'follow', seen_keys: ['movie:10', 'movie:99'] })
    removeMember(list, 'movie:10')
    expect(list.sources[0].seen_keys).toEqual(['movie:10', 'movie:99'])
  })
  it('preserves explicit once snapshot mode', () => {
    const list = newList('快照')
    mergeCandidates(list, [movie], { tmdb_id: 33, mode: 'once', name: '合集' })
    expect(list.sources[0]).toMatchObject({ mode: 'once', seen_keys: ['movie:10'] })
  })
  it('removes stale order keys, de-duplicates and appends new typed members', () => {
    expect(normaliseOrder(['tv:10', 'tv:10', 'movie:999'], [movie, series])).toEqual(['tv:10', 'movie:10'])
    expect(moveOrder(['movie:10', 'tv:10'], 1, 0)).toEqual(['tv:10', 'movie:10'])
    expect(moveOrder(['movie:10'], 0, -1)).toEqual(['movie:10'])
  })
  it('collapses legacy season positions to one selected work without mutating the stored list', () => {
    const order = ['tv:10:season:2', 'movie:10', 'tv:10:season:1']
    expect(normaliseOrder(order, [movie, series])).toEqual(['tv:10','movie:10'])
    const saved = { ...newList('旧配置'), members:[movie,series], targets:[{ id:'t', collection:{}, playlists:[{ id:'p',order }] }] }
    expect(editableList(saved).targets[0].playlists[0].order).toEqual(['tv:10','movie:10'])
    expect(saved.targets[0].playlists[0].order).toEqual(order)
    expect(normaliseOrder(order, [movie])).toEqual(['movie:10'])
  })
  it('parses whole ids and rejects partially numeric or invalid ids', () => {
    expect(parseIds('1，2 2\n3;4')).toEqual([1, 2, 3, 4])
    for (const text of ['10abc', '0', '-1', '1.2', '']) expect(() => parseIds(text)).toThrow()
  })
  it('defaults all automation and publication off', () => {
    const list = newList('新建')
    expect(list.policy).toEqual({ auto_subscribe: false, organize_category: '', publish_on_library: false })
    expect(list.targets).toEqual([])
  })
})
