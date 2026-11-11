"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  applyTheme(tg.themeParams || {});
}

const STATUS = {
  new: "Новое",
  in_progress: "В работе",
  waiting_info: "Ожидание информации",
  resolved: "Решено",
  rejected: "Отклонено",
  duplicate: "Дубль",
};
const PRIORITY = { critical: "Критичный", high: "Высокий", medium: "Средний", low: "Низкий" };
const ROLES = { support: "Поддержка", developer: "Разработчик", admin: "Администратор" };
const PAGE_SIZE = 20;

const state = { me: null, scope: "all", status: "", priority: "", page: 1 };
const view = document.getElementById("view");
const nav = document.getElementById("nav");

function applyTheme(params) {
  const root = document.documentElement;
  const map = {
    bg_color: "--bg", text_color: "--fg", hint_color: "--muted",
    button_color: "--accent", secondary_bg_color: "--card",
  };
  for (const [key, cssVar] of Object.entries(map)) {
    if (params[key]) root.style.setProperty(cssVar, params[key]);
  }
  root.dataset.theme = tg && tg.colorScheme === "dark" ? "dark" : "light";
}

function initData() {
  return (tg && tg.initData) || "";
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `tma ${initData()}`,
      ...(options.headers || {}),
    },
  });
  if (response.status === 204) return null;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || `Ошибка ${response.status}`);
    error.body = body;
    throw error;
  }
  return body;
}

function esc(value) {
  return String(value ?? "—").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmt(ts) {
  return ts ? new Date(ts).toLocaleString("ru-RU") : "—";
}

function showError(message) {
  const el = document.createElement("p");
  el.className = "error";
  el.textContent = message;
  view.prepend(el);
}

function renderNav() {
  nav.innerHTML = "";
  if (!state.me || !state.me.role) return;
  const tabs = [["all", "Все"]];
  if (state.me.role === "support" || state.me.role === "admin") tabs.unshift(["created", "Мои созданные"]);
  if (state.me.role === "developer" || state.me.role === "admin") tabs.unshift(["assigned", "Назначенные мне"]);
  for (const [scope, label] of tabs) {
    const button = document.createElement("button");
    button.className = scope === state.scope ? "" : "ghost";
    button.textContent = label;
    button.onclick = () => { state.scope = scope; state.page = 1; route(); };
    nav.appendChild(button);
  }
  if (state.me.role === "admin") {
    const roles = document.createElement("button");
    roles.className = "ghost";
    roles.textContent = "Роли";
    roles.onclick = () => { location.hash = "#/roles"; };
    nav.appendChild(roles);
  }
}

async function listScreen() {
  const filters = document.createElement("div");
  filters.className = "filters";
  filters.innerHTML = `
    <select id="f-status"><option value="">Все статусы</option>${Object.entries(STATUS)
      .map(([k, v]) => `<option value="${k}" ${k === state.status ? "selected" : ""}>${v}</option>`).join("")}</select>
    <select id="f-priority"><option value="">Любой приоритет</option>${Object.entries(PRIORITY)
      .map(([k, v]) => `<option value="${k}" ${k === state.priority ? "selected" : ""}>${v}</option>`).join("")}</select>`;
  filters.querySelector("#f-status").onchange = (e) => { state.status = e.target.value; state.page = 1; route(); };
  filters.querySelector("#f-priority").onchange = (e) => { state.priority = e.target.value; state.page = 1; route(); };
  view.replaceChildren(filters);

  const params = new URLSearchParams({ scope: state.scope, page: state.page, size: PAGE_SIZE });
  if (state.status) params.set("status", state.status);
  if (state.priority) params.set("priority", state.priority);
  const data = await api(`/tickets?${params}`);

  if (data.items.length === 0) {
    const empty = document.createElement("p");
    empty.className = "meta";
    empty.textContent = "Обращений нет";
    view.appendChild(empty);
  }
  for (const t of data.items) {
    const link = document.createElement("a");
    link.className = "card";
    link.href = `#/ticket/${t.id}`;
    link.innerHTML = `
      <div><strong>#${t.id}</strong> <span class="badge">${esc(STATUS[t.status])}</span>
        <span class="badge">${esc(PRIORITY[t.priority])}</span></div>
      <div>${esc(t.description.slice(0, 140))}</div>
      <div class="meta">${esc(fmt(t.created_at))} · исполнитель: ${esc(t.assignee)}</div>`;
    view.appendChild(link);
  }
  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
  const pager = document.createElement("div");
  pager.className = "pager";
  pager.innerHTML = `<button class="ghost" ${state.page <= 1 ? "disabled" : ""} id="prev">←</button>
    <span class="meta">${state.page} / ${pages}</span>
    <button class="ghost" ${state.page >= pages ? "disabled" : ""} id="next">→</button>`;
  pager.querySelector("#prev").onclick = () => { state.page -= 1; route(); };
  pager.querySelector("#next").onclick = () => { state.page += 1; route(); };
  view.appendChild(pager);
}

async function ticketScreen(id) {
  const t = await api(`/tickets/${id}`);
  const wrap = document.createElement("section");
  wrap.innerHTML = `
    <p><a href="#/">← к списку</a></p>
    <h2>Обращение #${t.id}</h2>
    <dl>
      <dt>Статус</dt><dd>${esc(STATUS[t.status])}</dd>
      <dt>Приоритет</dt><dd>${esc(PRIORITY[t.priority])}</dd>
      <dt>Автор</dt><dd>${esc(t.author)}</dd>
      <dt>Исполнитель</dt><dd>${esc(t.assignee)}</dd>
      <dt>ID заявки</dt><dd>${esc(t.request_id)}</dd>
      <dt>ID арендатора</dt><dd>${esc(t.tenant_id)}</dd>
      <dt>Создано</dt><dd>${esc(fmt(t.created_at))}</dd>
      <dt>Взято</dt><dd>${esc(fmt(t.taken_at))}</dd>
      <dt>Закрыто</dt><dd>${esc(fmt(t.closed_at))}</dd>
    </dl>
    <h3>Описание</h3>
    <pre class="desc">${esc(t.description)}</pre>
    <p class="meta">Статусы меняются кнопками под постом в чате разработчиков.</p>
    <div class="actions" id="actions"></div>`;
  view.replaceChildren(wrap);
  const actions = wrap.querySelector("#actions");

  if (t.can_reopen) actions.appendChild(button("Переоткрыть", async () => {
    await api(`/tickets/${id}/reopen`, { method: "POST", body: JSON.stringify({ expected_version: t.version }) });
    route();
  }));
  if (t.can_reassign) {
    const users = await api("/users");
    const select = document.createElement("select");
    select.innerHTML = users
      .filter((u) => u.role === "developer" || u.role === "admin")
      .map((u) => `<option value="${u.id}" ${u.id === t.assignee_id ? "selected" : ""}>${esc(u.name)}</option>`).join("");
    actions.appendChild(select);
    actions.appendChild(button("Переназначить", async () => {
      await api(`/tickets/${id}/reassign`, {
        method: "POST",
        body: JSON.stringify({ expected_version: t.version, assignee_id: Number(select.value) }),
      });
      route();
    }));
  }
  if (t.can_restore_post) actions.appendChild(button("Восстановить пост", async () => {
    await api(`/tickets/${id}/restore-post`, { method: "POST" });
    showError("Пост опубликован заново");
  }, "ghost"));
}

async function rolesScreen() {
  const users = await api("/users");
  const wrap = document.createElement("section");
  wrap.innerHTML = `<p><a href="#/">← к списку</a></p><h2>Роли</h2>`;
  for (const u of users) {
    const row = document.createElement("div");
    row.className = "card";
    row.innerHTML = `<div><strong>${esc(u.name)}</strong> ${u.username ? `@${esc(u.username)}` : ""}</div>
      <div class="meta">${esc(u.telegram_id)}</div>`;
    const select = document.createElement("select");
    select.innerHTML = `<option value="">Без роли</option>${Object.entries(ROLES)
      .map(([k, v]) => `<option value="${k}" ${k === u.role ? "selected" : ""}>${v}</option>`).join("")}`;
    row.appendChild(select);
    row.appendChild(button("Сохранить", async () => {
      try {
        await api(`/users/${u.telegram_id}/role`, {
          method: "PUT",
          body: JSON.stringify({ role: select.value || null }),
        });
        route();
      } catch (error) {
        const ids = error.body && error.body.ticket_ids ? ` Обращения: ${error.body.ticket_ids.map((x) => `#${x}`).join(", ")}` : "";
        showError(error.message + ids);
      }
    }));
    wrap.appendChild(row);
  }
  view.replaceChildren(wrap);
}

function button(label, onClick, kind = "") {
  const el = document.createElement("button");
  el.textContent = label;
  if (kind) el.className = kind;
  el.onclick = async () => {
    try {
      await onClick();
    } catch (error) {
      showError(error.message);
    }
  };
  return el;
}

async function route() {
  view.replaceChildren();
  try {
    const hash = location.hash || "#/";
    if (hash.startsWith("#/ticket/")) {
      await ticketScreen(hash.split("/")[2]);
    } else if (hash === "#/roles") {
      await rolesScreen();
    } else {
      if (!state.me.role) {
        view.textContent = "Доступа пока нет. Обратитесь к администратору.";
        return;
      }
      await listScreen();
    }
  } catch (error) {
    showError(error.message);
  }
}

async function start() {
  try {
    state.me = await api("/me");
  } catch (error) {
    view.textContent = `Не удалось войти: ${error.message}`;
    return;
  }
  if (state.me.role === "developer") state.scope = "assigned";
  if (state.me.role === "support") state.scope = "created";
  renderNav();
  window.addEventListener("hashchange", route);
  route();
}

start();
