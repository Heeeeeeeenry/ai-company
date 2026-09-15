/* ai-company 前端组件 —— 原生 JS，无构建、无框架。
 *
 * 安全约定：本文件**从不**发送 user_id。身份完全由 Django 视图从登录态推导，
 * 前端越权改不了别人的会话。
 */
(function () {
  "use strict";
  if (window.__aiCompanyLoaded) return;
  window.__aiCompanyLoaded = true;

  function csrf() {
    var m = document.cookie.match(/(^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[2]) : "";
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* 极简格式化：代码块 / 行内代码 / 保留换行（其余一律转义，不解析 HTML） */
  function fmt(text) {
    return esc(text).split("```").map(function (part, i) {
      if (i % 2) return "<pre><code>" + part.replace(/^\n/, "") + "</code></pre>";
      return part.replace(/`([^`]+)`/g, "<code>$1</code>");
    }).join("");
  }

  function api(root, path, opts) {
    opts = opts || {};
    var headers = { Accept: "application/json" };
    if (opts.body) headers["Content-Type"] = "application/json";
    if (opts.method && opts.method !== "GET") headers["X-CSRFToken"] = csrf();
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      credentials: "same-origin",
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var e = new Error(data.detail || ("HTTP " + resp.status));
          e.status = resp.status;
          throw e;
        }
        return data;
      });
    });
  }

  function Widget(root) {
    this.root = root;
    this.currentId = null;
    this.busy = false;
    this.el = {
      list: root.querySelector('[data-role="list"]'),
      msgs: root.querySelector('[data-role="msgs"]'),
      input: root.querySelector('[data-role="input"]'),
      title: root.querySelector('[data-role="title"]'),
      status: root.querySelector('[data-role="status"]'),
      dot: root.querySelector('[data-role="dot"]'),
      send: root.querySelector('[data-act="send"]'),
    };
    this.urls = {
      sessions: root.dataset.apiSessions,
      chat: root.dataset.apiChat,
      detail: root.dataset.apiSessionDetail,
    };
    this.bind();
    this.loadSessions();
    this.ping();
  }

  Widget.prototype.detailUrl = function (cid) {
    return this.urls.detail.replace("__CID__", encodeURIComponent(cid));
  };

  Widget.prototype.bind = function () {
    var self = this;
    this.root.querySelector('[data-act="new"]').addEventListener("click", function () {
      self.createSession();
    });
    this.el.send.addEventListener("click", function () { self.send(); });
    this.el.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self.send(); }
    });
    this.el.input.addEventListener("input", function () {
      self.el.input.style.height = "auto";
      self.el.input.style.height = Math.min(self.el.input.scrollHeight, 160) + "px";
    });
  };

  Widget.prototype.setStatus = function (text, cls) {
    this.el.status.textContent = text || "";
    this.el.dot.className = "ac-dot" + (cls ? " is-" + cls : "");
  };

  Widget.prototype.ping = function () {
    var self = this;
    var sessions = this.urls.sessions;
    var health = sessions.replace(/sessions\/?$/, "health/");
    api(this.root, health).then(function (info) {
      self.setStatus("已连接 · " + (info.model || "unknown"), "on");
    }).catch(function () {
      self.setStatus("后端不可用", "off");
    });
  };

  Widget.prototype.loadSessions = function () {
    var self = this;
    return api(this.root, this.urls.sessions).then(function (data) {
      var items = data.conversations || [];
      if (!items.length) {
        self.el.list.innerHTML = '<div class="ac-empty-hint">还没有会话，点「+ 新对话」开始</div>';
        return;
      }
      self.el.list.innerHTML = items.map(function (c) {
        var active = c.conversation_id === self.currentId ? " is-active" : "";
        var when = (c.last_active || "").replace("T", " ").slice(5, 16);
        return '<div class="ac-item' + active + '" data-cid="' + esc(c.conversation_id) + '">' +
          '<div class="ac-item-title">' + esc(c.title || "对话") + "</div>" +
          '<div class="ac-item-meta">' + esc(when) + " · " + (c.turn_count || 0) + " 轮</div>" +
          '<button type="button" class="ac-del" data-del="' + esc(c.conversation_id) + '" title="删除">×</button>' +
          "</div>";
      }).join("");
    }).catch(function (e) {
      self.el.list.innerHTML = '<div class="ac-empty-hint">加载失败：' + esc(e.message) + "</div>";
    });
  };

  Widget.prototype.renderEmpty = function () {
    this.el.msgs.innerHTML =
      '<div class="ac-empty"><b>开始一段新对话</b>' +
      "<span>左侧可创建多个互不干扰的会话</span></div>";
  };

  Widget.prototype.renderTurns = function (turns) {
    if (!turns || !turns.length) { this.renderEmpty(); return; }
    var html = "";
    turns.forEach(function (t) {
      if (t.user) html += '<div class="ac-msg user"><div class="ac-msg-role">我</div>' +
        '<div class="ac-msg-body">' + fmt(t.user) + "</div></div>";
      if (t.assistant) html += '<div class="ac-msg ai"><div class="ac-msg-role">AI 助手</div>' +
        '<div class="ac-msg-body">' + fmt(t.assistant) + "</div></div>";
    });
    this.el.msgs.innerHTML = html;
    this.scrollBottom();
  };

  Widget.prototype.scrollBottom = function () {
    this.el.msgs.scrollTop = this.el.msgs.scrollHeight;
  };

  Widget.prototype.open = function (cid) {
    var self = this;
    this.currentId = cid;
    this.el.msgs.innerHTML =
      '<div class="ac-empty"><span>载入中…</span></div>';
    return api(this.root, this.detailUrl(cid)).then(function (data) {
      self.el.title.textContent = data.title || "对话";
      self.renderTurns(data.turns);
      self.loadSessions();
    }).catch(function (e) {
      self.el.msgs.innerHTML = '<div class="ac-empty"><span class="ac-err">' +
        esc(e.message) + "</span></div>";
    });
  };

  Widget.prototype.createSession = function (title) {
    var self = this;
    return api(this.root, this.urls.sessions, {
      method: "POST", body: { title: title || "新对话" },
    }).then(function (created) {
      self.currentId = created.conversation_id;
      self.el.title.textContent = created.title || "新对话";
      self.renderEmpty();
      return self.loadSessions();
    });
  };

  Widget.prototype.remove = function (cid) {
    var self = this;
    if (!window.confirm("删除这个会话？聊天记录将一并清除。")) return;
    api(this.root, this.detailUrl(cid), { method: "DELETE" }).then(function () {
      if (self.currentId === cid) { self.currentId = null; self.renderEmpty(); }
      self.loadSessions();
    }).catch(function (e) { window.alert("删除失败：" + e.message); });
  };

  Widget.prototype.send = function () {
    var self = this;
    var text = this.el.input.value.trim();
    if (!text || this.busy) return;
    this.busy = true;
    this.el.send.disabled = true;
    this.el.input.value = "";
    this.el.input.style.height = "auto";

    if (!this.el.msgs.querySelector(".ac-msg")) this.el.msgs.innerHTML = "";
    this.el.msgs.insertAdjacentHTML("beforeend",
      '<div class="ac-msg user"><div class="ac-msg-role">我</div>' +
      '<div class="ac-msg-body">' + fmt(text) + "</div></div>");
    this.el.msgs.insertAdjacentHTML("beforeend",
      '<div class="ac-msg ai" data-role="pending"><div class="ac-msg-role">AI 助手</div>' +
      '<div class="ac-msg-body"><span class="ac-typing"><i></i><i></i><i></i></span></div></div>');
    this.scrollBottom();

    api(this.root, this.urls.chat, {
      method: "POST",
      body: { message: text, conversation_id: this.currentId || null },
    }).then(function (data) {
      var pending = self.el.msgs.querySelector('[data-role="pending"]');
      if (pending) pending.querySelector(".ac-msg-body").innerHTML = fmt(data.reply || "(空回复)");
      if (!self.currentId) { self.currentId = data.conversation_id; }
      self.el.title.textContent = data.title || "对话";
      self.loadSessions();
    }).catch(function (e) {
      var pending = self.el.msgs.querySelector('[data-role="pending"]');
      if (pending) pending.querySelector(".ac-msg-body").innerHTML =
        '<span class="ac-err">出错：' + esc(e.message) + "</span>";
    }).then(function () {
      self.busy = false;
      self.el.send.disabled = false;
      self.scrollBottom();
      self.el.input.focus();
    });
  };

  Widget.prototype.handleClick = function (e) {
    var del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); this.remove(del.dataset.del); return; }
    var item = e.target.closest("[data-cid]");
    if (item) this.open(item.dataset.cid);
  };

  function init() {
    document.querySelectorAll("[data-ai-company]").forEach(function (root) {
      var w = new Widget(root);
      root.addEventListener("click", function (e) { w.handleClick(e); });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
