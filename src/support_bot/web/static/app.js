(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const initData = tg ? tg.initData : "";

  const STATUS_LABELS = {
    new: "Новое",
    in_progress: "В работе",
    waiting_info: "Ожидает информации",
    resolved: "Решено",
    rejected: "Отклонено",
    duplicate: "Дубль",
  };

  const PRIORITY_LABELS = {
    critical: "Критический",
    high: "Высокий",
    medium: "Средний",
    low: "Низкий",
  };

  const ROLE_LABELS = {
    support: "Поддержка",
    developer: "Разработчик",
    admin: "Администратор",
    "": "Без роли",
  };

  const FINAL_STATUSES = new Set(["resolved", "rejected", "duplicate"]);

  const state = {
    me: null,
    view: "list",
    scope: "authored",
    status: "",
    priority: "",
    page: 1,
    pageSize: 20,
    ticket: null,
    users: [],
    usersPage: 1,
    assignable: [],
  };

  const content = document.getElementById("content");
  const tabs = document.getElementById("tabs");
  const userLine = document.getElementById("user-line");

  function esc(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function applyTheme() {
    if (!tg) return;
    const params = tg.themeParams || {};
    const root = document.documentElement;
    const mapping = {
      bg_color: "--bg",
      secondary_bg_color: "--secondary-bg",
      text_color: "--text",
      hint_color: "--hint",
      link_color: "--link",
      button_color: "--button",
      button_text_color: "--button-text",
    };
    for (const [key, cssVar] of Object.entries(mapping)) {
      if (params[key]) root.style.setProperty(cssVar, params[key]);
    }
    document.body.classList.toggle("dark", tg.colorScheme === "dark");
  }

  function toast(message) {
    const element = document.getElementById("toast");
    element.textContent = message;
    element.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(() => {
      element.hidden = true;
    }, 2600);
  }

  async function api(path, options = {}) {
    const headers = Object.assign({}, options.headers || {});
    if (initData) headers.Authorization = `tma ${initData}`;
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(path, Object.assign({}, options, { headers }));
    if (response.status === 401) {
      showFatal("Откройте приложение через Telegram.");
      throw new Error("unauthorized");
    }
    if (response.status === 403) {
      showFatal("Доступ не выдан. Обратитесь к администратору.");
      throw new Error("forbidden");
    }
    if (!response.ok) {
      let detail = null;
      try {
        const payload = await response.json();
        detail = payload.detail || null;
      } catch (error) {
        detail = null;
      }
      const failure = new Error(typeof detail === "string" ? detail : "request failed");
      failure.status = response.status;
      failure.detail = detail;
      throw failure;
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function showFatal(message) {
    tabs.innerHTML = "";
    content.innerHTML = `<div class="error-box">${esc(message)}</div>`;
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return `${date.toLocaleString("ru-RU", { timeZone: "UTC" })} UTC`;
  }

  function userTitle(user) {
    if (!user) return "—";
    const name = [user.first_name, user.last_name].filter(Boolean).join(" ");
    if (name && user.username) return `${name} (@${user.username})`;
    if (name) return name;
    if (user.username) return `@${user.username}`;
    return String(user.telegram_id);
  }

  function statusBadge(status) {
    return `<span class="badge status-${esc(status)}">${esc(STATUS_LABELS[status] || status)}</span>`;
  }

  function priorityBadge(priority) {
    return `<span class="badge priority-${esc(priority)}">${esc(PRIORITY_LABELS[priority] || priority)}</span>`;
  }

  function tabsFor(me) {
    const items = [];
    if (me.is_admin) {
      items.push({ key: "authored", label: "Созданные мной" });
      items.push({ key: "assigned", label: "Назначенные мне" });
      items.push({ key: "all", label: "Все обращения" });
      items.push({ key: "users", label: "Пользователи" });
    } else if (me.can_create) {
      items.push({ key: "authored", label: "Мои обращения" });
      items.push({ key: "all", label: "Все обращения" });
    } else {
      items.push({ key: "assigned", label: "Назначенные мне" });
      items.push({ key: "all", label: "Все обращения" });
    }
    return items;
  }

  function renderTabs() {
    const items = tabsFor(state.me);
    tabs.innerHTML = "";
    for (const item of items) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tab";
      const active =
        state.view === "users" ? item.key === "users" : item.key === state.scope;
      if (active) button.classList.add("active");
      button.textContent = item.label;
      button.addEventListener("click", () => {
        if (item.key === "users") {
          state.view = "users";
          state.ticket = null;
          renderUsers(1);
        } else {
          state.view = "list";
          state.scope = item.key;
          state.page = 1;
          state.ticket = null;
          loadList();
        }
        renderTabs();
      });
      tabs.appendChild(button);
    }
  }

  async function loadMe() {
    state.me = await api("/api/me");
    userLine.textContent = `${userTitle(state.me.user)} · ${
      ROLE_LABELS[state.me.user.role] || state.me.user.role
    }`;
    const available = tabsFor(state.me).map((item) => item.key);
    if (state.view === "users" && !available.includes("users")) state.view = "list";
    if (!available.includes(state.scope)) state.scope = available[0];
    renderTabs();
    if (state.view === "list") await loadList();
  }

  function filtersHtml() {
    const statuses = ["new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate"];
    const priorities = ["critical", "high", "medium", "low"];
    return `
      <div class="filters">
        <select id="filter-status">
          <option value="">Все статусы</option>
          ${statuses
            .map(
              (value) =>
                `<option value="${value}" ${state.status === value ? "selected" : ""}>${
                  STATUS_LABELS[value]
                }</option>`
            )
            .join("")}
        </select>
        <select id="filter-priority">
          <option value="">Все приоритеты</option>
          ${priorities
            .map(
              (value) =>
                `<option value="${value}" ${
                  state.priority === value ? "selected" : ""
                }>${PRIORITY_LABELS[value]}</option>`
            )
            .join("")}
        </select>
        <button type="button" class="secondary" id="apply-filters">Применить</button>
      </div>`;
  }

  function bindFilters() {
    const status = document.getElementById("filter-status");
    const priority = document.getElementById("filter-priority");
    if (status) status.addEventListener("change", () => (state.status = status.value));
    if (priority)
      priority.addEventListener("change", () => (state.priority = priority.value));
    const apply = document.getElementById("apply-filters");
    if (apply) {
      apply.addEventListener("click", () => {
        state.page = 1;
        loadList();
      });
    }
  }

  async function loadList() {
    state.view = "list";
    content.innerHTML = `<div class="empty">Загрузка…</div>`;
    const params = new URLSearchParams({
      scope: state.scope,
      page: String(state.page),
      page_size: String(state.pageSize),
    });
    if (state.status) params.set("status", state.status);
    if (state.priority) params.set("priority", state.priority);
    try {
      const data = await api(`/api/tickets?${params.toString()}`);
      content.innerHTML = filtersHtml();
      bindFilters();
      if (!data.items.length) {
        content.insertAdjacentHTML("beforeend", `<div class="empty">Обращений нет</div>`);
        return;
      }
      const cards = document.createElement("div");
      cards.className = "cards";
      for (const ticket of data.items) {
        const card = document.createElement("div");
        card.className = "card";
        card.innerHTML = `
          <div class="card-head">
            <span class="ticket-id">#${ticket.id}</span>
            <span>${statusBadge(ticket.status)} ${priorityBadge(ticket.priority)}</span>
          </div>
          <div>${esc(ticket.request_id)}${ticket.tenant_id ? ` · ${esc(ticket.tenant_id)}` : ""}</div>
          <div class="card-meta">
            <span>Автор: ${esc(userTitle(ticket.author))}</span>
            <span>Исполнитель: ${esc(userTitle(ticket.assignee))}</span>
            <span>${esc(formatDate(ticket.created_at))}</span>
          </div>`;
        card.addEventListener("click", () => openTicket(ticket.id));
        cards.appendChild(card);
      }
      content.appendChild(cards);
      const pages = Math.max(1, Math.ceil(data.total / data.page_size));
      const pager = document.createElement("div");
      pager.className = "pagination";
      pager.innerHTML = `
        <button type="button" class="secondary" id="prev-page" ${
          state.page <= 1 ? "disabled" : ""
        }>Назад</button>
        <span class="hint">Страница ${state.page} из ${pages} · всего ${data.total}</span>
        <button type="button" class="secondary" id="next-page" ${
          state.page >= pages ? "disabled" : ""
        }>Вперёд</button>`;
      content.appendChild(pager);
      const prev = document.getElementById("prev-page");
      const next = document.getElementById("next-page");
      if (prev) prev.addEventListener("click", () => {
        state.page -= 1;
        loadList();
      });
      if (next) next.addEventListener("click", () => {
        state.page += 1;
        loadList();
      });
    } catch (error) {
      if (!error.status) content.innerHTML = `<div class="error-box">Не удалось загрузить список.</div>`;
    }
  }

  async function openTicket(id) {
    state.view = "detail";
    content.innerHTML = `<div class="empty">Загрузка…</div>`;
    try {
      state.ticket = await api(`/api/tickets/${id}`);
      if (state.me.is_admin && ["in_progress", "waiting_info"].includes(state.ticket.status)) {
        try {
          state.assignable = await api("/api/users/assignable");
        } catch (error) {
          state.assignable = [];
        }
      } else {
        state.assignable = [];
      }
      renderDetail();
    } catch (error) {
      content.innerHTML = `<div class="error-box">Обращение не найдено.</div>`;
    }
  }

  function renderDetail() {
    const ticket = state.ticket;
    if (!ticket) return;
    const canReopen = state.me.is_admin && FINAL_STATUSES.has(ticket.status);
    const canReassign =
      state.me.is_admin && ["in_progress", "waiting_info"].includes(ticket.status);
    const assigneeOptions = state.assignable
      .map(
        (user) =>
          `<option value="${user.id}" ${
            ticket.assignee && ticket.assignee.id === user.id ? "selected" : ""
          }>${esc(userTitle(user))}</option>`
      )
      .join("");

    content.innerHTML = `
      <button type="button" class="secondary back" id="back-button">← К списку</button>
      <h2>#${ticket.id} ${statusBadge(ticket.status)} ${priorityBadge(ticket.priority)}</h2>
      <dl class="detail-grid">
        <dt>Автор</dt><dd>${esc(userTitle(ticket.author))}</dd>
        <dt>Исполнитель</dt><dd>${esc(userTitle(ticket.assignee))}</dd>
        <dt>ID заявки</dt><dd>${esc(ticket.request_id)}</dd>
        <dt>ID арендатора</dt><dd>${esc(ticket.tenant_id || "—")}</dd>
        <dt>Приоритет</dt><dd>${esc(PRIORITY_LABELS[ticket.priority])}</dd>
        <dt>Создано</dt><dd>${esc(formatDate(ticket.created_at))}</dd>
        <dt>Взято</dt><dd>${esc(formatDate(ticket.taken_at))}</dd>
        <dt>Закрыто</dt><dd>${esc(formatDate(ticket.closed_at))}</dd>
        <dt>Версия</dt><dd>${ticket.version}</dd>
      </dl>
      <div class="description">${esc(ticket.description)}</div>
      <div class="hint">Статусы меняются кнопками в чате разработчиков.</div>
      ${
        state.me.is_admin
          ? `<div class="actions">
              ${
                canReopen
                  ? `<button type="button" class="primary" id="reopen-button">Переоткрыть</button>`
                  : ""
              }
              ${
                canReassign
                  ? `<select id="assignee-select">${assigneeOptions}</select>
                     <button type="button" class="primary" id="reassign-button">Переназначить</button>`
                  : ""
              }
              <button type="button" class="secondary" id="restore-button">Восстановить пост</button>
            </div>`
          : ""
      }`;
    const back = document.getElementById("back-button");
    if (back) back.addEventListener("click", () => loadList());
    const reopen = document.getElementById("reopen-button");
    if (reopen) reopen.addEventListener("click", reopenTicket);
    const reassign = document.getElementById("reassign-button");
    if (reassign) reassign.addEventListener("click", reassignTicket);
    const restore = document.getElementById("restore-button");
    if (restore) restore.addEventListener("click", restorePost);
  }

  async function reopenTicket() {
    const ticket = state.ticket;
    try {
      state.ticket = await api(`/api/tickets/${ticket.id}/reopen`, {
        method: "POST",
        body: JSON.stringify({ version: ticket.version }),
      });
      toast("Обращение переоткрыто");
      renderDetail();
    } catch (error) {
      if (error.status === 409) {
        toast("Обращение изменилось, обновите карточку");
        await openTicket(ticket.id);
      } else {
        toast("Не удалось переоткрыть обращение");
      }
    }
  }

  async function reassignTicket() {
    const ticket = state.ticket;
    const select = document.getElementById("assignee-select");
    if (!select || !select.value) return;
    try {
      state.ticket = await api(`/api/tickets/${ticket.id}/reassign`, {
        method: "POST",
        body: JSON.stringify({
          version: ticket.version,
          assignee_id: Number(select.value),
        }),
      });
      toast("Исполнитель изменён");
      renderDetail();
    } catch (error) {
      if (error.status === 409) {
        toast("Обращение изменилось, обновите карточку");
        await openTicket(ticket.id);
      } else {
        toast("Не удалось переназначить обращение");
      }
    }
  }

  async function restorePost() {
    const ticket = state.ticket;
    try {
      await api(`/api/tickets/${ticket.id}/restore-post`, { method: "POST" });
      toast("Пост опубликован заново");
    } catch (error) {
      toast("Не удалось восстановить пост");
    }
  }

  async function renderUsers(page) {
    state.view = "users";
    state.usersPage = page;
    content.innerHTML = `<div class="empty">Загрузка…</div>`;
    try {
      const data = await api(`/api/users?page=${page}&page_size=50`);
      state.users = data.items;
      content.innerHTML = `<h2>Пользователи</h2><div id="users-list"></div>`;
      const list = document.getElementById("users-list");
      for (const user of data.items) {
        const row = document.createElement("div");
        row.className = "user-row";
        const options = ["", "support", "developer", "admin"]
          .map(
            (role) =>
              `<option value="${role}" ${
                (user.role || "") === role ? "selected" : ""
              }>${ROLE_LABELS[role]}</option>`
          )
          .join("");
        row.innerHTML = `
          <div>
            <div class="user-name">${esc(userTitle(user))}</div>
            <div class="hint">telegram_id: ${user.telegram_id}</div>
          </div>
          <div class="actions">
            <select data-user-id="${user.id}">${options}</select>
            <button type="button" class="primary" data-save-user="${user.id}">Сохранить</button>
          </div>`;
        list.appendChild(row);
        const save = row.querySelector(`[data-save-user="${user.id}"]`);
        const select = row.querySelector(`[data-user-id="${user.id}"]`);
        save.addEventListener("click", () => saveRole(user.id, select.value));
      }
      const pages = Math.max(1, Math.ceil(data.total / data.page_size));
      const pager = document.createElement("div");
      pager.className = "pagination";
      pager.innerHTML = `
        <button type="button" class="secondary" id="users-prev" ${
          page <= 1 ? "disabled" : ""
        }>Назад</button>
        <span class="hint">Страница ${page} из ${pages} · всего ${data.total}</span>
        <button type="button" class="secondary" id="users-next" ${
          page >= pages ? "disabled" : ""
        }>Вперёд</button>`;
      content.appendChild(pager);
      const prev = document.getElementById("users-prev");
      const next = document.getElementById("users-next");
      if (prev) prev.addEventListener("click", () => renderUsers(page - 1));
      if (next) next.addEventListener("click", () => renderUsers(page + 1));
    } catch (error) {
      content.innerHTML = `<div class="error-box">Не удалось загрузить пользователей.</div>`;
    }
  }

  async function saveRole(userId, role) {
    try {
      await api(`/api/users/${userId}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role: role || null }),
      });
      toast("Роль обновлена");
    } catch (error) {
      if (error.status === 409 && error.detail && error.detail.ticket_ids) {
        const ids = error.detail.ticket_ids.map((id) => `#${id}`).join(", ");
        toast(`Сначала переназначьте активные обращения: ${ids}`);
      } else {
        toast("Не удалось изменить роль");
      }
    }
  }

  async function start() {
    applyTheme();
    if (!tg) {
      showFatal("Откройте приложение через Telegram.");
      return;
    }
    tg.ready();
    tg.expand();
    if (!initData) {
      showFatal("Не удалось получить данные авторизации Telegram.");
      return;
    }
    document.getElementById("refresh-button").addEventListener("click", () => {
      if (state.view === "detail" && state.ticket) openTicket(state.ticket.id);
      else if (state.view === "users") renderUsers(state.usersPage);
      else loadList();
    });
    try {
      await loadMe();
    } catch (error) {
      if (!error.status) showFatal("Не удалось загрузить приложение.");
    }
  }

  start();
})();