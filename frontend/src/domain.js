export const clone = value => JSON.parse(JSON.stringify(value))
export const uid = () => globalThis.crypto?.randomUUID?.() || `local-${Date.now()}-${Math.random().toString(36).slice(2)}`

const supportedMediaServerKinds = new Set(['emby', 'jellyfin', 'plex'])
export const supportsMediaServer = server => supportedMediaServerKinds.has(String(typeof server === 'string' ? server : server?.kind).toLowerCase())
export function mediaServerTitle(server) {
  const name = server.name || server.id || '媒体服务器'
  const kind = { emby: 'Emby', jellyfin: 'Jellyfin', plex: 'Plex' }[String(server.kind).toLowerCase()] || server.kind
  const type = kind && !name.toLowerCase().includes(String(kind).toLowerCase()) ? ` · ${kind}` : ''
  const state = !supportsMediaServer(server) ? ' · 暂不支持' : server.configured === false ? ' · 未配置' : ''
  return `${name}${type}${state}`
}

export function retrySummary(record) {
  const parts = []
  if (record.attempt_count > 0) parts.push(`已检查 ${record.attempt_count} 次`)
  if (record.status === 'waiting' && record.next_retry_at) {
    const next = new Date(record.next_retry_at)
    if (!Number.isNaN(next.getTime())) parts.push(`下次重试 ${next.toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' })}`)
  }
  return parts.join(' · ')
}

export function mediaKey(media) {
  const id = Number(media?.tmdb_id)
  return ['movie', 'tv'].includes(media?.media_type) && Number.isSafeInteger(id) && id > 0
    ? `${media.media_type}:${id}` : ''
}

export function uniqueMembers(items = [], exclude = []) {
  const seen = new Set(exclude)
  return items.filter(item => {
    const key = mediaKey(item)
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  }).map(item => ({ ...clone(item), tmdb_id: Number(item.tmdb_id) }))
}

export function candidateKey(item) {
  const memberKey = mediaKey(item)
  if (memberKey) return memberKey
  return ['movie', 'tv'].includes(item?.media_type) && item?.media_source && item?.media_id
    ? `${item.media_type}:${item.media_source}:${item.media_id}` : ''
}

export function uniqueCandidates(items = [], exclude = []) {
  const seen = new Set(exclude)
  return items.filter(item => {
    const key = candidateKey(item)
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  }).map(clone)
}

export function normaliseOrder(order, members) {
  const keys = [...new Set(members.map(mediaKey).filter(Boolean))]
  const valid = new Set(keys)
  // Legacy season positions collapse to their selected work at its first
  // position. Editing always has exactly the same identities as membership.
  const saved = [...new Set((order || []).filter(key => /^(movie:[1-9]\d*|tv:[1-9]\d*(?::season:\d+)?)$/.test(key)).map(playlistParent).filter(key => valid.has(key)))]
  return [...saved, ...keys.filter(key => !saved.includes(key))]
}

export const playlistParent = key => String(key).split(':').slice(0, 2).join(':')

export function newList(name = '') {
  return { id: uid(), name, enabled: true, revision: 0, members: [], sources: [],
    policy: { auto_subscribe: false, organize_category: '', publish_on_library: false }, targets: [] }
}

export function editableList(list) {
  const result = { ...newList(), ...clone(list) }
  result.members = uniqueMembers(result.members)
  result.sources ||= []
  result.policy = { auto_subscribe: false, organize_category: '', publish_on_library: false, ...result.policy }
  result.targets = (result.targets || []).map(target => ({ ...target, library_ids: target.library_ids || [],
    collection: { enabled: false, name: '', ...target.collection },
    playlists: (target.playlists || []).map(playlist => ({ ...playlist,
      order: normaliseOrder(playlist.order, result.members) })) }))
  return applyCollectionRestrictions(result)
}

export function collectionDisabledReason(kind, members = []) {
  if (String(kind).toLowerCase() !== 'plex') return ''
  if (new Set(members.map(member => member.media_type)).size > 1) return 'Plex 合集不能混合电影和电视剧，请使用播放列表。'
  if (members.some(member => member.seasons != null)) return 'Plex 合集暂不支持指定季，请使用播放列表。'
  return ''
}

function applyCollectionRestrictions(list) {
  for (const target of list.targets) {
    if (collectionDisabledReason(target.kind, list.members)) target.collection.enabled = false
  }
  return list
}

export function mergeCandidates(list, items, source) {
  const added = uniqueMembers(items, list.members.map(mediaKey))
  list.members.push(...added)
  applyCollectionRestrictions(list)
  for (const target of list.targets) {
    for (const playlist of target.playlists) playlist.order = normaliseOrder(playlist.order, list.members)
  }
  if (source) {
    const existing = list.sources.find(item => Number(item.tmdb_id) === Number(source.tmdb_id))
    const seen = [...new Set([...(existing?.seen_keys || []), ...(source.seen_keys || []), ...items.map(mediaKey).filter(Boolean)])]
    const next = { ...clone(source), seen_keys: seen }
    if (existing) Object.assign(existing, next)
    else list.sources.push(next)
  }
  return added.length
}

export function removeMember(list, key) {
  list.members = list.members.filter(member => mediaKey(member) !== key)
  for (const target of list.targets) {
    for (const playlist of target.playlists) playlist.order = normaliseOrder(playlist.order, list.members)
  }
}

export function moveOrder(order, from, to) {
  const next = [...order]
  if (from < 0 || to < 0 || from >= next.length || to >= next.length || from === to) return next
  const [key] = next.splice(from, 1)
  next.splice(to, 0, key)
  return next
}

export function parseIds(text) {
  const parts = String(text || '').trim().split(/[\s,，;；]+/).filter(Boolean)
  if (!parts.length || parts.some(part => !/^\d+$/.test(part) || Number(part) <= 0 || !Number.isSafeInteger(Number(part)))) {
    throw new Error('请输入有效的正整数 TMDB ID，使用空格、逗号或换行分隔。')
  }
  return [...new Set(parts.map(Number))]
}

export const mediaTitle = member => member?.title || member?.name || `TMDB ${member?.tmdb_id || ''}`
const sourceNames = { themoviedb: 'TMDB', douban: '豆瓣', bangumi: 'Bangumi', anilist: 'AniList', imdb: 'IMDb', tvdb: 'TVDB' }
export const sourceName = source => sourceNames[source] || source
export const seasonScopeValid = member => member?.media_type !== 'tv' || member.seasons == null || (Array.isArray(member.seasons) && member.seasons.length > 0 && member.seasons.every(number => Number.isInteger(number) && number >= 0))
export const mediaSubtitle = member => [member?.media_type === 'tv' ? `电视剧 · ${Array.isArray(member.seasons) ? member.seasons.length ? member.seasons.map(number => number === 0 ? '特别篇' : `第 ${number} 季`).join('、') : '请选择季' : '整部'}` : '电影', member?.year,
  mediaKey(member) ? `TMDB ${member.tmdb_id}` : member?.media_source ? `${sourceName(member.media_source)} ${member.media_id} · 未关联 TMDB，暂不可加入` : '',
  member?.source_refs?.length ? `来源：${[...new Set(member.source_refs.map(ref => sourceName(ref.media_source)))].join(' / ')}` : '',
].filter(Boolean).join(' · ')
