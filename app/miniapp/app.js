const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const STATUS_LABELS = {
  new: 'Новый',
  in_progress: 'В работе',
  waiting_info: 'Ожидание информации',
  resolved: 'Решено',
  rejected: 'Отклонено',
  duplicate: 'Дубль',
};
const PRIORITY_LABELS = {
  critical: 'Критический', high: 'Высокий', medium: 'Средний', low: 'Низкий',
};
const ROLE_LABELS = {
  support: 'Первая линия', developer: 'Разработчик', admin: 'Администратор',
};

let mine = false;
let currentUserRole = null;
let page = 1;

function headers() {
  return {
    Authorization: `tma ${tg?.initData || ''}`,
    'Content-Type': 'application/json',
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(), ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail?.message || data.detail || 'Ошибка запроса');
  }
  return data;
}

function showError(error) {
  tg?.showAlert(error.message);
}

function field(label, value) {
  const paragraph = document.createElement('p');
  paragraph.textContent = `${label}: ${value ?? '—'}`;
  return paragraph;
}

async function loadTickets() {
  const host = document.querySelector('#tickets');
  const detail = document.querySelector('#detail');
  detail.hidden = true;
  host.hidden = false;
  if (!tg?.initData) {
    host.textContent = 'Откройте Mini App через Telegram.';
    return;
  }

  try {
    const query = new URLSearchParams({ mine: String(mine), page: String(page), page_size: '50' });
    const status = document.querySelector('#status').value;
    const priority = document.querySelector('#priority').value;
    if (status) query.set('status', status);
    if (priority) query.set('priority', priority);

    const tickets = await api(`/api/tickets?${query}`);
    host.replaceChildren();
    for (const ticket of tickets) {
      const card = document.createElement('article');
      const open = document.createElement('button');
      card.append(
        field(`#${ticket.id} · ${STATUS_LABELS[ticket.status]}`, PRIORITY_LABELS[ticket.priority]),
        field('Заявка', ticket.request_id),
      );
      const summary = document.createElement('p');
      summary.textContent = ticket.description.slice(0, 240);
      card.append(summary);
      open.textContent = 'Открыть';
      open.onclick = () => showTicket(ticket.id);
      card.append(open);
      host.append(card);
    }

    const pagination = document.querySelector('#pagination');
    pagination.hidden = tickets.length < 50 && page === 1;
    document.querySelector('#page').textContent = String(page);
    document.querySelector('#previous').disabled = page === 1;
    document.querySelector('#next').disabled = tickets.length < 50;

    const me = await api('/api/me');
    currentUserRole = me.role;
    document.querySelector('#roles').hidden = me.role !== 'admin';
  } catch (error) {
    host.textContent = error.message;
  }
}

async function showTicket(id) {
  try {
    const ticket = await api(`/api/tickets/${id}`);
    const box = document.querySelector('#detail');
    document.querySelector('#tickets').hidden = true;
    box.hidden = false;
    box.replaceChildren();
    const back = document.createElement('button');
    back.textContent = 'Назад';
    back.onclick = loadTickets;
    box.append(back);

    for (const [label, value] of Object.entries({
      Обращение: `#${ticket.id}`,
      Описание: ticket.description,
      Статус: STATUS_LABELS[ticket.status],
      Приоритет: PRIORITY_LABELS[ticket.priority],
      'ID заявки': ticket.request_id,
      'ID арендатора': ticket.tenant_id,
      Автор: ticket.author_name,
      Исполнитель: ticket.assignee_name,
      Создано: ticket.created_at,
      Обновлено: ticket.updated_at,
      Взято: ticket.taken_at,
      Закрыто: ticket.closed_at,
      'Следующее напоминание': ticket.next_reminder_at,
    })) {
      box.append(field(label, value));
    }

    if (currentUserRole === 'admin') {
      if (['resolved', 'rejected', 'duplicate'].includes(ticket.status)) {
        addAction(box, ticket, 'reopen', 'Переоткрыть');
      }
      if (['in_progress', 'waiting_info'].includes(ticket.status)) {
        const users = await api('/api/users');
        const select = document.createElement('select');
        for (const user of users.filter((item) => ['developer', 'admin'].includes(item.role))) {
          const option = document.createElement('option');
          option.value = user.id;
          option.textContent = `${user.name} (${ROLE_LABELS[user.role]})`;
          select.append(option);
        }
        const reassign = document.createElement('button');
        reassign.textContent = 'Переназначить';
        reassign.onclick = () => performAction(ticket, 'reassign', Number(select.value));
        box.append(select, reassign);
      }
      const restore = document.createElement('button');
      restore.textContent = 'Восстановить пост';
      restore.onclick = async () => {
        try {
          await api(`/api/tickets/${id}/restore-post`, { method: 'POST' });
          tg.showAlert('Пост восстановлен');
        } catch (error) {
          showError(error);
        }
      };
      box.append(restore);
    }
  } catch (error) {
    showError(error);
  }
}

function addAction(parent, ticket, action, label) {
  const button = document.createElement('button');
  button.textContent = label;
  button.onclick = () => performAction(ticket, action);
  parent.append(button);
}

async function performAction(ticket, action, assignee_id) {
  try {
    await api(`/api/tickets/${ticket.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        version: ticket.version, status: ticket.status, action, assignee_id,
      }),
    });
    await showTicket(ticket.id);
  } catch (error) {
    showError(error);
  }
}

async function manageRoles() {
  try {
    const users = await api('/api/users');
    const host = document.querySelector('#tickets');
    host.replaceChildren();
    document.querySelector('#detail').hidden = true;
    host.hidden = false;
    document.querySelector('#pagination').hidden = true;
    for (const user of users) {
      const row = document.createElement('article');
      const name = document.createElement('span');
      const select = document.createElement('select');
      const save = document.createElement('button');
      name.textContent = `${user.name} · ${user.telegram_id} `;
      for (const [value, label] of [
        ['', 'Без роли'], ['support', ROLE_LABELS.support],
        ['developer', ROLE_LABELS.developer], ['admin', ROLE_LABELS.admin],
      ]) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        option.selected = (user.role || '') === value;
        select.append(option);
      }
      save.textContent = 'Сохранить';
      save.onclick = async () => {
        try {
          await api(`/api/users/${user.id}/role`, {
            method: 'PUT', body: JSON.stringify({ role: select.value || null }),
          });
          tg.showAlert('Роль обновлена');
        } catch (error) {
          showError(error);
        }
      };
      row.append(name, select, save);
      host.append(row);
    }
  } catch (error) {
    showError(error);
  }
}

document.querySelector('#mine').onclick = () => { mine = true; page = 1; loadTickets(); };
document.querySelector('#all').onclick = () => { mine = false; page = 1; loadTickets(); };
document.querySelector('#apply').onclick = () => { page = 1; loadTickets(); };
document.querySelector('#previous').onclick = () => { page = Math.max(1, page - 1); loadTickets(); };
document.querySelector('#next').onclick = () => { page += 1; loadTickets(); };
document.querySelector('#roles').onclick = manageRoles;
loadTickets();
