// 悬浮窗回归：真导入 widget.js（不是逻辑复刻），用 DOM/storage/fetch 桩驱动。
// 覆盖「换用户后报 conversation not owned by user」三层自愈。
//
//   node widget_regression.mjs [widget.js 路径]
//
// 退出码 0 = 全过。需先有 node（无第三方依赖）。
import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const SRC = process.argv[2] || fileURLToPath(new URL('../static-src/ai_company/widget.js', import.meta.url))
const src = readFileSync(SRC, 'utf8')
const K_CID = 'aicw:cid'
const K_UID = 'aicw:uid'

let pass = 0, fail = 0
const ck = (c, l, e = '') => (c ? (pass++, console.log(`  [PASS] ${l}`)) : (fail++, console.log(`  [FAIL] ${l}  ${e}`)))
process.on('unhandledRejection', (e) => ck(false, `未捕获拒绝: ${e && e.message}`))

const def = (k, v) => Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true })
const mkStore = () => {
  const m = new Map()
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), clear: () => m.clear() }
}
const makeEl = (tag = 'div') => {
  const el = {
    tagName: String(tag).toUpperCase(), _html: '', _tc: '', dataset: {}, style: {}, children: [], value: '',
    scrollTop: 0, scrollHeight: 0, title: '', _qs: new Map(),
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { this.children.push(c); return c }, removeChild() {}, remove() {}, setAttribute() {}, removeAttribute() {},
    insertAdjacentHTML() {}, focus() {}, blur() {}, click() {}, scrollTo() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
    querySelector(s) { if (!this._qs.has(s)) this._qs.set(s, makeEl()); return this._qs.get(s) },
    querySelectorAll: () => [], closest: () => null,
    addEventListener(t, fn) { listeners.push({ el: this, type: t, fn }) }, removeEventListener() {},
  }
  Object.defineProperty(el, 'innerHTML', { get() { return this._html }, set(v) { this._html = String(v); if (this._html.includes('aicw-fab')) ROOT = this } })
  Object.defineProperty(el, 'textContent', { get() { return this._tc }, set(v) { this._tc = String(v); TEXT.push(this._tc) } })
  Object.defineProperty(el, 'childElementCount', { get() { return this.children.length } })
  return el
}

let listeners = [], TEXT = [], ROOT = null, calls = [], routes = []
const doc = {
  readyState: 'complete', title: '工作台', cookie: '', body: makeEl('body'), documentElement: makeEl('html'),
  createElement: (t) => makeEl(t), createTextNode: () => makeEl('text'),
  querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  addEventListener(t, fn) { listeners.push({ el: doc, type: t, fn }) }, removeEventListener() {},
}
def('document', doc); def('window', globalThis)
def('location', { pathname: '/workplace', hash: '', href: 'http://dev-admin.test/workplace' })
def('requestAnimationFrame', (fn) => { try { fn() } catch {} return 0 }); def('cancelAnimationFrame', () => {})
def('getComputedStyle', () => ({ getPropertyValue: () => '' })); def('matchMedia', () => ({ matches: false, addEventListener() {}, addListener() {} }))
// node 22 下 navigator/location 是只读全局，必须 defineProperty 覆盖
def('fetch', async (url, init = {}) => {
  const method = (init.method || 'GET').toUpperCase(), body = init.body ? JSON.parse(init.body) : null, u = String(url)
  calls.push({ url: u, method, body })
  const r = routes.find((x) => x.method === method && x.match(u))
  if (!r) throw new Error(`未登记路由 ${method} ${u}`)
  const hit = calls.filter((c) => c.url === u && c.method === method).length
  const out = r.reply({ url: u, body, hit })
  return { ok: out.status < 400, status: out.status, json: async () => out.body, text: async () => JSON.stringify(out.body) }
})

const flush = async (ms = 60) => { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, ms / 6)) }
const findL = (el, t) => listeners.find((l) => l.el === el && l.type === t)
const copies = []
let seq = 0
async function boot(seed, rs) {
  calls = []; routes = rs; listeners = []; TEXT = []; ROOT = null
  def('sessionStorage', mkStore()); def('localStorage', mkStore())
  for (const [k, v] of Object.entries(seed)) sessionStorage.setItem(k, v)
  // ESM 按 URL 缓存模块：每次写一份新副本，保证模块真正重新执行
  const f = join(tmpdir(), `hermes-verify-aicw-${process.pid}-${++seq}.mjs`)
  copies.push(f); writeFileSync(f, src); await import(pathToFileURL(f).href); await flush(); return ROOT
}
const SESS = (uid) => ({ method: 'GET', match: (u) => u.endsWith('/sessions/'), reply: () => ({ status: 200, body: { user_id: uid, conversations: [] } }) })
const HEALTH = { method: 'GET', match: (u) => u.endsWith('/health/'), reply: () => ({ status: 200, body: { model: 'deepseek-v4-flash' } }) }

console.log('A 换账号 000000 -> 000001')
await boot({ [K_CID]: 'stale-000000', [K_UID]: 'admin:1' }, [SESS('admin:2'), HEALTH])
ck(sessionStorage.getItem(K_CID) === null, '旧 cid 被清空', `实际=${sessionStorage.getItem(K_CID)}`)
ck(sessionStorage.getItem(K_UID) === 'admin:2', 'K_UID 更新为当前登录人')

console.log('B 同一账号')
await boot({ [K_CID]: 'keep-me', [K_UID]: 'admin:2' }, [SESS('admin:2'), HEALTH])
ck(sessionStorage.getItem(K_CID) === 'keep-me', 'cid 保留没误杀')

console.log('C 首次访问无 K_UID')
await boot({ [K_CID]: 'keep2' }, [SESS('admin:1'), HEALTH])
ck(sessionStorage.getItem(K_CID) === 'keep2', '无历史身份不误杀')
ck(sessionStorage.getItem(K_UID) === 'admin:1', '写入 K_UID')

console.log('D 发消息 403 自愈重发')
await boot({ [K_CID]: 'stale', [K_UID]: 'admin:1' }, [SESS('admin:1'), HEALTH, {
  method: 'POST', match: (u) => u.endsWith('/chat/'),
  reply: ({ hit }) => (hit === 1 ? { status: 403, body: { detail: 'conversation not owned by user' } } : { status: 200, body: { conversation_id: 'fresh-1', reply: '好的' } }),
}])
{
  const input = ROOT.querySelector('.aicw-input'); input.value = '在吗'
  const kd = findL(input, 'keydown'); ck(!!kd, 'keydown 已绑定')
  if (kd) kd.fn({ key: 'Enter', shiftKey: false, isComposing: false, preventDefault() {}, stopPropagation() {} })
  await flush(120)
  const chat = calls.filter((c) => c.url.endsWith('/chat/'))
  ck(chat.length === 2, '自动重发（共 2 次）', `实际=${chat.length}`)
  ck(chat[0] && chat[0].body.conversation_id === 'stale', '第 1 次带旧 cid', JSON.stringify(chat[0] && chat[0].body))
  ck(chat[1] && chat[1].body.conversation_id === null, '第 2 次丢掉 cid', JSON.stringify(chat[1] && chat[1].body))
  ck(sessionStorage.getItem(K_CID) === 'fresh-1', '新 cid 已落盘')
  ck(!TEXT.some((t) => t.includes('没能拿到回复')), '用户侧无报错气泡')
}

console.log('E 打开历史 403 优雅重置')
await boot({ [K_CID]: 'stale', [K_UID]: 'admin:1' }, [SESS('admin:1'), HEALTH, { method: 'GET', match: (u) => u.endsWith('/sessions/stale/'), reply: () => ({ status: 403, body: { detail: 'x' } }) }])
{
  const c = findL(ROOT, 'click'); ck(!!c, 'click 委托已绑定')
  if (c) c.fn({ target: { closest: (s) => (s === '[data-act]' ? { dataset: { act: 'toggle' } } : null) }, stopPropagation() {} })
  await flush(120)
  ck(sessionStorage.getItem(K_CID) === null, '403 后旧 cid 被清')
  ck(!TEXT.some((t) => t.includes('没能拿到回复')), '没抛成用户可见报错')
}

for (const f of copies) { try { unlinkSync(f) } catch {} }
console.log(`\n[widget_regression] 通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
