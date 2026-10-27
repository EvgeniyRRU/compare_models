"use strict";
const tg = window.Telegram?.WebApp;
const $ = (id) => document.getElementById(id);
const statuses = {new: "Новый", in_progress: "В работе", waiting_info: "В ожидании информации", resolved: "Решено", rejected: "Отклонено", duplicate: "Дубль"};
const priorities = {critical: "Критический", high: "Высокий", medium: "Средний", low: "Низкий"};
const roles = {support: "Первая линия", developer: "Разработчик", admin: "Администратор"};
const state = {me: null, scope: "all", page: 1, userPage: 1, pageSize: 20, view: "tickets", detail: null, ticketRequest: 0, userRequest: 0};
const dateFormat = new Intl.DateTimeFormat("ru-RU", {dateStyle: "medium", timeStyle: "short"});
const date = (value) => value ? dateFormat.format(new Date(value)) : "—";
const name = (user) => user ? `${user.first_name} ${user.last_name || ""}`.trim() + (user.username ? ` (@${user.username})` : "") : "Не назначен";
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message = "", error = false) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
  $("notice").classList.toggle("error", error);
}
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {...options, headers: {"Authorization": `tma ${tg?.initData || ""}`, "Content-Type": "application/json", ...options.headers}});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body.message || (typeof body.detail === "string" ? body.detail : "Не удалось выполнить запрос");
    const ids = body.ticket_ids?.length ? ` Обращения: ${body.ticket_ids.map(id => `#${id}`).join(", ")}.` : "";
    const error = new Error(message + ids);
    error.status = response.status;
    throw error;
  }
  return body;
}
function applyRole() {
  const role = state.me.role;
  document.querySelector('[data-scope="created"]').hidden = !["support", "admin"].includes(role);
  document.querySelector('[data-scope="assigned"]').hidden = !["developer", "admin"].includes(role);
  $("users-tab").hidden = role !== "admin";
  $("identity").textContent = `${name(state.me)} · ${roles[role] || "Без доступа"}`;
  if ((state.scope === "created" && role === "developer") || (state.scope === "assigned" && role === "support")) state.scope = "all";
}
function showView(view) {
  state.view = view;
  $("ticket-view").hidden = view !== "tickets";
  $("user-view").hidden = view !== "users";
  $("detail-view").hidden = view !== "detail";
  document.querySelectorAll("[data-scope]").forEach(button => {
    if (view !== "users" && button.dataset.scope === state.scope) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (view === "users") $("users-tab").setAttribute("aria-current", "page");
  else $("users-tab").removeAttribute("aria-current");
  if (view === "detail") tg?.BackButton?.show();
  else tg?.BackButton?.hide();
}
function pagination(prefix, page, total) {
  const pages = Math.max(1, Math.ceil(total / state.pageSize));
  $(`${prefix}previous`).disabled = page <= 1;
  $(`${prefix}next`).disabled = page >= pages;
  $(`${prefix}page-label`).textContent = `${page} / ${pages}`;
}
async function loadTickets() {
  const request = ++state.ticketRequest;
  const query = new URLSearchParams({scope: state.scope, page: state.page, page_size: state.pageSize});
  if ($("status").value) query.set("status", $("status").value);
  if ($("priority").value) query.set("priority", $("priority").value);
  $("tickets").replaceChildren(element("p", "Загрузка…", "empty"));
  try {
    const data = await api(`/tickets?${query}`);
    if (request !== state.ticketRequest) return;
    $("ticket-count").textContent = `Обращений: ${data.total}`;
    pagination("", data.page, data.total);
    $("tickets").replaceChildren();
    if (!data.items.length) $("tickets").append(element("p", "Обращений с такими условиями пока нет", "empty"));
    for (const ticket of data.items) {
      const row = element("button", undefined, "ticket");
      row.type = "button";
      const top = element("div", undefined, "ticket-top");
      top.append(element("span", `#${ticket.id} · ${statuses[ticket.status]}`, "ticket-number"));
      const pill = element("span", priorities[ticket.priority], "pill");
      pill.dataset.priority = ticket.priority;
      top.append(pill);
      const bottom = element("div", undefined, "ticket-bottom muted");
      bottom.append(element("span", `Заявка ${ticket.request_id}`), element("span", date(ticket.created_at)));
      row.append(top, element("p", ticket.description.length > 150 ? `${ticket.description.slice(0, 150)}…` : ticket.description, "ticket-description"), bottom);
      row.addEventListener("click", () => openTicket(ticket.id));
      $("tickets").append(row);
    }
  } catch (error) {
    if (request === state.ticketRequest) { $("tickets").replaceChildren(); notice(error.message, true); }
  }
}
function addField(list, label, value) {
  list.append(element("dt", label), element("dd", value === null || value === undefined ? "—" : String(value)));
}
async function openTicket(id, keepNotice = false) {
  if (!keepNotice) notice();
  showView("detail");
  state.detail = id;
  $("detail").replaceChildren(element("p", "Загрузка…", "empty"));
  try {
    const ticket = await api(`/tickets/${id}`);
    if (state.detail !== id || state.view !== "detail") return;
    const panel = element("article", undefined, "detail-panel");
    panel.append(element("h2", `Обращение #${ticket.id}`), element("p", `${statuses[ticket.status]} · ${priorities[ticket.priority]}`, "muted"), element("p", ticket.description, "description"));
    const fields = element("dl");
    [
      ["ID заявки", ticket.request_id], ["ID арендатора", ticket.tenant_id],
      ["Автор", `${name(ticket.author)} · ID ${ticket.author_id}`], ["Telegram ID автора", ticket.author?.telegram_id],
      ["Исполнитель", name(ticket.assignee)], ["ID исполнителя", ticket.assignee_id], ["Telegram ID исполнителя", ticket.assignee?.telegram_id],
      ["Создано", date(ticket.created_at)], ["Обновлено", date(ticket.updated_at)], ["Начало назначения", date(ticket.taken_at)],
      ["Закрыто", date(ticket.closed_at)], ["Следующее напоминание", date(ticket.next_reminder_at)],
      ["Часовой пояс", Intl.DateTimeFormat().resolvedOptions().timeZone], ["Версия", ticket.version],
      ["ID поста в чате", ticket.message_id], ["Версия поста", ticket.post_synced_version],
    ].forEach(([label, value]) => addField(fields, label, value));
    panel.append(fields);
    $("detail").replaceChildren(panel);
    if (state.me.role === "admin") await adminActions(panel, ticket);
  } catch (error) { notice(error.message, true); }
}
async function mutateTicket(ticket, action, button, extra = {}) {
  button.disabled = true;
  notice();
  try {
    const options = {method: "POST"};
    if (action !== "restore-post") options.body = JSON.stringify({expected_version: ticket.version, expected_status: ticket.status, ...extra});
    await api(`/tickets/${ticket.id}/${action}`, options);
    await openTicket(ticket.id);
    notice(action === "restore-post" ? "Синхронизация поста выполнена" : "Изменения сохранены");
  } catch (error) {
    if (error.status === 409) await openTicket(ticket.id, true);
    notice(error.message, true);
  } finally { button.disabled = false; }
}
async function allEligible() {
  const users = [];
  for (let page = 1; ; page++) {
    const data = await api(`/users?eligible=true&page_size=100&page=${page}`);
    users.push(...data.items);
    if (users.length >= data.total || !data.items.length) return users;
  }
}
async function adminActions(panel, ticket) {
  const actions = element("div", undefined, "actions");
  panel.append(actions);
  if (["resolved", "rejected", "duplicate"].includes(ticket.status)) {
    const reopen = element("button", "Переоткрыть");
    reopen.type = "button";
    reopen.onclick = () => mutateTicket(ticket, "reopen", reopen);
    actions.append(reopen);
  }
  if (["in_progress", "waiting_info"].includes(ticket.status)) {
    const label = element("label", "Новый исполнитель");
    const select = element("select");
    select.append(new Option("Выберите исполнителя", ""));
    label.append(select);
    const reassign = element("button", "Переназначить");
    reassign.type = "button";
    reassign.disabled = true;
    select.onchange = () => { reassign.disabled = !select.value; };
    reassign.onclick = () => mutateTicket(ticket, "reassign", reassign, {assignee_id: Number(select.value)});
    actions.append(label, reassign);
    try {
      const eligible = await allEligible();
      eligible.forEach(user => select.append(new Option(`${name(user)} · ${roles[user.role]}`, user.id)));
    } catch (error) { notice(error.message, true); }
  }
  const restore = element("button", "Восстановить пост", "secondary");
  restore.type = "button";
  restore.onclick = () => mutateTicket(ticket, "restore-post", restore);
  actions.append(restore);
}
async function loadUsers() {
  const request = ++state.userRequest;
  $("users").replaceChildren(element("p", "Загрузка…", "empty"));
  try {
    const data = await api(`/users?page=${state.userPage}&page_size=${state.pageSize}`);
    if (request !== state.userRequest) return;
    $("users").replaceChildren();
    pagination("users-", data.page, data.total);
    for (const user of data.items) {
      const row = element("div", undefined, "user-row");
      const info = element("div");
      info.append(element("strong", name(user)), element("p", `Telegram ID: ${user.telegram_id}`, "muted"));
      const select = element("select");
      select.setAttribute("aria-label", `Роль: ${name(user)}`);
      select.append(new Option("Без доступа", ""));
      Object.entries(roles).forEach(([key, title]) => select.append(new Option(title, key)));
      select.value = user.role || "";
      const save = element("button", "Сохранить", "secondary");
      save.type = "button";
      save.onclick = async () => {
        save.disabled = true;
        try {
          const updatedUser = await api(`/users/${user.id}/role`, {method: "PATCH", body: JSON.stringify({role: select.value || null})});
          notice("Роль сохранена");
          if (user.id === state.me.id) {
            state.me = updatedUser;
            applyRole();
            if (!state.me.role) {
              state.detail = null;
              $("workspace").hidden = true;
              tg?.BackButton?.hide();
              notice("Доступ не назначен. Обратитесь к администратору.", true);
              return;
            }
            if (state.me.role !== "admin") {
              state.scope = state.me.role === "developer" ? "assigned" : "created";
              state.page = 1;
              showView("tickets");
              await loadTickets();
            }
          }
        } catch (error) { notice(error.message, true); }
        finally { save.disabled = false; }
      };
      row.append(info, select, save);
      $("users").append(row);
    }
  } catch (error) { if (request === state.userRequest) { $("users").replaceChildren(); notice(error.message, true); } }
}
function back() { state.detail = null; showView("tickets"); loadTickets(); }
document.querySelectorAll("[data-scope]").forEach(button => button.onclick = () => {
  state.scope = button.dataset.scope;
  state.page = 1;
  state.detail = null;
  notice();
  showView("tickets");
  loadTickets();
});
["status", "priority"].forEach(id => $(id).onchange = () => { state.page = 1; loadTickets(); });
$("refresh").onclick = () => { notice(); loadTickets(); };
$("previous").onclick = () => { state.page--; loadTickets(); };
$("next").onclick = () => { state.page++; loadTickets(); };
$("users-previous").onclick = () => { state.userPage--; loadUsers(); };
$("users-next").onclick = () => { state.userPage++; loadUsers(); };
$("users-tab").onclick = () => { notice(); state.detail = null; showView("users"); loadUsers(); };
$("back").onclick = back;
tg?.BackButton?.onClick(back);
(async function boot() {
  tg?.ready();
  tg?.expand();
  if (!tg?.initData) { notice("Откройте приложение через меню Telegram-бота", true); return; }
  try {
    state.me = await api("/me");
    applyRole();
    state.scope = state.me.role === "developer" ? "assigned" : "created";
    $("workspace").hidden = false;
    showView("tickets");
    await loadTickets();
  } catch (error) { notice(error.message, true); }
})();
