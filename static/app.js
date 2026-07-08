/* ===============================================================
   NEURO.CORE dashboard — vanilla JS
   =============================================================== */

const API = {
  base: '',
  token: localStorage.getItem('dashboard_token') || '',
  setToken(t) {
    this.token = t || '';
    if (t) localStorage.setItem('dashboard_token', t);
    else   localStorage.removeItem('dashboard_token');
  },
  async req(method, path, body) {
    const headers = { 'Content-Type': 'application/json' };
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    const opts = { method, headers };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(this.base + path, opts);
    if (r.status === 401) {
      // протух — заставляем перелогиниться
      Login.show('Сессия не авторизована. Введите токен заново.');
      throw new Error('Unauthorized');
    }
    const text = await r.text();
    let data;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) {
      const msg = (data && data.detail) ? data.detail : `HTTP ${r.status}`;
      throw new Error(msg);
    }
    return data;
  },
  get(p)         { return this.req('GET',    p); },
  post(p, b)     { return this.req('POST',   p, b); },
  put(p, b)      { return this.req('PUT',    p, b); },
  del(p)         { return this.req('DELETE', p); },
  async upload(path, formData) {
    const headers = {};
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    const r = await fetch(this.base + path, { method: 'POST', headers, body: formData });
    if (r.status === 401) { Login.show(); throw new Error('Unauthorized'); }
    const data = await r.json().catch(() => null);
    if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
    return data;
  },
};

// ============ TOAST ============
const toastEl = document.getElementById('toast');
function toast(msg, kind = '') {
  toastEl.className = 'toast';
  toastEl.classList.add('show');
  if (kind) toastEl.classList.add(`toast-${kind}`);
  toastEl.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toastEl.classList.remove('show'), 3200);
}

// ============ LOGIN ============
const Login = {
  el: document.getElementById('login-overlay'),
  form: document.getElementById('login-form'),
  input: document.getElementById('login-token'),
  error: document.getElementById('login-error'),
  show(msg = '') {
    this.error.textContent = msg;
    this.el.classList.remove('hidden');
  },
  hide() { this.el.classList.add('hidden'); },
  async check() {
    let status;
    try {
      const r = await fetch('/auth-status');
      status = await r.json();
    } catch {
      status = { auth_required: false };
    }
    if (!status.auth_required) {
      this.hide();
      return true;
    }
    if (API.token) {
      // пробуем
      try {
        await API.get('/health');
        this.hide();
        return true;
      } catch {
        API.setToken('');
      }
    }
    this.show();
    return false;
  },
};
Login.form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const t = Login.input.value.trim();
  if (!t) return;
  API.setToken(t);
  try {
    await API.get('/health');
    Login.hide();
    boot();
  } catch (err) {
    Login.show('Неверный токен');
    API.setToken('');
  }
});

// ============ TABS ============
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const id = 'tab-' + btn.dataset.tab;
    document.getElementById(id).classList.add('active');
    if (btn.dataset.tab === 'channels') Channels.refresh();
    if (btn.dataset.tab === 'logs') Logs.refresh();
    if (btn.dataset.tab === 'accounts') Accounts.refresh();
    if (btn.dataset.tab === 'proxies') Proxies.refresh();
  });
});

// ============ ACCOUNTS ============
const Accounts = {
  data: [],
  proxies: [],
  async refresh() {
    const grid = document.getElementById('accounts-grid');
    try {
      this.data = await API.get('/accounts');
    } catch (e) {
      grid.innerHTML = `<div class="empty">Ошибка: ${escape(e.message)}</div>`;
      return;
    }
    try { this.proxies = await API.get('/proxies'); } catch { this.proxies = []; }
    document.getElementById('m-accounts').textContent = this.data.length;
    document.getElementById('m-running').textContent =
      this.data.filter(a => a.is_running).length;

    if (!this.data.length) {
      grid.innerHTML = `<div class="empty">Нет аккаунтов. Импортируйте .session-файл.</div>`;
      return;
    }
    grid.innerHTML = this.data.map(a => this.renderCard(a)).join('');
    grid.querySelectorAll('[data-act]').forEach(b => {
      b.addEventListener('click', () => this.action(b.dataset.act, +b.dataset.id));
    });
    grid.querySelectorAll('[data-proxy-for]').forEach(sel => {
      sel.addEventListener('change', () => this.assignProxy(+sel.dataset.proxyFor, sel.value));
    });
  },
  proxyOptions(currentId) {
    const opts = ['<option value="">Без прокси</option>'];
    for (const p of this.proxies) {
      const sel = String(p.id) === String(currentId) ? ' selected' : '';
      const label = `${(p.type || 'http').toUpperCase()} ${p.ip}:${p.port}`;
      opts.push(`<option value="${p.id}"${sel}>${escape(label)}</option>`);
    }
    return opts.join('');
  },
  async assignProxy(accId, proxyId) {
    try {
      await API.post(`/accounts/${accId}/proxy`, { proxy_id: proxyId ? Number(proxyId) : null });
      toast(proxyId ? 'Прокси привязан' : 'Прокси отвязан', 'ok');
    } catch (e) {
      toast(e.message, 'error');
      this.refresh();
    }
  },
  renderCard(a) {
    const status = a.is_running ? 'running' : (a.status || 'stopped');
    const statusLabel = a.is_running ? 'running' : (a.status || 'stopped');
    const stats = a.stats || {};
    const comments = stats.total_comments_sent || stats.comments_sent || 0;
    const banned = a.banned_channels_count || 0;
    return `
      <div class="account-card ${a.is_running ? 'running' : ''}">
        <div class="account-card-head">
          <div>
            <div class="account-phone">${escape(a.phone || '—')}</div>
            <div class="muted small">${escape(a.session_name || '')}</div>
          </div>
          <span class="account-status-dot ${escape(status)}" title="${escape(statusLabel)}"></span>
        </div>
        <div class="account-meta">
          <span>Комментариев</span><span>${comments}</span>
        </div>
        <div class="account-meta">
          <span>Забанен в каналах</span><span>${banned}</span>
        </div>
        <div class="account-meta">
          <span>Health</span><span>${escape(a.health_status || '—')}</span>
        </div>
        <div class="account-proxy">
          <span class="muted">Прокси</span>
          <select data-proxy-for="${a.id}">${this.proxyOptions(a.proxy_id)}</select>
        </div>
        <div class="account-actions">
          ${a.is_running
            ? `<button class="btn btn-ghost btn-sm" data-act="stop"   data-id="${a.id}">Стоп</button>`
            : `<button class="btn btn-primary btn-sm" data-act="start" data-id="${a.id}">Старт</button>`}
          <button class="btn btn-ghost btn-sm" data-act="profile" data-id="${a.id}">Профиль</button>
          <button class="btn btn-ghost btn-sm" data-act="reset-bans" data-id="${a.id}">Сбросить баны</button>
          <button class="btn btn-danger btn-sm" data-act="delete"    data-id="${a.id}">Удалить</button>
        </div>
      </div>`;
  },
  async action(act, id) {
    try {
      if (act === 'start') {
        await API.post(`/accounts/${id}/start`);
        toast('Воркер запущен', 'ok');
      } else if (act === 'stop') {
        await API.post(`/accounts/${id}/stop`);
        toast('Воркер остановлен', 'ok');
      } else if (act === 'delete') {
        if (!confirm('Удалить аккаунт?')) return;
        await API.del(`/accounts/${id}`);
        toast('Удалён', 'ok');
      } else if (act === 'reset-bans') {
        await API.post(`/accounts/${id}/reset-bans`);
        toast('Баны сброшены', 'ok');
      } else if (act === 'profile') {
        const acc = this.data.find(x => x.id === id);
        if (!acc || !acc.is_running) {
          toast('Запустите аккаунт, чтобы редактировать профиль', 'error');
          return;
        }
        Profile.open(id, acc);
        return;
      }
      this.refresh();
    } catch (e) {
      toast(e.message, 'error');
    }
  },
};
document.getElementById('btn-refresh-accounts').addEventListener('click', () => Accounts.refresh());

// ============ PROFILE EDITING ============
const Profile = {
  id: null,
  newAvatar: null, // base64 data URL, если пользователь выбрал новую
  modal: document.getElementById('modal-profile'),
  form: document.getElementById('form-profile'),
  loading: document.getElementById('profile-loading'),

  async open(id, acc) {
    this.id = id;
    this.newAvatar = null;
    this.form.classList.add('hidden');
    this.loading.classList.remove('hidden');
    this.loading.textContent = 'Загрузка профиля…';
    this.modal.classList.remove('hidden');

    // Базовые данные из карточки
    document.getElementById('profile-phone').textContent = acc.phone || '—';
    const badge = document.getElementById('profile-status');
    badge.textContent = 'активен';
    badge.className = 'profile-status-badge running';

    try {
      const p = await API.get(`/accounts/${id}/profile`);
      this.form.first_name.value = p.first_name || '';
      this.form.last_name.value = p.last_name || '';
      this.form.username.value = p.username || '';
      this.form.bio.value = p.bio || '';
      if (p.phone) document.getElementById('profile-phone').textContent = '+' + String(p.phone).replace(/^\+/, '');
      this.setAvatar(p.avatar, p.first_name || p.username || '?');
      this.form.classList.remove('hidden');
      this.loading.classList.add('hidden');
    } catch (e) {
      this.loading.textContent = 'Не удалось получить профиль: ' + e.message;
    }
  },

  setAvatar(src, nameForFallback) {
    const img = document.getElementById('profile-avatar-img');
    const fb = document.getElementById('profile-avatar-fallback');
    if (src) {
      img.src = src;
      img.style.display = 'block';
      fb.style.display = 'none';
    } else {
      img.style.display = 'none';
      fb.style.display = 'grid';
      fb.textContent = (nameForFallback || '?').trim().charAt(0).toUpperCase() || '?';
    }
  },

  close() { this.modal.classList.add('hidden'); },

  async save(e) {
    e.preventDefault();
    const payload = {
      first_name: this.form.first_name.value.trim(),
      last_name: this.form.last_name.value.trim(),
      username: this.form.username.value.trim().replace(/^@/, ''),
      bio: this.form.bio.value.trim(),
    };
    if (this.newAvatar) payload.avatar_base64 = this.newAvatar;
    const btn = this.form.querySelector('button[type="submit"]');
    btn.disabled = true;
    btn.textContent = 'Сохранение…';
    try {
      await API.put(`/accounts/${this.id}/profile`, payload);
      toast('Профиль обновлён в Telegram', 'ok');
      this.close();
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Сохранить в Telegram';
    }
  },
};
Profile.modal.querySelector('.modal-close').addEventListener('click', () => Profile.close());
Profile.modal.addEventListener('click', (e) => { if (e.target === Profile.modal) Profile.close(); });
Profile.form.addEventListener('submit', (e) => Profile.save(e));
document.getElementById('profile-avatar-input').addEventListener('change', (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) { toast('Файл больше 5 МБ', 'error'); return; }
  const reader = new FileReader();
  reader.onload = () => {
    Profile.newAvatar = reader.result;
    Profile.setAvatar(reader.result, '');
  };
  reader.readAsDataURL(file);
});

// Add account modal
const modal = document.getElementById('modal-add-account');
document.getElementById('btn-add-account').addEventListener('click', () => modal.classList.remove('hidden'));
modal.querySelector('.modal-close').addEventListener('click', () => modal.classList.add('hidden'));
modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.add('hidden'); });

// Если API ID/Hash заданы глоба��ьно в 1.envv — прячем ручные поля, показываем подсказку
(async () => {
  try {
    const cfg = await API.get('/config-status');
    if (cfg && cfg.api_configured) {
      document.getElementById('api-fields')?.classList.add('hidden');
      document.getElementById('api-fields-note')?.classList.remove('hidden');
    }
  } catch (_) { /* необязательно */ }
})();

document.getElementById('form-import-session').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  // Убираем пустые API-поля, чтобы сработал fallback на 1.envv
  if (!fd.get('api_id')) fd.delete('api_id');
  if (!(fd.get('api_hash') || '').trim()) fd.delete('api_hash');
  const fileCount = (fd.getAll('files') || []).filter((f) => f && f.name).length;
  if (!fileCount) { toast('Выберите хотя бы один .session файл', 'error'); return; }
  try {
    const res = await API.upload('/import-sessions-bulk', fd);
    const ok = res.success || 0;
    const skipped = (res.results || []).filter((r) => r.status === 'skip').length;
    const failed = (res.results || []).filter((r) => r.status === 'error').length;
    let msg = `Импортировано: ${ok} из ${res.total || fileCount}`;
    if (skipped) msg += `, пропущено: ${skipped}`;
    if (failed) msg += `, ошибок: ${failed}`;
    toast(msg, failed && !ok ? 'error' : 'ok');
    if (ok) {
      modal.classList.add('hidden');
      e.target.reset();
      Accounts.refresh();
    }
  } catch (err) {
    toast(err.message, 'error');
  }
});

// ============ SETTINGS / TOGGLES (общая логика) ============
const Settings = {
  current: {},
  async load() {
    try {
      this.current = await API.get('/settings');
    } catch (e) {
      toast(`Не удалось загрузить настройки: ${e.message}`, 'error');
      return;
    }
    // распихиваем значения по всем элементам [data-setting]
    for (const el of document.querySelectorAll('[data-setting]')) {
      const k = el.dataset.setting;
      if (!(k in this.current)) continue;
      const v = this.current[k];
      if (el.type === 'checkbox') el.checked = !!v;
      else if (el.tagName === 'SELECT' || el.type === 'text' || el.type === 'password' ||
               el.type === 'number' || el.tagName === 'TEXTAREA') {
        el.value = v == null ? '' : v;
      }
    }
    // Глобальная пауза в топбаре
    document.getElementById('global-pause-toggle').checked = !!this.current.global_pause;
    document.getElementById('m-pause').textContent = this.current.global_pause ? 'ВКЛ' : 'выкл';
    document.getElementById('ch-watcher-int').textContent = this.current.channel_watcher_interval_minutes || 30;
  },
  async save(diff) {
    try {
      await API.post('/settings', diff);
      this.current = { ...this.current, ...diff };
      const hint = document.getElementById('behavior-saved-hint');
      if (hint) {
        hint.textContent = '✓ Сохранено';
        clearTimeout(this._h);
        this._h = setTimeout(() => hint.textContent = '', 1800);
      }
    } catch (e) {
      toast(`Сохранение: ${e.message}`, 'error');
    }
  },
};

// чекбоксы → автосейв при изменении
document.addEventListener('change', async (e) => {
  const el = e.target;
  if (!el.matches('[data-setting]')) return;
  if (el.type !== 'checkbox') return;
  const k = el.dataset.setting;
  await Settings.save({ [k]: el.checked });
});

// глобальная пауза
document.getElementById('global-pause-toggle').addEventListener('change', async (e) => {
  await Settings.save({ global_pause: e.target.checked });
  document.getElementById('m-pause').textContent = e.target.checked ? 'ВКЛ' : 'выкл';
});

// кнопка Сохранить (для текстовых полей и select'ов)
document.getElementById('btn-save-settings').addEventListener('click', async () => {
  const diff = {};
  for (const el of document.querySelectorAll('#tab-settings [data-setting]')) {
    const k = el.dataset.setting;
    if (el.type === 'checkbox') diff[k] = el.checked;
    else if (el.type === 'number') diff[k] = el.value === '' ? null : Number(el.value);
    else diff[k] = el.value;
  }
  // фильтруем null
  for (const k in diff) if (diff[k] === null || diff[k] === undefined) delete diff[k];
  await Settings.save(diff);
  toast('Настройки сохранены', 'ok');
});

// тест нейросети
document.getElementById('btn-test-ai').addEventListener('click', async () => {
  const out = document.getElementById('settings-test-result');
  out.innerHTML = '<span class="muted">Проверяю…</span>';
  try {
    const r = await API.post('/settings/test-ai');
    if (r.status === 'ok') out.innerHTML = `<span class="pill good">${escape(r.message)}</span> <span class="muted small">${escape(r.reply || '')}</span>`;
    else                   out.innerHTML = `<span class="pill bad">${escape(r.message)}</span>`;
  } catch (e) {
    out.innerHTML = `<span class="pill bad">Ошибка: ${escape(e.message)}</span>`;
  }
});

// ============ CHANNELS ============
const Channels = {
  data: [],
  async refresh() {
    const tbody = document.getElementById('channels-tbody');
    try {
      this.data = await API.get('/discovery/channels');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty">Ошибка: ${escape(e.message)}</td></tr>`;
      return;
    }
    document.getElementById('ch-total').textContent = this.data.length;
    document.getElementById('ch-open').textContent  = this.data.filter(c => c.can_comment).length;
    this.render();
  },
  render() {
    const q = (document.getElementById('channels-search').value || '').toLowerCase().trim();
    const tbody = document.getElementById('channels-tbody');
    let rows = this.data;
    if (q) rows = rows.filter(c =>
      (c.channel || '').toLowerCase().includes(q) ||
      (c.title || '').toLowerCase().includes(q));
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty">Ничего не найдено</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.slice(0, 500).map(c => `
      <tr>
        <td><a href="https://t.me/${escape((c.channel || '').replace(/^\+/,'+'))}" target="_blank" rel="noopener">@${escape(c.channel || '')}</a></td>
        <td>${escape((c.title || '').slice(0, 60))}</td>
        <td class="muted">${(c.subs_count || 0).toLocaleString('ru-RU')}</td>
        <td>${c.can_comment ? '<span class="pill good">открыты</span>' : '<span class="pill bad">закрыты</span>'}</td>
        <td class="muted small">${escape(c.source || '—')}</td>
        <td class="muted small">${escape((c.last_checked || '').slice(0, 16))}</td>
        <td>
          <button class="btn btn-ghost btn-sm" data-recheck="${escape(c.channel)}" title="Перепроверить">↻</button>
          <button class="btn btn-ghost btn-sm" data-comment="${escape(c.channel)}" title="Комментить сейчас">✎</button>
          <button class="btn btn-ghost btn-sm" data-del="${escape(c.channel)}" title="Удалить">×</button>
        </td>
      </tr>
    `).join('');
    tbody.querySelectorAll('[data-del]').forEach(b => {
      b.addEventListener('click', async () => {
        if (!confirm(`Удалить @${b.dataset.del}?`)) return;
        try {
          await API.del(`/discovery/channels/${encodeURIComponent(b.dataset.del)}`);
          this.refresh();
        } catch (e) { toast(e.message, 'error'); }
      });
    });
    tbody.querySelectorAll('[data-recheck]').forEach(b => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const r = await API.post(`/discovery/channels/${encodeURIComponent(b.dataset.recheck)}/recheck`);
          toast((r && r.message) || 'Перепроверено', 'ok');
          this.refresh();
        } catch (e) { toast(e.message, 'error'); }
        finally { b.disabled = false; }
      });
    });
    tbody.querySelectorAll('[data-comment]').forEach(b => {
      b.addEventListener('click', async () => {
        if (!confirm(`Оставить комментарий в @${b.dataset.comment} сейчас?`)) return;
        b.disabled = true;
        try {
          const r = await API.post(`/discovery/channels/${encodeURIComponent(b.dataset.comment)}/comment-now`);
          toast((r && r.message) || 'Отправлено', 'ok');
        } catch (e) { toast(e.message, 'error'); }
        finally { b.disabled = false; }
      });
    });
  },
};
document.getElementById('btn-refresh-channels').addEventListener('click', () => Channels.refresh());
document.getElementById('btn-cleanup-channels').addEventListener('click', async () => {
  if (!confirm('Удалить все каналы с закрытыми комментариями?')) return;
  try {
    await API.post('/discovery/cleanup');
    toast('Очищено', 'ok');
    Channels.refresh();
  } catch (e) { toast(e.message, 'error'); }
});
document.getElementById('channels-search').addEventListener('input', () => Channels.render());

// Ручное добавление канала
document.getElementById('form-add-channel').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('add-channel-input');
  const titleEl = document.getElementById('add-channel-title');
  const channel = (input.value || '').trim();
  if (!channel) { toast('Укажите канал', 'error'); return; }
  try {
    await API.post('/discovery/channels/add', { channel, title: (titleEl.value || '').trim() });
    toast('Канал добавлен', 'ok');
    input.value = ''; titleEl.value = '';
    Channels.refresh();
  } catch (err) { toast(err.message, 'error'); }
});

// Обслуживание базы каналов
function bindChannelMaintenance(btnId, path, confirmMsg) {
  const el = document.getElementById(btnId);
  if (!el) return;
  el.addEventListener('click', async () => {
    if (confirmMsg && !confirm(confirmMsg)) return;
    el.disabled = true;
    const label = el.textContent;
    el.textContent = 'Выполняю…';
    try {
      const r = await API.post(path);
      toast((r && r.message) || 'Готово', 'ok');
      Channels.refresh();
    } catch (e) { toast(e.message, 'error'); }
    finally { el.disabled = false; el.textContent = label; }
  });
}
bindChannelMaintenance('btn-recheck-closed', '/discovery/channels/recheck-all-closed', 'Перепроверить все каналы с закрытыми комментариями? Может занять время.');
bindChannelMaintenance('btn-join-private', '/discovery/channels/join-private', 'Отправить заявки на вступление во все приватные каналы?');
bindChannelMaintenance('btn-reset-closed', '/discovery/channels/reset-closed', 'Сбросить статус «закрытые» у каналов?');

// ============ PROXIES ============
const Proxies = {
  data: [],
  async refresh() {
    const tbody = document.getElementById('proxies-tbody');
    try {
      this.data = await API.get('/proxies');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Ошибка: ${escape(e.message)}</td></tr>`;
      return;
    }
    // считаем, сколько аккаунтов привязано к каждому прокси
    let usage = {};
    try {
      const accs = Accounts.data.length ? Accounts.data : await API.get('/accounts');
      for (const a of accs) if (a.proxy_id) usage[a.proxy_id] = (usage[a.proxy_id] || 0) + 1;
    } catch { /* необязательно */ }

    if (!this.data.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Нет прокси. Добавьте прокси через форму выше.</td></tr>`;
      return;
    }
    tbody.innerHTML = this.data.map(p => `
      <tr>
        <td><span class="pill">${escape((p.type || 'http').toUpperCase())}</span></td>
        <td class="mono">${escape(p.ip)}:${escape(String(p.port))}</td>
        <td class="muted small">${escape(p.username || '—')}</td>
        <td class="muted">${usage[p.id] || 0}</td>
        <td><button class="btn btn-danger btn-sm" data-del-proxy="${p.id}">Удалить</button></td>
      </tr>
    `).join('');
    tbody.querySelectorAll('[data-del-proxy]').forEach(b => {
      b.addEventListener('click', async () => {
        if (!confirm('Удалить прокси? Он будет отвязан от всех аккаунтов.')) return;
        try {
          await API.del(`/proxies/${b.dataset.delProxy}`);
          toast('Прокси удалён', 'ok');
          this.refresh();
        } catch (e) { toast(e.message, 'error'); }
      });
    });
  },
  readForm() {
    const f = document.getElementById('form-add-proxy');
    const port = Number(f.port.value);
    return {
      type: f.type.value,
      ip: (f.ip.value || '').trim(),
      port: Number.isFinite(port) ? port : 0,
      username: (f.username.value || '').trim() || null,
      password: (f.password.value || '').trim() || null,
    };
  },
};
document.getElementById('btn-refresh-proxies').addEventListener('click', () => Proxies.refresh());

document.getElementById('form-add-proxy').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = Proxies.readForm();
  if (!body.ip || !body.port) { toast('Укажите IP и порт', 'error'); return; }
  try {
    await API.post('/proxies', body);
    toast('Прокси добавлен', 'ok');
    e.target.reset();
    Proxies.refresh();
  } catch (err) { toast(err.message, 'error'); }
});

document.getElementById('btn-check-proxy').addEventListener('click', async () => {
  const out = document.getElementById('proxy-check-result');
  const body = Proxies.readForm();
  if (!body.ip || !body.port) { toast('Укажите IP и порт', 'error'); return; }
  out.innerHTML = '<span class="muted">Проверяю…</span>';
  try {
    const r = await API.post('/proxies/check', body);
    out.innerHTML = r.status === 'ok'
      ? `<span class="pill good">${escape(r.message)}</span>`
      : `<span class="pill bad">${escape(r.message)}</span>`;
  } catch (e) {
    out.innerHTML = `<span class="pill bad">Ошибка: ${escape(e.message)}</span>`;
  }
});

// ============ LOGS ============
const Logs = {
  _timer: null,
  async refresh() {
    const c = document.getElementById('logs-container');
    const lvl = document.getElementById('logs-level').value;
    try {
      const params = new URLSearchParams();
      if (lvl) params.set('level', lvl);
      params.set('limit', '300');
      const data = await API.get('/logs?' + params.toString());
      const list = Array.isArray(data) ? data : (data.logs || []);
      if (!list.length) { c.textContent = '— пусто —'; return; }
      c.innerHTML = list.map(l => {
        const ts = (l.timestamp || '').slice(11, 19) || '--:--:--';
        const level = (l.level || 'info').toLowerCase();
        const acc = l.account_id != null ? `acc:${l.account_id}` : 'sys';
        return `<div class="log-line">
          <span class="log-time">${escape(ts)}</span>
          <span class="log-level ${escape(level)}">${escape(level)}</span>
          <span class="log-acc">${escape(acc)}</span>
          <span class="log-msg">${escape(l.message || '')}</span>
        </div>`;
      }).join('');
    } catch (e) {
      c.textContent = `Ошибка: ${e.message}`;
    }
  },
  startAuto() {
    this.stopAuto();
    this._timer = setInterval(() => this.refresh(), 4000);
  },
  stopAuto() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  },
};
document.getElementById('btn-refresh-logs').addEventListener('click', () => Logs.refresh());
document.getElementById('logs-level').addEventListener('change', () => Logs.refresh());
document.getElementById('logs-autorefresh').addEventListener('change', (e) => {
  if (e.target.checked) Logs.startAuto(); else Logs.stopAuto();
});

// ============ HEADER STATUS ============
async function refreshStatus() {
  try {
    const h = await API.get('/health');
    const watcher = document.getElementById('status-watcher');
    watcher.classList.toggle('ok', !!h.watcher_running);
    watcher.classList.toggle('bad', !h.watcher_running);
    watcher.querySelector('.status-label').textContent =
      h.watcher_running ? 'watcher' : 'no watcher';

    const w = document.getElementById('status-workers');
    const running = h.workers_running ?? 0;
    const total = h.workers_total ?? 0;
    w.classList.toggle('ok', running > 0);
    w.querySelector('.status-label').textContent = `${running}/${total} workers`;

    const pause = document.getElementById('global-pause-toggle');
    pause.checked = !!h.global_pause;

    document.getElementById('ch-watcher').textContent =
      h.watcher_running ? '✓' : '✗';
  } catch {
    /* ignore */
  }
}

// ============ HELPERS ============
function escape(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ============ BOOT ============
async function boot() {
  await Settings.load();
  await Accounts.refresh();
  await refreshStatus();
  setInterval(refreshStatus, 5000);
  setInterval(() => {
    if (document.querySelector('.tab-pane.active').id === 'tab-accounts') Accounts.refresh();
  }, 10000);
  Logs.startAuto();
  // ленивая загрузка каналов при первом переключении на вкладку
}

(async () => {
  const ok = await Login.check();
  if (ok) boot();
})();
