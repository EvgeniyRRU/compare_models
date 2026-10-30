"use strict";
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const STATUS = { new: "🆕 Новое", in_progress: "🛠 В работе", waiting_info: "⏳ Ожидает информации",
  resolved: "✅ Решено", rejected: "🚫 Отклонено", duplicate: "♻️ Дубль" };
const PRIORITY = { critical: "🔴 critical", high: "🟠 high", medium: "🟡 medium", low: "🟢 low" };
const ROLES = { "": "— нет —", support: "support", developer: "developer", admin: "admin" };

const state = { me: null, tab: null, page: 1, status: "", priority: "", error: "" };
const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "onclick" || k === "onchange") e[k] = v; else if (k === "class") e.className = v; else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
};

class ApiError extends Error { constructor(status, detail) { super(typeof detail === "string" ? detail : detail.message); this.status = status; this.detail = detail; } }

async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: { "Authorization": "tma " + (tg ? tg.initData : ""), "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

function tabsFor(role) {
  const t = [];
  if (role === "support" || role === "admin") t.push({ id: "created", title: "Созданные мной" });
  if (role === "developer" || role === "admin") t.push({ id: "assigned", title: "Назначенные мне" });
  t.push({ id: "all", title: "Все обращения" });
  if (role === "admin") t.push({ id: "roles", title: "Роли" });
  return t;
}

function renderTabs() {
  const nav = $("#tabs"); nav.replaceChildren();
  for (const t of tabsFor(state.me.role)) {
    nav.append(el("button", { class: state.tab === t.id ? "active" : "", onclick: () => { state.tab = t.id; state.page = 1; state.error = ""; show(); } }, t.title));
  }
}

async function show() {
  renderTabs();
  const view = $("#view"); view.replaceChildren();
  try {
    if (state.tab === "roles") await showRoles(view); else await showList(view);
  } catch (e) { view.append(el("div", { class: "err" }, e.message)); }
}

function select(options, value, onchange) {
  const s = el("select", { onchange: (ev) => onchange(ev.target.value) });
  for (const [v, label] of options) { const o = el("option", { value: v }, label); if (v === value) o.selected = true; s.append(o); }
  return s;
}

async function showList(view) {
  const q = new URLSearchParams({ scope: state.tab, page: state.page, page_size: 15 });
  if (state.status) q.set("status", state.status);
  if (state.priority) q.set("priority", state.priority);
  const data = await api("GET", "/tickets?" + q);
  view.append(el("div", { class: "filters" },
    select([["", "Все статусы"], ...Object.entries(STATUS)], state.status, (v) => { state.status = v; state.page = 1; show(); }),
    select([["", "Все приоритеты"], ...Object.entries(PRIORITY)], state.priority, (v) => { state.priority = v; state.page = 1; show(); })));
  if (!data.items.length) view.append(el("div", { class: "muted" }, "Обращений нет"));
  for (const t of data.items) {
    view.append(el("div", { class: "card", onclick: () => openTicket(t.id) },
      el("div", { class: "top" }, el("b", {}, "#" + t.id), el("span", {}, PRIORITY[t.priority])),
      el("div", {}, STATUS[t.status]),
      el("div", { class: "muted" }, t.description.slice(0, 120)),
      el("div", { class: "muted" }, new Date(t.created_at).toLocaleString("ru-RU"))));
  }
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  view.append(el("div", { class: "pager" },
    el("button", { onclick: () => { state.page--; show(); }, ...(state.page <= 1 ? { disabled: "" } : {}) }, "←"),
    el("span", { class: "muted" }, `${state.page} / ${pages}`),
    el("button", { onclick: () => { state.page++; show(); }, ...(state.page >= pages ? { disabled: "" } : {}) }, "→")));
}

async function openTicket(id, notice) {
  const view = $("#view"); view.replaceChildren();
  try {
    const t = await api("GET", "/tickets/" + id);
    const kv = (k, v) => [el("div", {}, k), el("div", {}, v ?? "—")];
    const fmt = (d) => d ? new Date(d).toLocaleString("ru-RU") : null;
    view.append(el("button", { onclick: show }, "← Назад"), el("h3", {}, "#" + t.id),
      notice ? el("div", { class: "muted" }, notice) : null,
      el("div", { class: "kv" },
        ...kv("Статус", STATUS[t.status]), ...kv("Приоритет", PRIORITY[t.priority]),
        ...kv("Заявка", t.request_id), ...kv("Арендатор", t.tenant_id),
        ...kv("Автор", t.author.name), ...kv("Исполнитель", t.assignee && t.assignee.name),
        ...kv("Создано", fmt(t.created_at)), ...kv("Взято", fmt(t.taken_at)), ...kv("Закрыто", fmt(t.closed_at))),
      el("div", { class: "desc" }, t.description));
    if (state.me.role === "admin") await adminActions(view, t);
  } catch (e) { view.append(el("div", { class: "err" }, e.message)); }
}

async function adminActions(view, t) {
  const box = el("div", {}); const err = el("div", { class: "err" });
  const run = async (fn) => {
    err.textContent = "";
    try { await fn(); } catch (e) { if (e.status === 409) openTicket(t.id, "Обращение изменилось, данные обновлены"); else err.textContent = e.message; }
  };
  if (["resolved", "rejected", "duplicate"].includes(t.status)) {
    box.append(el("div", { class: "row" }, el("button", { class: "primary", onclick: () => run(async () => { await api("POST", `/tickets/${t.id}/reopen`, { version: t.version }); openTicket(t.id, "Переоткрыто"); }) }, "Переоткрыть")));
  }
  if (["in_progress", "waiting_info"].includes(t.status)) {
    const devs = await api("GET", "/users?assignable=true");
    const sel = select(devs.filter((u) => !t.assignee || u.id !== t.assignee.id).map((u) => [u.id, u.name + " (" + u.role + ")"]), "", () => {});
    box.append(el("div", { class: "row" }, sel, el("button", { class: "primary", onclick: () => run(async () => {
      if (!sel.value) return;
      await api("POST", `/tickets/${t.id}/reassign`, { version: t.version, assignee_id: Number(sel.value) }); openTicket(t.id, "Переназначено"); }) }, "Переназначить")));
  }
  box.append(el("div", { class: "row" }, el("button", { onclick: () => run(async () => { await api("POST", `/tickets/${t.id}/restore-post`); openTicket(t.id, "Пост опубликован заново"); }) }, "Восстановить пост")));
  view.append(box, err);
}

async function showRoles(view) {
  const users = await api("GET", "/users");
  const err = el("div", { class: "err" });
  for (const u of users) {
    const sel = select(Object.entries(ROLES), u.role || "", async (v) => {
      err.textContent = "";
      try { await api("PUT", `/users/${u.id}/role`, { role: v || null }); }
      catch (e) {
        err.textContent = e.message;
        if (e.detail && e.detail.ticket_ids) err.textContent += " (" + e.detail.ticket_ids.map((i) => "#" + i).join(", ") + ")";
        show();
      }
    });
    view.append(el("div", { class: "card" }, el("div", { class: "top" }, el("b", {}, u.name), sel), el("div", { class: "muted" }, "id " + u.telegram_id)));
  }
  view.append(err);
}

(async function init() {
  try {
    state.me = await api("GET", "/me");
    state.tab = tabsFor(state.me.role)[0].id;
    await show();
  } catch (e) {
    $("#view").append(el("div", { class: "err" }, e.status === 403 ? "Нет доступа. Обратитесь к администратору." : e.message));
  }
})();
