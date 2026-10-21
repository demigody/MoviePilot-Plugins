export class PluginError extends Error {
  constructor(message, status = 'error', detail = {}) { super(message); this.status = status; this.detail = detail }
}

const statusText = {
  empty: '没有找到内容，请修改条件后重试。', timeout: '请求超时，请稍后重试。',
  cancelled: '查询已取消。', cooldown: '查询正在冷却，请稍后重试。', busy: '服务正在处理其他任务，请稍后重试。',
  unsupported: '当前媒体服务器不支持此操作。', conflict: '片单已被其他操作更新。请保留当前草稿，重新读取后再保存。',
  provider_error: '上游服务读取失败。', server_error: '媒体服务器读取失败。', invalid_input: '输入内容无效。',
}
export const statusMessage = (status, fallback) => statusText[status] || errorMessage(fallback) || '操作失败，请重试。'
export function errorMessage(error) {
  const message = typeof error === 'string' ? error : error?.message || error?.detail || ''
  if (typeof message !== 'string' || !message) return ''
  if (/[\u3400-\u9fff]/u.test(message)) return message
  if (/^(?:Network Error|Failed to fetch|Load failed)$/i.test(message)) return '无法连接服务，请检查网络后重试。'
  if (/timeout|timed out/i.test(message)) return statusText.timeout
  if (/abort|cancel/i.test(message)) return statusText.cancelled
  const http = message.match(/^Request failed with status code (\d{3})$/)
  if (http) return `服务器返回 HTTP ${http[1]}，请检查服务状态和访问权限。`
  return '操作失败，请稍后重试。'
}

export function createClient(api, pluginId = 'EmbyCollectionSync') {
  return async function request(method, name, payload, options = {}) {
    if (typeof api?.[method] !== 'function') throw new PluginError('插件 API 尚未连接，请从 MoviePilot V3 插件页面打开。')
    try {
      const path = `plugin/${pluginId}/${name}`
      const config = { timeout: 25000, ...options }
      const result = method === 'get'
        ? await api.get(path, { ...config, params: payload })
        : await api[method](path, payload, config)
      if (!result || typeof result !== 'object') throw new PluginError('服务返回了无效响应。')
      if (result.success === false) {
        const status = result.status === 'error' ? result.error_code || 'error' : result.status || result.error_code || 'error'
        throw new PluginError(errorMessage(result.error) || statusMessage(status), status, result)
      }
      return result
    } catch (error) {
      if (error instanceof PluginError) throw error
      if (error?.code === 'ERR_CANCELED' || error?.name === 'AbortError') throw new PluginError(statusMessage('cancelled'), 'cancelled')
      if (['ECONNABORTED', 'ETIMEDOUT'].includes(error?.code)) throw new PluginError(statusMessage('timeout'), 'timeout')
      throw new PluginError(errorMessage(error?.response?.data?.error) || errorMessage(error) || '请求失败，请检查连接。', error?.response?.data?.status || 'error')
    }
  }
}
