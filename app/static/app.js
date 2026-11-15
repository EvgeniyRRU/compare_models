/* Rent Support Bot — Telegram Mini App frontend (Vanilla JS, no build step).
 *
 * Single-page app that talks to the FastAPI backend under /api. Authentication
 * is implicit: every request carries the Telegram WebApp initData in the
 * `X-Telegram-Init-Data` header; the backend resolves the current user and role.
 */

"use strict";

/* ------------------------------------------------------------------ *
 * Static data (mirrors app/services/render.py labels exactly)
 * ------------------------------------------------------------------ */

const STATUS_LABELS = {
  new: "Новое",
  in_progress: "В работе",
  waiting_info: "Ожидает информации",
  resolved: "Решено",
  rejected: "Отклонено",
  duplicate: "Дубль",
};

const PRIORITY_LABELS = {
  critical: "Критичный",
  high: "Высокий",
  medium: "Средний",
  low: "Низкий",
};

const ROLE_LABELS = {
  support: "Поддержка",
  developer: "Разработчик",
  admin: "Администратор",
};

const STATUS_OPTIONS = [
  ["", "Все"],
  ["new", "Новое"],
  ["in_progress", "В работе"],
  ["waiting_info", "Ожидает информации"],
  ["resolved", "Решено"],
  ["rejected", "Отклонено"],
  ["duplicate", "Дубль"],
];

const PRIORITY_OPTIONS = [
  ["", "Все"],
  ["critical", "Критичный"],
  ["high", "Высокий"],
  ["medium", "Средний"],
  ["low", "Низкий"],
];

const ROLE_OPTIONS = [
  ["", "Без роли"],
  ["support", "Поддержка"],
  ["developer", "Разработчик"],
  ["admin", "Администратор"],
];

/* ------------------------------------------------------------------ *
 * State
 * ------------------------------------------------------------------ */

const state = {
  role: null, // "admin" | "support" | "developer" | null
  view: "mine", // "mine" | "all" | "users"
  mineScope: "authored", // admin only: "authored" | "assigned"
  status: "", // "" = all
  priority: "", // "" = all
  page: 1,
  pageSize: 20,
  total: 0,
};

/* ------------------------------------------------------------------ *
 * Helpers
 * ------------------------------------------------------------------ */

function tg() {
  return window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
}

function truncate(text, limit) {
  const s = String(text == null ? "" : text);
  if (s.length <= limit) return s;
  return s.slice(0, limit - 1) + "…";
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("ru-RU");
}

/* ------------------------------------------------------------------ *
 * HTTP wrapper
 * ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const app = tg();
  if (app && app.initData) {
    headers.set("X-Telegram-Init-Data", app.initData);
  }
  const res = await fetch(path, { ...options, headers });
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    /* non-JSON response body */
  }
  if (!res.ok) {
    const err = new Error(data && data.detail ? data.detail : "HTTP " + res.status);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function jsonBody(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function errorMessage(err) {
  if (err && (err.status === 401 || err.status === 403)) return "Доступ запрещён";
  if (err && err.status === 404) return "Не найдено";
  if (err && err.message) return err.message;
  return "Ошибка";
}

/* ------------------------------------------------------------------ *
 * DOM builders
 * ------------------------------------------------------------------ */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function badge(text, className) {
  return el("span", "badge " + className, text);
}

function message(text) {
  return el("div", "message", text);
}

function loading() {
  return el("div", "message", "Загрузка…");
}

function field(label, value) {
  const wrap = el("div", "field");
  wrap.appendChild(el("div", "field-label", label));
  wrap.appendChild(el("div", "field-value", value));
  return wrap;
}

function selectBox(label, options, value, onChange) {
  const wrap = el("label", "select-wrap");
  wrap.appendChild(el("span", "select-label", label));
  const sel = document.createElement("select");
  sel.className = "filter-select";
  for (const [val, lbl] of options) {
    const opt = el("option", null, lbl);
    opt.value = val;
    if (value === val) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => onChange(sel.value));
  wrap.appendChild(sel);
  return wrap;
}

function actionButton(label, onClick) {
  const btn = el("button", "action-btn", label);
  btn.addEventListener("click", onClick);
  return btn;
}

let toastTimer = null;
function showToast(text) {
  let toast = document.getElementById("toast");
  if (!toast) {
    toast = el("div", "toast");
    toast.id = "toast";
    document.body.appendChild(toast);
  }
  toast.textContent = text;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3000);
}

/* ------------------------------------------------------------------ *
 * Theme
 * ------------------------------------------------------------------ */

function applyTheme() {
  const app = tg();
  const scheme = app && app.colorScheme ? app.colorScheme : "light";
  const dark = scheme === "dark";
  const tp = (app && app.themeParams) || {};
  const get = (key, fallback) => (tp[key] ? tp[key] : fallback);

  const root = document.documentElement;
  root.style.setProperty("--bg-color", get("bg_color", dark ? "#18222d" : "#ffffff"));
  root.style.setProperty("--text-color", get("text_color", dark ? "#f5f5f5" : "#000000"));
  root.style.setProperty("--hint-color", get("hint_color", dark ? "#8a9aa8" : "#8e8e93"));
  root.style.setProperty("--accent", get("button_color", dark ? "#5eb0e6" : "#2ea6ff"));
  root.style.setProperty("--accent-text", get("button_text_color", "#ffffff"));
  root.style.setProperty(
    "--card-bg",
    get("secondary_bg_color", dark ? "#232e3c" : "#f2f3f5"),
  );
  root.style.setProperty("--divider", dark ? "#2b3644" : "#e5e5ea");
}

/* ------------------------------------------------------------------ *
 * Boot + auth
 * ------------------------------------------------------------------ */

async function init() {
  const app = tg();
  if (app) {
    app.ready();
    app.expand();
    app.onEvent("themeChanged", applyTheme);
  }
  applyTheme();
  await loadMe();
}

async function loadMe() {
  try {
    const me = await api("/api/me");
    state.role = me.role;
    renderApp();
  } catch (err) {
    renderFatal(err);
  }
}

function renderFatal(err) {
  document.getElementById("tabbar").innerHTML = "";
  document.getElementById("filters").innerHTML = "";
  const content = document.getElementById("content");
  content.innerHTML = "";
  content.appendChild(message(errorMessage(err)));
}

/* ------------------------------------------------------------------ *
 * Top-level rendering
 * ------------------------------------------------------------------ */

function renderApp() {
  const content = document.getElementById("content");
  if (state.role === null) {
    document.getElementById("tabbar").innerHTML = "";
    document.getElementById("filters").innerHTML = "";
    content.innerHTML = "";
    content.appendChild(message("Обратитесь к администратору для доступа к боту"));
    return;
  }
  renderTabbar();
  renderFilters();
  route();
}

function renderTabbar() {
  const bar = document.getElementById("tabbar");
  bar.innerHTML = "";
  const tabs = [{ view: "mine", label: "Мои обращения" }];
  if (state.role === "admin") {
    tabs.push({ view: "all", label: "Все обращения" });
    tabs.push({ view: "users", label: "Пользователи" });
  }
  for (const t of tabs) {
    const btn = el("button", "tab" + (state.view === t.view ? " active" : ""), t.label);
    btn.dataset.view = t.view;
    btn.addEventListener("click", () => {
      state.view = t.view;
      state.page = 1;
      renderApp();
    });
    bar.appendChild(btn);
  }
}

function renderFilters() {
  const box = document.getElementById("filters");
  box.innerHTML = "";
  if (state.view === "users") return;

  if (state.view === "mine" && state.role === "admin") {
    const sub = el("div", "subtabs");
    for (const [scope, label] of [
      ["authored", "Созданные мной"],
      ["assigned", "Назначенные мне"],
    ]) {
      const btn = el("button", "subtab" + (state.mineScope === scope ? " active" : ""), label);
      btn.addEventListener("click", () => {
        state.mineScope = scope;
        state.page = 1;
        renderApp();
      });
      sub.appendChild(btn);
    }
    box.appendChild(sub);
  }

  const row = el("div", "filter-row");
  row.appendChild(
    selectBox("Статус", STATUS_OPTIONS, state.status, (v) => {
      state.status = v;
      state.page = 1;
      route();
    }),
  );
  row.appendChild(
    selectBox("Приоритет", PRIORITY_OPTIONS, state.priority, (v) => {
      state.priority = v;
      state.page = 1;
      route();
    }),
  );
  box.appendChild(row);
}

function route() {
  if (state.view === "users") {
    renderUsers();
  } else {
    renderTicketList();
  }
}

function currentScope() {
  if (state.view === "all") return "all";
  if (state.role === "support") return "authored";
  if (state.role === "developer") return "assigned";
  return state.mineScope || "authored";
}

/* ------------------------------------------------------------------ *
 * Ticket list
 * ------------------------------------------------------------------ */

async function renderTicketList() {
  const content = document.getElementById("content");
  content.innerHTML = "";
  content.appendChild(loading());

  const params = new URLSearchParams();
  params.set("scope", currentScope());
  if (state.status) params.set("status", state.status);
  if (state.priority) params.set("priority", state.priority);
  params.set("page", String(state.page));
  params.set("page_size", String(state.pageSize));

  try {
    const data = await api("/api/tickets?" + params.toString());
    state.total = data.total;
    content.innerHTML = "";
    if (!data.items.length) {
      content.appendChild(message("Обращений нет"));
    } else {
      const list = el("div", "ticket-list");
      for (const t of data.items) list.appendChild(ticketRow(t));
      content.appendChild(list);
    }
    content.appendChild(pagination(data.total, data.page, data.page_size));
  } catch (err) {
    content.innerHTML = "";
    content.appendChild(message(errorMessage(err)));
  }
}

function ticketRow(t) {
  const row = el("button", "ticket-row");

  const head = el("div", "row-head");
  head.appendChild(badge("#" + t.id, "id"));
  head.appendChild(badge(PRIORITY_LABELS[t.priority] || t.priority, "priority-" + t.priority));
  head.appendChild(badge(STATUS_LABELS[t.status] || t.status, "status-" + t.status));
  row.appendChild(head);

  row.appendChild(el("div", "row-desc", truncate(t.description, 120)));

  const meta = el(
    "div",
    "row-meta",
    "Автор: " +
      (t.author_name || "—") +
      (t.assignee_name ? " · Исполнитель: " + t.assignee_name : ""),
  );
  row.appendChild(meta);

  row.addEventListener("click", () => openTicket(t.id));
  return row;
}

function pagination(total, page, pageSize) {
  const wrap = el("div", "pagination");
  const pages = Math.max(1, Math.ceil(total / pageSize));

  const prev = el("button", "page-btn", "‹ Назад");
  prev.disabled = page <= 1;
  prev.addEventListener("click", () => {
    state.page = page - 1;
    renderTicketList();
  });

  const info = el("span", "page-info", "Стр. " + page + " из " + pages);

  const next = el("button", "page-btn", "Вперёд ›");
  next.disabled = page >= pages;
  next.addEventListener("click", () => {
    state.page = page + 1;
    renderTicketList();
  });

  wrap.appendChild(prev);
  wrap.appendChild(info);
  wrap.appendChild(next);
  return wrap;
}

/* ------------------------------------------------------------------ *
 * Ticket card (overlay)
 * ------------------------------------------------------------------ */

async function openTicket(id) {
  try {
    const t = await api("/api/tickets/" + id);
    renderCard(t);
  } catch (err) {
    showToast(errorMessage(err));
  }
}

function closeCard() {
  document.getElementById("modal-root").innerHTML = "";
}

function renderCard(t) {
  const modal = document.getElementById("modal-root");
  modal.innerHTML = "";

  const overlay = el("div", "overlay");
  const card = el("div", "card");

  const head = el("div", "card-head");
  const title = el("div", "card-title");
  title.appendChild(badge("#" + t.id, "id"));
  title.appendChild(badge(PRIORITY_LABELS[t.priority] || t.priority, "priority-" + t.priority));
  title.appendChild(badge(STATUS_LABELS[t.status] || t.status, "status-" + t.status));
  const close = el("button", "close-btn", "✕");
  close.addEventListener("click", closeCard);
  head.appendChild(title);
  head.appendChild(close);
  card.appendChild(head);

  const body = el("div", "card-body");
  body.appendChild(field("Автор", t.author_name || "—"));
  body.appendChild(field("Исполнитель", t.assignee_name || "—"));
  body.appendChild(field("Заявка", t.request_id));
  body.appendChild(field("Арендатор", t.tenant_id || "—"));
  body.appendChild(field("Версия", String(t.version)));
  body.appendChild(field("Взято", fmtDate(t.taken_at)));
  body.appendChild(field("Закрыто", fmtDate(t.closed_at)));
  body.appendChild(field("Создано", fmtDate(t.created_at)));
  body.appendChild(field("Обновлено", fmtDate(t.updated_at)));

  body.appendChild(el("div", "field-label", "Описание"));
  body.appendChild(el("div", "field-value description", t.description));

  body.appendChild(el("p", "note", "Статус меняется через бота"));
  card.appendChild(body);

  if (state.role === "admin") {
    adminActions(t).then((actions) => card.appendChild(actions));
  }

  overlay.appendChild(card);
  modal.appendChild(overlay);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeCard();
  });
}

async function performTicketAction(fn) {
  try {
    const updated = await fn();
    showToast("Обращение обновлено");
    renderTicketList();
    if (updated && updated.id) {
      renderCard(updated);
    }
  } catch (err) {
    showToast(errorMessage(err));
  }
}

async function adminActions(t) {
  const wrap = el("div", "actions");

  if (["resolved", "rejected", "duplicate"].includes(t.status)) {
    wrap.appendChild(
      actionButton("Переоткрыть", () =>
        performTicketAction(() =>
          api("/api/tickets/" + t.id + "/reopen", jsonBody("POST", { version: t.version })),
        ),
      ),
    );
  }

  if (["in_progress", "waiting_info"].includes(t.status)) {
    wrap.appendChild(await reassignControls(t));
  }

  wrap.appendChild(
    actionButton("Восстановить пост", () =>
      performTicketAction(() =>
        api("/api/tickets/" + t.id + "/restore-post", jsonBody("POST", {})),
      ),
    ),
  );

  return wrap;
}

async function reassignControls(t) {
  const wrap = el("div", "reassign");

  let assignable;
  try {
    const users = await api("/api/users");
    assignable = users.filter((u) => u.role === "developer" || u.role === "admin");
  } catch (err) {
    wrap.appendChild(el("div", "error-text", errorMessage(err)));
    return wrap;
  }

  const sel = document.createElement("select");
  sel.className = "assignee-select";
  for (const u of assignable) {
    const opt = el("option", null, u.full_name || u.username || "#" + u.telegram_id);
    opt.value = String(u.id);
    if (t.assignee_id === u.id) opt.selected = true;
    sel.appendChild(opt);
  }
  wrap.appendChild(sel);

  wrap.appendChild(
    actionButton("Назначить", () =>
      performTicketAction(() =>
        api(
          "/api/tickets/" + t.id + "/reassign",
          jsonBody("POST", { version: t.version, assignee_id: Number(sel.value) }),
        ),
      ),
    ),
  );

  return wrap;
}

/* ------------------------------------------------------------------ *
 * Users view (role management, admin only)
 * ------------------------------------------------------------------ */

async function renderUsers() {
  const content = document.getElementById("content");
  content.innerHTML = "";
  content.appendChild(loading());
  try {
    const users = await api("/api/users");
    content.innerHTML = "";
    const list = el("div", "user-list");
    for (const u of users) list.appendChild(userRow(u));
    content.appendChild(list);
  } catch (err) {
    content.innerHTML = "";
    content.appendChild(message(errorMessage(err)));
  }
}

function userRow(u) {
  const row = el("div", "user-row");

  const info = el("div", "user-info");
  info.appendChild(el("div", "user-name", u.full_name || u.username || "#" + u.telegram_id));
  info.appendChild(
    el("div", "user-tg", "tg: " + u.telegram_id + (u.username ? " (@" + u.username + ")" : "")),
  );
  row.appendChild(info);

  const sel = document.createElement("select");
  sel.className = "role-select";
  for (const [val, label] of ROLE_OPTIONS) {
    const opt = el("option", null, label);
    opt.value = val;
    if ((u.role || "") === val) opt.selected = true;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => changeRole(u, sel.value));
  row.appendChild(sel);

  return row;
}

async function changeRole(u, roleValue) {
  const role = roleValue === "" ? null : roleValue;
  try {
    await api("/api/users/" + u.id + "/role", jsonBody("PUT", { role }));
    showToast("Роль обновлена");
    renderUsers();
  } catch (err) {
    let msg = errorMessage(err);
    if (err.status === 409 && err.data && err.data.active_ticket_ids && err.data.active_ticket_ids.length) {
      msg = "Нельзя снять роль: активные обращения #" + err.data.active_ticket_ids.join(", #");
    }
    showToast(msg);
    renderUsers();
  }
}

/* ------------------------------------------------------------------ */

document.addEventListener("DOMContentLoaded", init);
