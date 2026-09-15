/* ============================================================================
 * 全站常驻 AI 助手悬浮窗
 * ----------------------------------------------------------------------------
 * 挂在 SPA 外壳 templates/views/WorkplaceLayout/index.html 上，因此管理端
 * 每一个页面（路由切换不重新加载文件）都带着它。
 *
 * 后端契约（民意智感中心管理端 /api/ai/ 前缀，由 ai_company 嵌入件提供）：
 *   GET    /api/ai/health/
 *   GET    /api/ai/sessions/                  -> {conversations:[{conversation_id,title,last_active,preview,turn_count}]}
 *   POST   /api/ai/sessions/                  -> {conversation_id,title,...}
 *   GET    /api/ai/sessions/<cid>/            -> {conversation_id,title,turns:[{timestamp,user,assistant}]}
 *   DELETE /api/ai/sessions/<cid>/
 *   POST   /api/ai/chat/   {message,conversation_id?} -> {conversation_id,title,reply}
 *
 * 身份安全：user_id 完全由服务端从 session_key cookie 推导（ai_company/conf.py），
 * 前端不传、也无法指定 user_id。
 * ========================================================================= */

const API = '/api/ai'
const K_CID = 'aicw:cid'
const K_CTX = 'aicw:ctx'
/* 记住「上一个用这个浏览器的人是谁」。
 * sessionStorage 在同一标签页内跨登录是不会自己清掉的：000000 聊完退出、
 * 000001 在同一个标签页登录，旧 conversation_id 会被带过去，服务端归属校验
 * 就会回 403 conversation not owned by user。所以 cid 必须跟用户绑在一起。 */
const K_UID = 'aicw:uid'
const CTX_TEXT_LIMIT = 3000

/* marked 用于渲染 Markdown；拿不到就退化成纯文本 + 换行。 */
let marked = null
try {
  const mod = await import('marked')
  marked = mod.marked || mod.default || null
} catch {
  marked = null
}

/* ── 小工具 ─────────────────────────────────────────────────────────── */

const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  )

/* 先转义再交给 marked：模型输出里的 <script> 只会变成可见文本，不会进 DOM。 */
function mdToHtml(text) {
  const safe = esc(text)
  if (marked) {
    try {
      const html = marked.parse(safe, { breaks: true, gfm: true })
      if (typeof html === 'string') return html
    } catch {
      /* 落到下面的纯文本分支 */
    }
  }
  return safe.replace(/\n/g, '<br>')
}

function fmtTime(value) {
  if (!value) return ''
  const d = new Date(String(value).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return String(value)
  const now = new Date()
  const sameDay = d.toDateString() === now.toDateString()
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  if (sameDay) return `${hh}:${mm}`
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`
}

async function api(path, options = {}) {
  const init = { credentials: 'include', headers: {}, ...options }
  if (init.body !== undefined && typeof init.body !== 'string') {
    init.headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(init.body)
  }
  const res = await fetch(`${API}${path}`, init)
  let payload = {}
  try {
    payload = await res.json()
  } catch {
    payload = {}
  }
  if (!res.ok) {
    const msg =
      payload.detail ||
      payload.error ||
      (res.status === 401
        ? '登录状态已失效，请刷新页面重新登录'
        : `请求失败（HTTP ${res.status}）`)
    const err = new Error(msg)
    err.status = res.status
    throw err
  }
  return payload
}

/* ── 状态 ───────────────────────────────────────────────────────────── */

const state = {
  cid: sessionStorage.getItem(K_CID) || '',
  ctxOn: localStorage.getItem(K_CTX) !== '0',
  lastCtxPath: '',
  turns: [],
  busy: false,
  open: false,
}

/* ── 页面上下文 ─────────────────────────────────────────────────────── */

function captureContext() {
  const node = document.getElementById('app')
  let text = node ? node.innerText || '' : document.body.innerText || ''
  text = text.replace(/\n{3,}/g, '\n\n').trim()
  if (text.length > CTX_TEXT_LIMIT) {
    text = `${text.slice(0, CTX_TEXT_LIMIT)}…（内容过长已截断）`
  }
  return { path: `${location.pathname}${location.hash}`, title: document.title, text }
}

function buildMessage(userText) {
  const ctx = captureContext()
  if (!ctx.text) return { message: userText, path: ctx.path }
  const head = '【当前页面上下文】'
  const body = `页面标题：${ctx.title}\n页面路径：${ctx.path}\n页面可见内容：\n${ctx.text}`
  return {
    message: `${head}\n${body}\n\n【用户问题】\n${userText}`,
    path: ctx.path,
  }
}

/* 该不该给这条消息带上页面上下文？
 *  - 开关关着              -> 不带
 *  - 会话第一句            -> 带（建立场景）
 *  - 页面换了（同会话内）  -> 带（否则模型还停留在旧页面） */
function shouldAttachContext() {
  if (!state.ctxOn) return false
  if (state.turns.length === 0) return true
  return state.lastCtxPath !== `${location.pathname}${location.hash}`
}

/* ── DOM ────────────────────────────────────────────────────────────── */

const root = document.createElement('div')
root.className = 'aicw-root'
root.dataset.open = '0'
root.innerHTML = `
  <div class="aicw-panel" hidden>
    <div class="aicw-head">
      <div class="aicw-avatar"><i class="fa-solid fa-robot"></i></div>
      <div class="aicw-title">
        <b>智感助手</b>
        <span class="aicw-subtitle">基于 ai-company · 可聊页面、可闲聊</span>
      </div>
      <button class="aicw-iconbtn" type="button" data-act="sessions" title="历史会话">
        <i class="fa-solid fa-clock-rotate-left"></i>
      </button>
      <button class="aicw-iconbtn" type="button" data-act="new" title="新建对话">
        <i class="fa-solid fa-plus"></i>
      </button>
      <button class="aicw-iconbtn" type="button" data-act="close" title="收起（Esc）">
        <i class="fa-solid fa-chevron-down"></i>
      </button>
    </div>

    <div class="aicw-body" data-role="body"></div>

    <div class="aicw-foot">
      <div class="aicw-ctxbar" data-act="togglectx" data-on="0" title="点击切换是否把当前页面内容作为上下文发给 AI">
        <i class="fa-solid fa-file-lines"></i>
        <span class="aicw-ctx-text"></span>
        <span class="aicw-switch"></span>
      </div>
      <div class="aicw-inputrow">
        <textarea class="aicw-input" rows="1" placeholder="问点什么，或让我看看当前页面…"></textarea>
        <button class="aicw-send" type="button" data-act="send" title="发送">
          <i class="fa-solid fa-paper-plane"></i>
        </button>
      </div>
      <div class="aicw-hint">Enter 发送 · Shift+Enter 换行</div>
    </div>

    <div class="aicw-drawer" data-open="0">
      <div class="aicw-drawer-head">
        <span>历史会话</span>
        <span>
          <button class="aicw-iconbtn" type="button" data-act="new" title="新建对话">
            <i class="fa-solid fa-plus"></i>
          </button>
          <button class="aicw-iconbtn" type="button" data-act="closedrawer" title="返回">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </span>
      </div>
      <div class="aicw-drawer-list" data-role="sessions"></div>
    </div>
  </div>

  <button class="aicw-fab" type="button" data-act="toggle" title="AI 助手">
    <i class="fa-solid fa-comment-dots"></i>
    <span class="aicw-dot" hidden></span>
  </button>
`

const panel = root.querySelector('.aicw-panel')
const bodyEl = root.querySelector('[data-role="body"]')
const sessionsEl = root.querySelector('[data-role="sessions"]')
const drawerEl = root.querySelector('.aicw-drawer')
const inputEl = root.querySelector('.aicw-input')
const sendBtn = root.querySelector('.aicw-send')
const ctxBar = root.querySelector('.aicw-ctxbar')
const ctxText = root.querySelector('.aicw-ctx-text')
const subtitle = root.querySelector('.aicw-subtitle')
const dotEl = root.querySelector('.aicw-dot')

let socketReady = false

/* ── 渲染 ───────────────────────────────────────────────────────────── */

const SUGGESTIONS = [
  { label: '这个页面是干什么的？', text: '请结合当前页面的内容，用几句话说明这个页面是做什么的、主要有哪些操作。' },
  { label: '帮我总结当前页面', text: '请读一下当前页面的内容，帮我做一个要点总结。' },
  { label: '你能做什么？', text: '你好，介绍一下你自己，你能帮我做哪些事？' },
]

function renderEmpty() {
  bodyEl.innerHTML = `
    <div class="aicw-empty">
      <div class="aicw-empty-icon"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
      <p>你好，我是智感助手</p>
      <small>可以问当前页面的问题，也可以随便聊</small>
      <div class="aicw-chips">
        ${SUGGESTIONS.map(
          (s, i) => `<button class="aicw-chip" type="button" data-chip="${i}">${esc(s.label)}</button>`
        ).join('')}
      </div>
    </div>`
}

function appendMessage(role, text, { error = false } = {}) {
  const el = document.createElement('div')
  el.className = 'aicw-msg'
  el.dataset.role = role
  const icon = role === 'user' ? 'fa-user' : 'fa-robot'
  el.innerHTML = `
    <div class="aicw-msg-avatar"><i class="fa-solid ${icon}"></i></div>
    <div class="aicw-bubble${error ? ' is-error' : ''}"></div>`
  const bubble = el.querySelector('.aicw-bubble')
  if (role === 'assistant' && !error) {
    bubble.innerHTML = mdToHtml(text)
  } else {
    bubble.textContent = text
  }
  bodyEl.appendChild(el)
  scrollToBottom(true)
  return el
}

function appendTyping() {
  const el = document.createElement('div')
  el.className = 'aicw-msg'
  el.dataset.role = 'assistant'
  el.dataset.typing = '1'
  el.innerHTML = `
    <div class="aicw-msg-avatar"><i class="fa-solid fa-robot"></i></div>
    <div class="aicw-bubble"><span class="aicw-typing"><i></i><i></i><i></i></span></div>`
  bodyEl.appendChild(el)
  scrollToBottom(true)
  return el
}

function scrollToBottom(force) {
  const nearBottom =
    bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight < 140
  if (force || nearBottom) bodyEl.scrollTop = bodyEl.scrollHeight
}

function renderTurns() {
  if (!state.turns.length) {
    renderEmpty()
    return
  }
  bodyEl.innerHTML = ''
  for (const t of state.turns) {
    if (t.user) appendMessage('user', t.user)
    if (t.assistant) appendMessage('assistant', t.assistant)
  }
  scrollToBottom(true)
}

function refreshCtxBar() {
  ctxBar.dataset.on = state.ctxOn ? '1' : '0'
  const title = document.title || '当前页面'
  ctxText.textContent = state.ctxOn ? `带上页面：${title}` : '不携带页面上下文'
}

function setBusy(busy) {
  state.busy = busy
  sendBtn.disabled = busy
  inputEl.disabled = busy
}

/* ── 动作 ───────────────────────────────────────────────────────────── */

async function loadSessions() {
  sessionsEl.innerHTML = '<div class="aicw-drawer-empty">加载中…</div>'
  let list = []
  try {
    const data = await api('/sessions/')
    list = Array.isArray(data.conversations) ? data.conversations : []
  } catch (err) {
    sessionsEl.innerHTML = `<div class="aicw-drawer-empty">${esc(err.message)}</div>`
    return
  }
  if (!list.length) {
    sessionsEl.innerHTML = '<div class="aicw-drawer-empty">还没有历史会话</div>'
    return
  }
  sessionsEl.innerHTML = ''
  for (const c of list) {
    const item = document.createElement('div')
    item.className = 'aicw-sess'
    item.dataset.active = c.conversation_id === state.cid ? '1' : '0'
    item.dataset.cid = c.conversation_id
    item.innerHTML = `
      <div class="aicw-sess-main">
        <div class="aicw-sess-title">${esc(c.title || '未命名对话')}</div>
        <div class="aicw-sess-time">${esc(fmtTime(c.last_active))} · ${Number(c.turn_count) || 0} 轮</div>
      </div>
      <button class="aicw-sess-del" type="button" data-del="${esc(c.conversation_id)}" title="删除">
        <i class="fa-solid fa-trash-can"></i>
      </button>`
    sessionsEl.appendChild(item)
  }
}

async function openSession(cid) {
  state.cid = cid
  sessionStorage.setItem(K_CID, cid)
  drawerEl.dataset.open = '0'
  bodyEl.innerHTML = '<div class="aicw-drawer-empty">加载会话…</div>'
  try {
    const data = await api(`/sessions/${encodeURIComponent(cid)}/`)
    state.turns = Array.isArray(data.turns) ? data.turns : []
    state.lastCtxPath = ''
    renderTurns()
  } catch (err) {
    if (err.status === 403) {
      /* 这条会话不属于当前登录的人 —— 多半是上一位用户留在 sessionStorage 里的。 */
      forgetSession()
      state.turns = []
      bodyEl.innerHTML = ''
      renderEmpty()
      return
    }
    bodyEl.innerHTML = ''
    appendMessage('assistant', err.message, { error: true })
  }
}

/* 只丢掉「当前会话」这个指针，不动已经渲染出来的消息。 */
function forgetSession() {
  state.cid = ''
  sessionStorage.removeItem(K_CID)
}

/* 把 cid 跟登录人绑在一起：换人了就把上一位用户的会话指针清掉。
 * 身份只认服务端返回的 user_id（/sessions/ 的响应），前端不做任何推导。 */
async function syncIdentity() {
  let uid = ''
  try {
    const data = await api('/sessions/')
    uid = data && data.user_id ? String(data.user_id) : ''
  } catch {
    return /* 后端没起来 / 未登录：交给探活那条路去提示 */
  }
  if (!uid) return
  const prev = sessionStorage.getItem(K_UID) || ''
  if (prev && prev !== uid) {
    forgetSession()
    state.turns = []
    state.lastCtxPath = ''
    if (typeof renderEmpty === 'function' && !bodyEl.childElementCount) renderEmpty()
  }
  sessionStorage.setItem(K_UID, uid)
}

function newSession() {
  state.cid = ''
  state.turns = []
  state.lastCtxPath = ''
  sessionStorage.removeItem(K_CID)
  drawerEl.dataset.open = '0'
  renderEmpty()
  inputEl.focus()
}

async function deleteSession(cid, ev) {
  ev.stopPropagation()
  try {
    await api(`/sessions/${encodeURIComponent(cid)}/`, { method: 'DELETE' })
  } catch (err) {
    appendMessage('assistant', `删除失败：${err.message}`, { error: true })
    return
  }
  if (cid === state.cid) newSession()
  loadSessions()
}

/* ── SSE 流式 ───────────────────────────────────────────────────────── */

/* 用 fetch + ReadableStream，不用 EventSource：EventSource 只能 GET、塞不了 JSON
   body，也读不到 403 响应体 —— 而「会话归属失效就原地重开」正好依赖后者。
   逐帧解析 `data: {...}\n\n`，半个帧留在 buf 等下一块。 */
async function* sseEvents(reader) {
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let cut
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, cut)
      buf = buf.slice(cut + 2)
      for (const line of frame.split('\n')) {
        if (!line.startsWith('data:')) continue
        const body = line.slice(5).trim()
        if (!body) continue
        try {
          yield JSON.parse(body)
        } catch {
          /* 残缺帧：忽略，等下一块补上（后端不会发半个 JSON，但代理会切包） */
        }
      }
    }
  }
}

async function postStream(path, body) {
  const res = await fetch(`${API}${path}`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok || !res.body) {
    let detail = ''
    try {
      detail = (await res.json()).detail || ''
    } catch {
      /* 非 JSON 错误体 */
    }
    const err = new Error(detail || `请求失败（HTTP ${res.status}）`)
    err.status = res.status
    throw err
  }
  return res.body.getReader()
}

/* 取数过程对用户可见：把「正在统计信件总量…」这类文案写进等待气泡。 */
function setTypingText(el, text) {
  const bubble = el.querySelector('.aicw-bubble')
  if (bubble) {
    bubble.innerHTML =
      `<span class="aicw-status"><i class="fa-solid fa-circle-notch fa-spin"></i>${esc(text)}</span>`
  }
}

function appendFiles(files) {
  if (!files || !files.length) return
  const el = document.createElement('div')
  el.className = 'aicw-msg'
  el.dataset.role = 'assistant'
  el.innerHTML = `
    <div class="aicw-msg-avatar"><i class="fa-solid fa-file-arrow-down"></i></div>
    <div class="aicw-files">${files
      .map(
        (f) => `<a class="aicw-file" href="${esc(f.url)}" target="_blank" rel="noopener">
        <i class="fa-solid fa-file-excel"></i><span>${esc(f.name || '导出文件')}</span>
        <i class="fa-solid fa-download"></i></a>`
      )
      .join('')}</div>`
  bodyEl.appendChild(el)
  scrollToBottom(true)
}

async function send() {
  const raw = inputEl.value.trim()
  if (!raw || state.busy) return

  if (bodyEl.querySelector('.aicw-empty')) bodyEl.innerHTML = ''

  let outgoing = raw
  if (shouldAttachContext()) {
    const built = buildMessage(raw)
    outgoing = built.message
    state.lastCtxPath = built.path
  }

  appendMessage('user', raw)
  inputEl.value = ''
  autoGrow()

  setBusy(true)
  const typing = appendTyping()

  let bubble = null
  let text = ''
  let streamed = false

  /* 第一个 delta 到达前是「等待气泡」，到达后换成真正的回答气泡并逐字长。 */
  const ensureBubble = () => {
    if (bubble) return
    typing.remove()
    bubble = appendMessage('assistant', '').querySelector('.aicw-bubble')
    if (bubble) bubble.classList.add('is-streaming')
  }

  try {
    const start = (message, conversation_id) =>
      postStream('/chat/stream/', { message, conversation_id })

    let reader
    try {
      reader = await start(outgoing, state.cid || null)
    } catch (err) {
      /* 403 + 归属校验失败 = 手里这个 cid 是上一位登录用户留下的。
         丢掉它、原地重开一轮，用户无感，也不用刷新页面。 */
      if (err.status === 403 && state.cid) {
        forgetSession()
        reader = await start(outgoing, null)
      } else {
        throw err
      }
    }

    for await (const ev of sseEvents(reader)) {
      switch (ev.type) {
        case 'meta':
          state.cid = ev.conversation_id || state.cid
          if (state.cid) sessionStorage.setItem(K_CID, state.cid)
          break
        case 'status':
          if (!bubble) setTypingText(typing, ev.text || '正在处理…')
          break
        case 'tool':
          if (!bubble) setTypingText(typing, ev.label || '正在查询…')
          break
        case 'files':
          appendFiles(ev.files)
          break
        case 'delta':
          ensureBubble()
          streamed = true
          text += ev.text || ''
          if (bubble) bubble.innerHTML = mdToHtml(text)
          scrollToBottom(false)
          break
        case 'correction':
          console.warn('成文里的数字查无出处：', ev.unsupported)
          break
        case 'error':
          throw new Error(ev.detail || '服务端出错')
        case 'done':
          if (!text && ev.reply) {
            ensureBubble()
            streamed = true
            text = ev.reply
            if (bubble) bubble.innerHTML = mdToHtml(text)
          }
          break
        default:
          break
      }
    }
  } catch (err) {
    typing.remove()
    const hint =
      err.status === 401
        ? '登录状态已失效，刷新页面重新登录后即可继续。'
        : err.message
    if (bubble && text) {
      /* 已经吐了一半：保留已出的内容，只追加中断提示，别让用户白等 */
      appendMessage('assistant', `（回答中断：${hint}）`, { error: true })
    } else {
      if (bubble) bubble.closest('.aicw-msg')?.remove()
      appendMessage('assistant', `没能拿到回复：${hint}`, { error: true })
    }
    text = ''
    streamed = false
  } finally {
    if (bubble) bubble.classList.remove('is-streaming')
    typing.remove()
    setBusy(false)
    inputEl.focus()
  }

  const reply = String(text || '').trim()
  if (reply) {
    state.turns.push({ user: raw, assistant: reply })
  } else if (!streamed) {
    if (bubble) bubble.closest('.aicw-msg')?.remove()
    appendMessage('assistant', '（本轮没有返回内容）', { error: true })
  }
}

function autoGrow() {
  inputEl.style.height = 'auto'
  inputEl.style.height = `${Math.min(inputEl.scrollHeight, 132)}px`
}

/* ── 开关面板 ───────────────────────────────────────────────────────── */

function openPanel() {
  state.open = true
  root.dataset.state = 'opening'
  panel.hidden = false
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      root.dataset.state = 'open'
    })
  })
  root.dataset.open = '1'
  dotEl.hidden = true
  refreshCtxBar()
  if (!bodyEl.childElementCount) {
    if (state.cid) openSession(state.cid)
    else renderEmpty()
  }
  setTimeout(() => inputEl.focus(), 120)
}

function closePanel() {
  state.open = false
  root.dataset.state = 'closing'
  root.dataset.open = '0'
  drawerEl.dataset.open = '0'
  setTimeout(() => {
    panel.hidden = true
    root.dataset.state = ''
  }, 190)
}

/* ── 事件绑定 ───────────────────────────────────────────────────────── */

root.addEventListener('click', (ev) => {
  const chip = ev.target.closest('[data-chip]')
  if (chip) {
    const item = SUGGESTIONS[Number(chip.dataset.chip)]
    if (item) {
      inputEl.value = item.text
      autoGrow()
      send()
    }
    return
  }

  const del = ev.target.closest('[data-del]')
  if (del) {
    deleteSession(del.dataset.del, ev)
    return
  }

  const sess = ev.target.closest('.aicw-sess')
  if (sess && sess.dataset.cid) {
    openSession(sess.dataset.cid)
    return
  }

  const act = ev.target.closest('[data-act]')
  if (!act) return

  switch (act.dataset.act) {
    case 'toggle':
      state.open ? closePanel() : openPanel()
      break
    case 'close':
      closePanel()
      break
    case 'new':
      newSession()
      break
    case 'sessions':
      drawerEl.dataset.open = '1'
      loadSessions()
      break
    case 'closedrawer':
      drawerEl.dataset.open = '0'
      break
    case 'togglectx':
      state.ctxOn = !state.ctxOn
      localStorage.setItem(K_CTX, state.ctxOn ? '1' : '0')
      refreshCtxBar()
      break
    case 'send':
      send()
      break
    default:
      break
  }
})

inputEl.addEventListener('input', autoGrow)
inputEl.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault()
    send()
  }
})

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && state.open) closePanel()
})

/* 路由切换（SPA 不重新加载文件）后刷新上下文标签 */
setInterval(() => {
  if (!state.open) return
  if (!subtitle.dataset.ready) {
    subtitle.dataset.ready = '1'
  }
  refreshCtxBar()
}, 2000)

/* ── 挂载 ───────────────────────────────────────────────────────────── */

function mount() {
  if (document.querySelector('.aicw-root')) return
  document.body.appendChild(root)
  refreshCtxBar()
  /* 先确认「现在是谁在用」，把上一位用户留下的会话指针清干净。
     不 await：探活与交互不因为它而阻塞。 */
  syncIdentity()
  /* 探活：后端没起来时在头部把状态说清楚，而不是等用户发消息才报错 */
  api('/health/')
    .then((info) => {
      socketReady = true
      subtitle.textContent = `在线 · ${info.model || '已连接'}`
    })
    .catch((err) => {
      socketReady = false
      subtitle.textContent = `后端未就绪（${err.status || '网络'}）`
    })
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mount, { once: true })
} else {
  mount()
}

window.__aicw = { open: openPanel, close: closePanel, newSession, state }
