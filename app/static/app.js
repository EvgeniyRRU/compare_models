"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  applyTheme(tg);
}
const INIT_DATA = (tg && tg.initData) || "";

const STATUS_RU = {
  new: "новое", in_progress: "в работе", waiting_info: "ждём информацию",
  resolved: "решено", rejected: "отклонено", duplicate: "дубль",
};
const PRIORITY_RU = { critical: "критический", high: "высокий", medium: "средний", low: "низкий" };
const FINAL = ["resolved", "rejected", "duplicate"];
const ACTIVE = ["in_progress", "waiting_info"];

let me = null;
const state = { tab: "mine", page: 1, per_page: 20, status: "", priority: "", total: 0 };

function applyTheme(tg) {
  const p = tg.themeParams || {};
  const root = document.documentElement.style;
  root.setProperty("--bg", p.bg_color || "#ffffff");
  root.setProperty("--text", p.text_color || "#000000");
  root.setProperty("--hint", p.hint_color || "#999999");
  root.setProperty("--secondary-bg", p.secondary_bg_color || "#f4f4f5");
  root.setProperty("--button", p.button_color || "#2481cc");
  root.setProperty("--button-text", p.button_text_color || "#ffffff");
  if (tg.setHeaderColor) tg.setHeaderColor(p.header_color || p.bg_color || "#ffffff");
  if (tg.setBackgroundColor) tg.setBackgroundColor(p.bg_color || "#ffffff");
}

async function api(path, opts) {
  const { method = "GET", body } = opts || {};
  const url = path + (path.includes("?") ? "&" : "?") + "initData=" + encodeURIComponent(INIT_DATA);
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail; } catch (_) { /* ignore */ }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.json();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function loadMe() {
  me = await api("/api/me");
  if (me.role === "admin") document.getElementById("tab-roles").hidden = false;
  switchTab("mine");
}

async function switchTab(tab) {
  state.tab = tab;
  state.page = 1;
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab)
  );
  const inList = tab === "mine" || tab === "all";
  document.getElementById("view-list").hidden = !inList;
  document.getElementById("view-card").hidden = true;
  document.getElementById("view-roles").hidden = tab !== "roles";
  if (tab === "roles") await loadUsers();
  else await loadList();
}

async function loadList() {
  const params = new URLSearchParams({ tab: state.tab, page: state.page, per_page: state.per_page });
  if (state.status) params.set("status", state.status);
  if (state.priority) params.set("priority", state.priority);
  const data = await api("/api/tickets?" + params.toString());
  state.total = data.total;
  renderList(data.items);
  const pages = Math.max(1, Math.ceil(data.total / state.per_page));
  document.getElementById("page-info").textContent = state.page + " / " + pages;
  document.getElementById("prev").disabled = state.page <= 1;
  document.getElementById("next").disabled = state.page >= pages;
}

function renderList(items) {
  const ul = document.getElementById("ticket-list");
  ul.innerHTML = "";
  for (const t of items) {
    const li = document.createElement("li");
    li.className = "ticket";
    li.innerHTML =
      '<div class="ticket-top">' +
      '<span class="num">#' + t.id + "</span>" +
      '<span class="badge status-' + t.status + '">' + STATUS_RU[t.status] + "</span>" +
      '<span class="badge prio-' + t.priority + '">' + PRIORITY_RU[t.priority] + "</span>" +
      "</div>" +
      '<div class="ticket-desc">' + escapeHtml(t.description.slice(0, 120)) + "</div>" +
      '<div class="ticket-meta">Заявка: ' + escapeHtml(t.request_id) + " · " +
      escapeHtml(t.author_name) + (t.assignee_name ? " → " + escapeHtml(t.assignee_name) : "") + "</div>";
    li.addEventListener("click", () => openCard(t.id));
    ul.appendChild(li);
  }
  if (!items.length) ul.innerHTML = '<li class="empty">Нет обращений</li>';
}

async function openCard(id) {
  const t = await api("/api/tickets/" + id);
  document.getElementById("view-list").hidden = true;
  document.getElementById("view-card").hidden = false;
  renderCard(t);
}

function renderCard(t) {
  const el = document.getElementById("card");
  el.innerHTML =
    '<div class="card">' +
    '<div class="ticket-top">' +
    '<span class="num">#' + t.id + "</span>" +
    '<span class="badge status-' + t.status + '">' + STATUS_RU[t.status] + "</span>" +
    '<span class="badge prio-' + t.priority + '">' + PRIORITY_RU[t.priority] + "</span>" +
    "</div>" +
    '<p class="desc">' + escapeHtml(t.description) + "</p>" +
    "<dl>" +
    "<dt>Приоритет</dt><dd>" + PRIORITY_RU[t.priority] + "</dd>" +
    "<dt>Id заявки</dt><dd>" + escapeHtml(t.request_id) + "</dd>" +
    "<dt>Id арендатора</dt><dd>" + (t.tenant_id ? escapeHtml(t.tenant_id) : "—") + "</dd>" +
    "<dt>Автор</dt><dd>" + escapeHtml(t.author_name) + "</dd>" +
    "<dt>Исполнитель</dt><dd>" + (t.assignee_name ? escapeHtml(t.assignee_name) : "—") + "</dd>" +
    "<dt>Создано</dt><dd>" + new Date(t.created_at).toLocaleString("ru") + "</dd>" +
    "</dl>" +
    '<div id="admin-actions"></div>' +
    "</div>";
  if (me.role === "admin") renderAdminActions(t);
}

function onApiError(e, t) {
  if (e.status === 409) {
    window.alert("Обращение изменилось");
    openCard(t.id);
  } else {
    window.alert(e.message);
  }
}

function renderAdminActions(t) {
  const box = document.getElementById("admin-actions");
  box.innerHTML = "";
  const mk = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", fn);
    box.appendChild(b);
  };
  if (FINAL.includes(t.status)) {
    mk("Переоткрыть", async () => {
      try {
        await api("/api/tickets/" + t.id + "/reopen", { method: "POST", body: { expected_version: t.version } });
        openCard(t.id);
      } catch (e) { onApiError(e, t); }
    });
  }
  if (ACTIVE.includes(t.status)) {
    mk("Переназначить", async () => {
      let users;
      try { users = await api("/api/users"); } catch (e) { window.alert(e.message); return; }
      users = users.filter((u) => ["developer", "admin"].includes(u.role));
      if (!users.length) { window.alert("Нет пользователей с ролью developer/admin"); return; }
      const pick = window.prompt(
        "Переназначить на:\n" + users.map((u, i) => (i + 1) + ". " + (u.first_name || "") + " (" + u.id + ")").join("\n"),
        "1"
      );
      if (pick === null) return;
      const target = users[parseInt(pick, 10) - 1];
      if (!target) return;
      try {
        await api("/api/tickets/" + t.id + "/reassign", {
          method: "POST",
          body: { user_id: target.id, expected_version: t.version },
        });
        openCard(t.id);
      } catch (e) { onApiError(e, t); }
    });
  }
  mk("Восстановить пост", async () => {
    try {
      await api("/api/tickets/" + t.id + "/restore-post", { method: "POST" });
      window.alert("Пост восстановлен");
    } catch (e) { window.alert(e.message); }
  });
}

async function loadUsers() {
  let users;
  try { users = await api("/api/users"); } catch (e) { window.alert(e.message); return; }
  const tbody = document.querySelector("#users-table tbody");
  tbody.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td>" + escapeHtml(u.first_name || "") + " (" + u.telegram_id + ")</td>" +
      "<td>" + (u.username ? "@" + escapeHtml(u.username) : "—") + "</td>";
    const sel = document.createElement("select");
    for (const r of [null, "support", "developer", "admin"]) {
      const opt = document.createElement("option");
      opt.value = r === null ? "" : r;
      opt.textContent = r === null ? "без роли" : r;
      opt.selected = (u.role || null) === r;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", async () => {
      try {
        await api("/api/users/" + u.id + "/role", { method: "PUT", body: { role: sel.value || null } });
      } catch (e) {
        if (e.status === 409 && e.detail && e.detail.tickets) {
          window.alert("У пользователя активные обращения: " + e.detail.tickets.join(", "));
        } else {
          window.alert(e.message);
        }
        loadUsers();
      }
    });
    tr.appendChild(sel);
    tbody.appendChild(tr);
  }
}

document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => switchTab(b.dataset.tab))
);
document.getElementById("f-status").addEventListener("change", (e) => {
  state.status = e.target.value; state.page = 1; loadList();
});
document.getElementById("f-priority").addEventListener("change", (e) => {
  state.priority = e.target.value; state.page = 1; loadList();
});
document.getElementById("prev").addEventListener("click", () => { state.page -= 1; loadList(); });
document.getElementById("next").addEventListener("click", () => { state.page += 1; loadList(); });
document.getElementById("back").addEventListener("click", () => {
  document.getElementById("view-card").hidden = true;
  document.getElementById("view-list").hidden = false;
});

loadMe().catch((e) => {
  document.body.innerHTML = "<p>Ошибка: " + escapeHtml(e.message) + "</p>";
});
