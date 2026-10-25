"use strict";

// Telegram Mini App: списки обращений, карточка, действия admin, управление ролями.
// Без сборки и фреймворков. DOM строится через h(), текст — только через textContent.

const tg = window.Telegram && window.Telegram.WebApp;
if (tg && tg.initData) {
  document.documentElement.classList.add("tg");
  tg.ready();
  tg.expand();
}

const STATUS = {
  new: "🆕 Новое",
  in_progress: "🛠 В работе",
  waiting_info: "⏳ Ожидает информации",
  resolved: "✅ Решено",
  rejected: "🚫 Отклонено",
  duplicate: "♻️ Дубль",
};
const PRIORITY = {
  critical: "🔴 Критический",
  high: "🟠 Высокий",
  medium: "🟡 Средний",
  low: "🟢 Низкий",
};
const ROLES = { "": "Нет роли", support: "Поддержка", developer: "Разработчик", admin: "Администратор" };
const PAGE_SIZE = 20;

const state = {
  me: null,
  tab: "mine", // mine | all | users
  scope: null, // created | assigned (для «Мои»)
  filters: { status: "", priority: "" },
  page: 1,
  ticketId: null, // открытая карточка
};

// --- утилиты ---------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "value") el.value = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function fmtDate(value) {
  return value ? new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";
}

function userName(u) {
  return u ? u.display_name : "—";
}

class ApiError extends Error {
  constructor(status, body) {
    super((body && body.detail) || `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `tma ${tg ? tg.initData : ""}`,
      ...(options.headers || {}),
    },
  });
  let body = null;
  try {
    body = await response.json();
  } catch (_) {
    body = null;
  }
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : null;
    throw new ApiError(response.status, { ...(body || {}), detail });
  }
  return body;
}

function alertMsg(text) {
  return new Promise((resolve) => (tg && tg.initData ? tg.showAlert(text, resolve) : (window.alert(text), resolve())));
}

function confirmMsg(text) {
  return new Promise((resolve) => (tg && tg.initData ? tg.showConfirm(text, resolve) : resolve(window.confirm(text))));
}

function root() {
  return document.getElementById("app");
}

function showError(err) {
  root().replaceChildren(h("p", { class: "error" }, err.message || String(err)));
}

// --- навигация -------------------------------------------------------------------------

function availableTabs() {
  const tabs = [];
  if (state.me.can_create || state.me.can_be_assignee) tabs.push(["mine", "Мои обращения"]);
  tabs.push(["all", "Все обращения"]);
  if (state.me.is_admin) tabs.push(["users", "Пользователи"]);
  return tabs;
}

function mineScopes() {
  const scopes = [];
  if (state.me.can_create) scopes.push(["created", "Созданные мной"]);
  if (state.me.can_be_assignee) scopes.push(["assigned", "Назначенные мне"]);
  return scopes;
}

function renderTabs() {
  const nav = document.getElementById("tabs");
  nav.replaceChildren(
    ...availableTabs().map(([key, label]) =>
      h("button", {
        class: `tab${state.tab === key && !state.ticketId ? " active" : ""}`,
        role: "tab",
        onclick: () => {
          state.tab = key;
          state.ticketId = null;
          state.page = 1;
          render();
        },
      }, label),
    ),
  );
}

let backHandler = null;

function setBackButton(handler) {
  if (!tg || !tg.BackButton) return;
  if (backHandler) tg.BackButton.offClick(backHandler);
  backHandler = handler;
  if (handler) {
    tg.BackButton.onClick(handler);
    tg.BackButton.show();
  } else {
    tg.BackButton.hide();
  }
}

async function render() {
  renderTabs();
  try {
    if (state.ticketId) await renderTicket(state.ticketId);
    else if (state.tab === "users") await renderUsers();
    else await renderList();
  } catch (err) {
    showError(err);
  }
}

// --- список ----------------------------------------------------------------------------

function filtersBar() {
  const statusSelect = h(
    "select",
    { "aria-label": "Статус", onchange: (e) => { state.filters.status = e.target.value; state.page = 1; render(); } },
    h("option", { value: "" }, "Все статусы"),
    Object.entries(STATUS).map(([k, v]) => h("option", { value: k, selected: state.filters.status === k }, v)),
  );
  const prioritySelect = h(
    "select",
    { "aria-label": "Приоритет", onchange: (e) => { state.filters.priority = e.target.value; state.page = 1; render(); } },
    h("option", { value: "" }, "Все приоритеты"),
    Object.entries(PRIORITY).map(([k, v]) => h("option", { value: k, selected: state.filters.priority === k }, v)),
  );
  return h("div", { class: "filters" }, statusSelect, prioritySelect);
}

function ticketCard(t) {
  return h(
    "button",
    { class: "card", onclick: () => { state.ticketId = t.id; render(); } },
    h("div", { class: "card-head" },
      h("span", { class: "card-title" }, `#${t.id}`),
      h("span", { class: "badge" }, STATUS[t.status])),
    h("div", { class: "card-text" }, t.description),
    h("div", { class: "meta" }, `${PRIORITY[t.priority]} · ${fmtDate(t.created_at)} · исп.: ${userName(t.assignee)}`),
  );
}

async function renderList() {
  setBackButton(null);
  const parts = [];
  let scope = "all";
  if (state.tab === "mine") {
    const scopes = mineScopes();
    if (!scopes.some(([k]) => k === state.scope)) state.scope = scopes[0][0];
    scope = state.scope;
    if (scopes.length > 1) {
      parts.push(h("div", { class: "subtabs" }, scopes.map(([key, label]) =>
        h("button", {
          class: `tab${state.scope === key ? " active" : ""}`,
          onclick: () => { state.scope = key; state.page = 1; render(); },
        }, label))));
    }
  }
  parts.push(filtersBar());

  const params = new URLSearchParams({ scope, page: state.page, page_size: PAGE_SIZE });
  if (state.filters.status) params.append("status", state.filters.status);
  if (state.filters.priority) params.append("priority", state.filters.priority);
  const data = await api(`/api/tickets?${params}`);

  if (!data.items.length) parts.push(h("p", { class: "hint" }, "Обращений нет."));
  parts.push(...data.items.map(ticketCard));

  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  if (pages > 1) {
    parts.push(h("div", { class: "pager" },
      h("button", { class: "btn", disabled: state.page <= 1, onclick: () => { state.page -= 1; render(); } }, "← Назад"),
      h("span", { class: "meta" }, `стр. ${state.page} из ${pages} · всего ${data.total}`),
      h("button", { class: "btn", disabled: state.page >= pages, onclick: () => { state.page += 1; render(); } }, "Вперёд →")));
  }
  root().replaceChildren(...parts);
}

// --- карточка --------------------------------------------------------------------------

async function runAction(fn, ticketId) {
  try {
    await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      await alertMsg(err.message || "Обращение изменилось");
    } else {
      await alertMsg(err.message || String(err));
    }
  }
  state.ticketId = ticketId;
  await render();
}

async function renderTicket(id) {
  const back = () => { state.ticketId = null; render(); };
  setBackButton(back);
  const t = await api(`/api/tickets/${id}`);
  const fields = [
    ["Статус", STATUS[t.status]],
    ["Приоритет", PRIORITY[t.priority]],
    ["Id заявки", t.request_id],
    ["Id арендатора", t.tenant_id || "—"],
    ["Автор", userName(t.author)],
    ["Исполнитель", userName(t.assignee)],
    ["Создано", fmtDate(t.created_at)],
    ["Взято", fmtDate(t.taken_at)],
    ["Закрыто", fmtDate(t.closed_at)],
    ["Обновлено", fmtDate(t.updated_at)],
    ["Пост", t.has_post ? "опубликован" : "ещё не опубликован"],
  ];
  const parts = [
    h("button", { class: "back", onclick: back }, "← К списку"),
    h("div", { class: "section" },
      h("h2", {}, `Обращение #${t.id}`),
      h("dl", { class: "fields" }, fields.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))),
    h("div", { class: "section" }, h("h2", {}, "Описание"), h("p", { class: "description" }, t.description)),
  ];

  const p = t.permissions;
  if (p.can_reopen || p.can_reassign || p.can_restore_post) {
    const actions = h("div", { class: "actions" });
    if (p.can_reopen) {
      actions.append(h("button", {
        class: "btn primary",
        onclick: async () => {
          if (!(await confirmMsg(`Переоткрыть обращение #${t.id}?`))) return;
          await runAction(() => api(`/api/tickets/${t.id}/reopen`, {
            method: "POST", body: JSON.stringify({ version: t.version }),
          }), t.id);
        },
      }, "Переоткрыть"));
    }
    if (p.can_restore_post) {
      actions.append(h("button", {
        class: "btn",
        onclick: async () => {
          if (!(await confirmMsg("Опубликовать пост заново в чате разработчиков?"))) return;
          await runAction(() => api(`/api/tickets/${t.id}/restore-post`, { method: "POST" }), t.id);
        },
      }, "Восстановить пост"));
    }
    const section = h("div", { class: "section" }, h("h2", {}, "Действия администратора"), actions);
    if (p.can_reassign) section.append(await reassignControl(t));
    parts.push(section);
  }
  parts.push(h("p", { class: "hint" }, "Статусы меняются кнопками под постом в чате разработчиков."));
  root().replaceChildren(...parts);
}

async function reassignControl(t) {
  const users = await api("/api/users/assignees");
  const candidates = users.filter((u) => !t.assignee || u.id !== t.assignee.id);
  const select = h("select", { "aria-label": "Новый исполнитель" },
    h("option", { value: "" }, "Выберите исполнителя"),
    candidates.map((u) => h("option", { value: u.id }, `${u.display_name} — ${ROLES[u.role]}`)));
  const button = h("button", {
    class: "btn primary",
    onclick: async () => {
      if (!select.value) return;
      await runAction(() => api(`/api/tickets/${t.id}/reassign`, {
        method: "POST", body: JSON.stringify({ version: t.version, assignee_id: Number(select.value) }),
      }), t.id);
    },
  }, "Переназначить");
  return h("div", { class: "row", style: "margin-top: 12px" }, select, button);
}

// --- пользователи ----------------------------------------------------------------------

async function renderUsers() {
  setBackButton(null);
  const users = await api("/api/users");
  const message = h("p", { class: "error" });
  const rows = users.map((u) => {
    const self = u.id === state.me.id;
    const select = h("select", {
      "aria-label": `Роль ${u.display_name}`,
      disabled: self,
      onchange: async (e) => {
        const role = e.target.value || null;
        message.replaceChildren();
        try {
          await api(`/api/users/${u.id}/role`, { method: "PUT", body: JSON.stringify({ role }) });
        } catch (err) {
          message.append(err.message);
          if (err.body && Array.isArray(err.body.tickets)) {
            message.append(h("br"), "Активные обращения: ",
              ...err.body.tickets.flatMap((id, i) => [i ? ", " : "", h("a", {
                href: "#", onclick: (ev) => { ev.preventDefault(); state.ticketId = id; render(); },
              }, `#${id}`)]));
          }
        }
        await renderUsersKeepMessage(message);
      },
    }, Object.entries(ROLES).map(([k, v]) => h("option", { value: k, selected: (u.role || "") === k }, v)));
    return h("div", { class: "user-row" },
      h("div", {}, h("div", {}, u.display_name), h("div", { class: "meta" }, `Telegram ID ${u.telegram_id}${self ? " · это вы" : ""}`)),
      select);
  });
  root().replaceChildren(h("div", { class: "section" }, h("h2", {}, "Роли пользователей"), message, ...rows));
}

async function renderUsersKeepMessage(message) {
  const saved = Array.from(message.childNodes);
  await renderUsers();
  const fresh = root().querySelector(".error");
  if (fresh) fresh.replaceChildren(...saved);
}

// --- старт -----------------------------------------------------------------------------

async function main() {
  if (!tg || !tg.initData) {
    root().replaceChildren(h("p", { class: "hint" }, "Откройте приложение из Telegram (кнопка меню бота)."));
    return;
  }
  try {
    state.me = await api("/api/me");
  } catch (err) {
    showError(err);
    return;
  }
  state.tab = availableTabs()[0][0];
  await render();
}

main();
