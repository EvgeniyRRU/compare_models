/* Mini App logic: tabs by role, filters + pagination, card view, admin actions, roles. */
/* global Telegram, API */
(function () {
  const $ = (id) => document.getElementById(id);
  const webApp = API.webApp;
  const STATUS_RU = {
    new: "Новое",
    in_progress: "В работе",
    waiting_info: "Ожидает информации",
    resolved: "Решено",
    rejected: "Отклонено",
    duplicate: "Дубль",
  };
  const PRIORITY_RU = { critical: "Критический", high: "Высокий", medium: "Средний", low: "Низкий" };
  const ROLE_RU = { developer: "Разработчик", admin: "Админ", support: "Саппорт", null: "Нет роли" };
  const CLOSED = ["resolved", "rejected", "duplicate"];
  const PAGE = 20;

  const state = { me: null, scope: "mine", offset: 0, total: 0, card: null };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
    return d.toLocaleString();
  }

  function fmtUser(u) {
    if (!u) return "—";
    return u.username ? "@" + u.username : u.full_name || String(u.telegram_id);
  }

  let toastTimer = null;
  function toast(text, isError) {
    const el = $("toast");
    el.textContent = text;
    el.className = "toast" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3500);
  }

  function applyError(err) {
    toast(err.message, true);
    if (err.status === 401) {
      // Stale/expired initData: reopening the app fetches a fresh one.
      webApp.ready();
    }
  }

  function applyTheme() {
    document.documentElement.dataset.theme = webApp.colorScheme || "light";
    const p = webApp.themeParams || {};
    const map = {
      "--bg": p.bg_color,
      "--fg": p.text_color,
      "--muted": p.hint_color,
      "--card": p.secondary_bg_color,
      "--accent": p.button_color,
    };
    for (const [v, color] of Object.entries(map)) {
      if (color) document.documentElement.style.setProperty(v, color);
    }
    if (p.button_text_color) {
      document.querySelectorAll(".btn:not(.secondary)").forEach((b) => (b.style.color = p.button_text_color));
    }
  }

  function show(screen) {
    for (const id of ["list-screen", "card-screen", "roles-screen"]) {
      $(id).classList.toggle("hidden", id !== screen);
    }
  }

  function buildTabs() {
    const tabs = [];
    if (state.me.role === "admin") {
      tabs.push(["Мои созданные", "mine_created"], ["Мои назначенные", "mine_assigned"], ["Все", "all"], ["Роли", "roles"]);
    } else {
      tabs.push(["Мои", "mine"], ["Все", "all"]);
    }
    const nav = $("tabs");
    nav.innerHTML = "";
    for (const [label, key] of tabs) {
      const b = document.createElement("button");
      b.textContent = label;
      b.dataset.key = key;
      b.onclick = () => selectTab(key, b);
      nav.appendChild(b);
    }
    selectTab(tabs[0][1], nav.children[0]);
  }

  function selectTab(key, btn) {
    for (const el of $("tabs").children) el.classList.toggle("active", el === btn);
    if (key === "roles") {
      show("roles-screen");
      loadRoles();
      return;
    }
    state.scope = key;
    show("list-screen");
    reloadList();
  }

  function listParams(extra) {
    const p = { scope: state.scope, status: $("filter-status").value, priority: $("filter-priority").value };
    return Object.assign(p, extra || {});
  }

  async function reloadList() {
    state.offset = 0;
    $("ticket-list").innerHTML = "";
    await fetchPage();
  }

  async function fetchPage() {
    try {
      const data = await API.listTickets(listParams({ limit: PAGE, offset: state.offset }));
      state.total = data.total;
      appendTickets(data.items);
    } catch (err) {
      applyError(err);
    }
  }

  function appendTickets(items) {
    state.offset += items.length;
    const list = $("ticket-list");
    for (const t of items) {
      const b = document.createElement("button");
      b.className = "card";
      b.innerHTML =
        '<div class="head"><span>№ ' + esc(t.id) + '</span><span class="badge ' + esc(t.status) + '">' +
        esc(STATUS_RU[t.status]) + '</span></div>' +
        '<div class="meta">' + esc(PRIORITY_RU[t.priority]) + " · " + esc(t.request_id || "—") +
        " · " + fmtDate(t.created_at) + "</div>" +
        '<div class="meta">' + "Автор: " + esc(fmtUser(t.author)) + " · Исполнитель: " + esc(fmtUser(t.assignee)) + "</div>" +
        '<div class="desc">' + esc(t.description.slice(0, 200)) + (t.description.length > 200 ? "…" : "") + "</div>";
      b.onclick = () => openCard(t.id);
      list.appendChild(b);
    }
    $("load-more").classList.toggle("hidden", state.offset >= state.total);
  }

  async function openCard(id) {
    try {
      renderCard(await API.ticket(id));
      show("card-screen");
    } catch (err) {
      applyError(err);
    }
  }

  function renderCard(t) {
    state.card = t;
    const rows = [
      ["Статус", STATUS_RU[t.status]],
      ["Приоритет", PRIORITY_RU[t.priority]],
      ["Заявка", t.request_id || "—"],
      ["Арендатор", t.tenant_id || "—"],
      ["Автор", fmtUser(t.author)],
      ["Исполнитель", fmtUser(t.assignee)],
      ["Создано", fmtDate(t.created_at)],
      ["Взято", fmtDate(t.taken_at)],
      ["Закрыто", fmtDate(t.closed_at)],
      ["Версия", t.version],
    ];
    let html = '<div class="card"><div class="head"><span>№ ' + esc(t.id) + "</span>" +
      '<span class="badge ' + esc(t.status) + '">' + esc(STATUS_RU[t.status]) + "</span></div>" +
      '<div class="desc">' + esc(t.description) + "</div>";
    for (const [k, v] of rows) {
      html += '<div class="field"><span class="k">' + esc(k) + "</span><span>" + esc(v) + "</span></div>";
    }
    html += '<div class="actions" id="card-actions"></div></div>';
    $("ticket-card").innerHTML = html;

    const actions = $("card-actions");
    if (state.me.role === "admin") {
      if (CLOSED.includes(t.status)) addBtn(actions, "Переоткрыть", () => doAction(() => API.reopen(t.id, t.version)));
      if (t.status === "in_progress" || t.status === "waiting_info") addReassign(actions, t);
      addBtn(actions, "Восстановить пост", () => doAction(() => API.restorePost(t.id)), "secondary");
    }
  }

  function addBtn(container, label, fn, cls) {
    const b = document.createElement("button");
    b.className = "btn" + (cls ? " " + cls : "");
    b.textContent = label;
    b.onclick = fn;
    container.appendChild(b);
    return b;
  }

  async function addReassign(container, t) {
    const select = document.createElement("select");
    select.className = "role";
    try {
      const data = await API.users("", true);
      for (const u of data.items) {
        const o = document.createElement("option");
        o.value = u.telegram_id;
        o.textContent = fmtUser(u) + " (" + ROLE_RU[u.role] + ")";
        select.appendChild(o);
      }
    } catch (err) {
      applyError(err);
      return;
    }
    container.appendChild(select);
    addBtn(container, "Переназначить", () =>
      doAction(() => API.reassign(t.id, t.version, Number(select.value)))
    );
  }

  async function doAction(fn) {
    try {
      const card = await fn();
      toast("Готово");
      renderCard(card);
    } catch (err) {
      applyError(err);
      if (err.status === 409 || err.status === 422) {
        // Someone changed the ticket or the action is now invalid: refresh.
        openCard(state.card.id);
      }
    }
  }

  async function loadRoles() {
    try {
      const data = await API.users($("roles-search").value.trim() || "", false);
      const list = $("roles-list");
      list.innerHTML = "";
      for (const u of data.items) {
        const div = document.createElement("div");
        div.className = "card";
        div.innerHTML = '<div class="head"><span>' + esc(fmtUser(u)) + "</span><span>" +
          esc(String(u.telegram_id)) + "</span></div>";
        const select = document.createElement("select");
        select.className = "role";
        for (const role of ["developer", "admin", "support", ""]) {
          const o = document.createElement("option");
          o.value = role;
          o.textContent = role ? ROLE_RU[role] : ROLE_RU.null;
          if ((u.role || "") === role) o.selected = true;
          select.appendChild(o);
        }
        select.onchange = async () => {
          try {
            await API.setRole(u.telegram_id, select.value || null);
            toast("Роль обновлена");
          } catch (err) {
            const ids = err.payload && err.payload.active_ticket_ids;
            applyError(new Error(err.message + (ids ? " (№ " + ids.join(", ") + ")" : "")));
            loadRoles();
          }
        };
        div.appendChild(select);
        list.appendChild(div);
      }
    } catch (err) {
      applyError(err);
    }
  }

  let rolesDebounce = null;
  function wire() {
    $("filter-status").onchange = reloadList;
    $("filter-priority").onchange = reloadList;
    $("load-more").onclick = fetchPage;
    $("back-to-list").onclick = () => {
      show("list-screen");
      reloadList();
    };
    $("roles-search").oninput = () => {
      clearTimeout(rolesDebounce);
      rolesDebounce = setTimeout(loadRoles, 300);
    };
  }

  async function init() {
    webApp.ready();
    webApp.expand();
    applyTheme();
    wire();
    try {
      state.me = await API.me();
    } catch (err) {
      applyError(err);
      $("ticket-list").innerHTML = '<div class="card">Нет доступа. Попросите администратора выдать роль и откройте приложение заново.</div>';
      return;
    }
    buildTabs();
  }

  init();
})();
