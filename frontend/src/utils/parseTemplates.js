// 日志解析模板库：给「解析规则」向导用，用户只需选类型 + 贴一行真实日志。
//
// 每个模板 = 一份解析器定义 + 契约字段候选 + 默认扩展属性。
// 这里只描述「怎么切」，不写死字段映射——向导会按样例实际抽出的字段再做映射，
// 避免模板里的键名和现场日志对不上（例如 ts / time / @timestamp 三种写法）。

export const CONTRACT_FIELDS = [
  { key: 'level', label: '级别', hint: 'INFO / WARN / ERROR，用于筛选和告警' },
  { key: 'source', label: '来源', hint: '应用名或模块名，如 order-service / nginx' },
  { key: 'host', label: '主机', hint: '产生日志的机器，缺省由采集端补齐' },
  { key: 'message', label: '内容', hint: '检索和分析展示的正文' },
  { key: 'status', label: '状态码', hint: 'HTTP 状态码，5xx 会自动判为 ERROR' },
]

const NGINX_PATTERN =
  '%{COMMONAPACHELOG} "%{NOTSPACE:referer}" "%{GREEDYDATA:user_agent}"'

const JAVA_PATTERN =
  '%{TIMESTAMP_ISO8601:ts}\\s+\\[%{JAVATHREAD:thread}\\]\\s+%{LOGLEVEL:level}\\s+' +
  '%{JAVACLASS:logger}\\s+-\\s+%{GREEDYDATA:message}'

const SYSLOG_PATTERN =
  '%{SYSLOGTIMESTAMP:body_ts}\\s+%{HOSTNAME:host}\\s+' +
  '%{WORD:facility}\\.%{WORD:severity_name}\\s+(?P<source>[^\\s:]+):?\\s+%{GREEDYDATA:message}'

export const PARSE_TEMPLATES = [
  {
    key: 'nginx',
    icon: 'web',
    name: 'Nginx / Apache 访问日志',
    summary: 'IP、方法、路径、状态码、耗时一次抽全；5xx 自动算 ERROR',
    sample:
      '10.1.1.20 - - [21/Nov/2026:22:10:00 +0800] "GET /api/orders?id=42 HTTP/1.1" 200 1234 "-" "curl/7.68.0"',
    pathHint: '/var/log/nginx/access.log',
    parser: { type: 'grok', pattern: NGINX_PATTERN },
    // 这些字段固定进扩展属性，向导还会把其余抽出的字段一并放进去
    attrs: {
      'attrs.clientip': 'clientip',
      'attrs.method': 'method',
      'attrs.request': 'request',
      'attrs.bytes': 'bytes:int',
      'attrs.referer': 'referer',
      'attrs.user_agent': 'user_agent',
      'attrs.body_ts': 'ts',
    },
  },
  {
    key: 'java',
    icon: 'java',
    name: 'SpringBoot / Java logback',
    summary: '时间、线程、级别、类名、正文；异常栈需采集端做多行合并',
    sample:
      '2026-11-21 22:10:00.123 [http-nio-8080-exec-3] INFO  c.s.i.order.OrderController - 开始处理订单',
    pathHint: '/var/log/app/*.log',
    parser: { type: 'grok', pattern: JAVA_PATTERN },
    attrs: {
      'attrs.thread': 'thread',
      'attrs.logger': 'logger',
      'attrs.body_ts': 'ts',
    },
  },
  {
    key: 'syslog',
    icon: 'system',
    name: '系统日志 Syslog',
    summary: 'RFC3164 格式，抽主机、facility、severity 与正文',
    sample:
      'Nov 21 22:10:00 node-01 authpriv.warn sshd: Failed password for root from 10.1.1.9 port 22 ssh2',
    pathHint: '/var/log/syslog',
    parser: { type: 'grok', pattern: SYSLOG_PATTERN },
    attrs: {
      'attrs.facility': 'facility',
      'attrs.severity_name': 'severity_name',
      'attrs.body_ts': 'body_ts',
    },
  },
  {
    key: 'json',
    icon: 'json',
    name: 'JSON 结构化日志',
    summary: '一行一个 JSON 对象，自动列出所有字段供你挑选',
    sample:
      '{"ts":"2026-11-21T22:10:00+08:00","level":"INFO","service":"order-service","host":"node-01","msg":"订单创建成功","orderId":"A1001","costMs":12}',
    pathHint: '/var/log/app/*.json',
    parser: { type: 'json' },
    attrs: {},
  },
  {
    key: 'kv',
    icon: 'kv',
    name: '键值对（Key=Value）',
    summary: 'k1=v1 k2=v2 形式的应用日志或设备日志',
    sample:
      'ts=2026-11-21T22:10:00 level=INFO service=order-service msg="订单创建成功" orderId=A1001',
    pathHint: '/var/log/app/*.log',
    parser: { type: 'kv', separator: '=', split: ' ' },
    attrs: {},
  },
]

export function findTemplate(key) {
  return PARSE_TEMPLATES.find((t) => t.key === key) || null
}

// 自动识别：只做「像不像」判断，不追求覆盖所有格式；识别不出就让用户选。
export function detectTemplate(sample) {
  const text = String(sample || '').trim()
  if (!text) return null
  if (text.startsWith('{')) {
    try {
      const obj = JSON.parse(text)
      if (obj && typeof obj === 'object' && !Array.isArray(obj)) return findTemplate('json')
    } catch { /* 不是完整 JSON，继续往下猜 */ }
  }
  if (/\b(?:GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH)\s+\S+\s+HTTP\/\d\.\d/.test(text)) {
    return findTemplate('nginx')
  }
  if (/\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b/.test(text)
      && /\[\S*(?:thread|exec|nio|pool)\S*\]/i.test(text)) {
    return findTemplate('java')
  }
  if (/^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+/.test(text)) {
    return findTemplate('syslog')
  }
  if (/^[\w.-]+=[^\s]+(?:\s+[\w.-]+=[^\s]+)+/.test(text)) {
    return findTemplate('kv')
  }
  if (/\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b/.test(text)) {
    return findTemplate('java')
  }
  return null
}

// 常见别名的自动匹配，减少“模板抽出来了但没填进平台字段”的困惑
const ALIASES = {
  level: ['level', 'loglevel', 'severity', 'severity_name', 'levelname', 'lvl'],
  source: ['source', 'logger', 'service', 'app', 'component', 'module', 'name'],
  host: ['host', 'hostname', 'node', 'node_name', 'server'],
  message: ['message', 'msg', 'log', 'content', 'text', 'request'],
  status: ['status', 'status_code', 'statuscode', 'http_status', 'code'],
}

export function pickAlias(fields, contractKey, fallback) {
  const keys = Object.keys(fields || {})
  const lower = new Map(keys.map((k) => [k.toLowerCase(), k]))
  for (const alias of ALIASES[contractKey] || []) {
    if (lower.has(alias)) return lower.get(alias)
  }
  if (fallback && keys.includes(fallback)) return fallback
  return ''
}

// 契约字段默认映射：模板没写死，靠样例实际抽出的字段来选
export function defaultMapping(fields, template) {
  const out = {}
  for (const f of CONTRACT_FIELDS) {
    const hit = pickAlias(fields, f.key)
    if (hit) out[f.key] = f.key === 'status' ? `${hit}:int` : hit
  }
  const used = new Set(Object.values(out).map((v) => String(v).split(':')[0]))
  const attrs = { ...(template?.attrs || {}) }
  const mappedAttrSources = new Set(Object.values(attrs).map((v) => String(v).split(':')[0]))
  for (const key of Object.keys(fields || {})) {
    if (used.has(key) || mappedAttrSources.has(key)) continue
    const target = key === 'ts' || key === 'time' || key === '@timestamp'
      ? 'attrs.body_ts'
      : `attrs.${key}`
    if (attrs[target] === undefined) attrs[target] = key
  }
  return { ...out, ...attrs }
}

export function ruleCode(templateKey) {
  const suffix = Date.now().toString(36) + Math.random().toString(36).slice(2, 5)
  return { parser: `tpl-${templateKey}-${suffix}`, rule: `rule-${templateKey}-${suffix}` }
}
