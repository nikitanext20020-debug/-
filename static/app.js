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
  if (!toastEl) return;
  toastEl.className = 'toast';
  toastEl.classList.add('show');
  if (kind) toastEl.classList.add(`toast-${kind}`);
  toastEl.textContent = msg;
  toastEl.setAttribute('role', 'status');
  toastEl.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
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
    if (btn.dataset.tab === 'dashboard') Dashboard.refresh();
    if (btn.dataset.tab === 'channels') { Channels.refresh(); Pending.refresh(); }
    if (btn.dataset.tab === 'logs') Logs.refresh();
    if (btn.dataset.tab === 'accounts') Accounts.refresh();
    if (btn.dataset.tab === 'proxies') Proxies.refresh();
    if (btn.dataset.tab === 'stats') Stats.refresh();
    if (btn.dataset.tab === 'comments') { accSelectFill('cm-acc-select', { includeAll: true }); Comments.refresh(); }
    if (btn.dataset.tab === 'inviter') Inviter.refresh();
    if (btn.dataset.tab === 'masssend') MassSend.refresh();
    if (btn.dataset.tab === 'ownchannels') OwnChannels.refresh();
  });
});

// ============ ACCOUNTS ============
const Accounts = {
  data: [],
  proxies: [],
  async refresh() {
    const grid = document.getElementById('accounts-grid');
    try {
      // ✅ Сначала синхронизируем профили из Telegram (для running аккаунтов)
      try {
        await API.post('/accounts/sync-profiles');
      } catch (e) {
        // Молча игнорируем ошибку - это не критично
      }
      
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
    
    // ✅ display_name с fallback на session_name или phone
    const displayName = (a.display_name && a.display_name !== 'Unknown') 
      ? a.display_name 
      : (a.session_name || a.phone || '—');
    const remainingSeconds = a.remaining_seconds || 0;
    const joinedChannels = a.joined_channels_count || 0;
    
    // DEBUG: логируем rate_limited_until
    if (a.rate_limited_until) {
      console.log(`[Account ${a.id}] rate_limited_until=${a.rate_limited_until}, remaining=${remainingSeconds}s`);
    }
    
    // Форматирование таймаута
    const timeoutChip = remainingSeconds > 0 
      ? `<span class="badge badge-warning timeout-badge" data-remaining="${remainingSeconds}">⏸ ${this.formatTime(remainingSeconds)}</span>`
      : '';
    
    return `
      <div class="account-card ${a.is_running ? 'running' : ''}">
        <div class="account-card-head">
          <div>
            <div class="account-name">${escape(displayName)}</div>
            <div class="account-phone">${escape(a.phone || '—')}</div>
            <div class="muted small">${escape(a.session_name || '')}</div>
            ${timeoutChip}
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
          <span>Вступлен в каналов</span><span>${joinedChannels}</span>
        </div>
        <div class="account-meta">
          <span>Health</span><span>${escape(a.health_status || '—')}</span>
        </div>
        <div class="account-proxy">
          <span class="muted">Прокси</span>
          <select data-proxy-for="${a.id}">${this.proxyOptions(a.proxy_id)}</select>
        </div>
        <div class="account-actions">
          <div class="action-row">
            ${a.is_running
              ? `<button class="btn btn-ghost btn-sm" data-act="stop"   data-id="${a.id}">Стоп</button>`
              : `<button class="btn btn-primary btn-sm" data-act="start" data-id="${a.id}">Старт</button>`}
            <button class="btn btn-ghost btn-sm" data-act="profile" data-id="${a.id}">Профиль</button>
            <button class="btn btn-ghost btn-sm" data-act="reset-bans" data-id="${a.id}">Сбросить</button>
            <button class="btn btn-danger btn-sm" data-act="delete"    data-id="${a.id}">Удалить</button>
          </div>
          <div class="action-row">
            <button class="btn btn-info btn-sm" data-act="stats" data-id="${a.id}">📊 Стата</button>
            <button class="btn btn-info btn-sm" data-act="channels" data-id="${a.id}">📥 Каналы (${joinedChannels})</button>
            <button class="btn btn-warning btn-sm" data-act="rate-history" data-id="${a.id}">⏱️ Лимиты</button>
          </div>
        </div>
      </div>`;
  },
  
  formatTime(seconds) {
    if (seconds <= 0) return '0:00';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) {
      return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
    return `${m}:${String(s).padStart(2, '0')}`;
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
      } else if (act === 'stats') {
        // ✅ NEW: Открыть модалку со статистикой
        await this.showStats(id);
        return;
      } else if (act === 'channels') {
        // ✅ NEW: Открыть модалку с каналами
        await this.showChannels(id);
        return;
      } else if (act === 'rate-history') {
        // ✅ NEW: Открыть модалку с историей rate limit'ов
        await this.showRateLimitHistory(id);
        return;
      }
      this.refresh();
    } catch (e) {
      toast(e.message, 'error');
    }
  },
  
  async showStats(id) {
    try {
      const stats = await API.get(`/accounts/${id}/stats-full`);
      const html = `
        <div class="modal-overlay" id="modal-stats-overlay">
          <div class="modal" id="modal-stats">
            <div class="modal-header">
              <h3>📊 Статистика: ${escape(stats.display_name)}</h3>
              <button class="modal-close" onclick="document.getElementById('modal-stats-overlay').remove()">×</button>
            </div>
            <div class="modal-body">
              <div class="stats-grid">
                <div class="stat-card">
                  <div class="stat-label">💬 Комментарии</div>
                  <div class="stat-value">${stats.comments || 0}</div>
                </div>
                <div class="stat-card">
                  <div class="stat-label">📨 Инвайты</div>
                  <div class="stat-value">${stats.invites || 0}</div>
                </div>
                <div class="stat-card">
                  <div class="stat-label">📥 Вступления</div>
                  <div class="stat-value">${stats.joined_channels || 0}</div>
                </div>
                <div class="stat-card">
                  <div class="stat-label">🚫 Забаны</div>
                  <div class="stat-value">${stats.banned_count || 0}</div>
                </div>
                <div class="stat-card">
                  <div class="stat-label">❤️ Статус</div>
                  <div class="stat-value">${escape(stats.health_status || 'unknown')}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      `;
      document.body.insertAdjacentHTML('beforeend', html);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'error');
    }
  },
  
  async showChannels(id) {
    try {
      const data = await API.get(`/accounts/${id}/joined-channels`);
      const channels = data.channels || [];
      const html = `
        <div class="modal-overlay" id="modal-channels-overlay">
          <div class="modal" id="modal-channels">
            <div class="modal-header">
              <h3>📥 Каналы (${channels.length})</h3>
              <button class="modal-close" onclick="document.getElementById('modal-channels-overlay').remove()">×</button>
            </div>
            <div class="modal-body">
              ${channels.length === 0 
                ? '<p class="muted">Нет вступлений</p>' 
                : `<table class="channels-table">
                    <thead>
                      <tr>
                        <th>Канал</th>
                        <th>Название</th>
                        <th>Дата</th>
                      </tr>
                    </thead>
                    <tbody>
                      ${channels.map(ch => `
                        <tr>
                          <td><code>${escape(ch.channel)}</code></td>
                          <td>${escape(ch.title || '—')}</td>
                          <td>${ch.joined_at ? new Date(ch.joined_at).toLocaleDateString('ru-RU') : '—'}</td>
                        </tr>
                      `).join('')}
                    </tbody>
                  </table>`
              }
            </div>
          </div>
        </div>
      `;
      document.body.insertAdjacentHTML('beforeend', html);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'error');
    }
  },
  
  async showRateLimitHistory(id) {
    try {
      const data = await API.get(`/accounts/${id}/rate-limit-history`);
      const history = data.history || [];
      const stats = data.stats || {};
      
      const html = `
        <div class="modal-overlay" id="modal-rate-history-overlay">
          <div class="modal" id="modal-rate-history">
            <div class="modal-header">
              <h3>⏱️ История rate limit'ов</h3>
              <button class="modal-close" onclick="document.getElementById('modal-rate-history-overlay').remove()">×</button>
            </div>
            <div class="modal-body">
              <div class="stats-grid">
                <div class="stat-card">
                  <div class="stat-label">📊 Всего лимитов</div>
                  <div class="stat-value">${stats.total_limits || 0}</div>
                </div>
                ${Object.entries(stats.by_action || {}).map(([action, count]) => `
                  <div class="stat-card">
                    <div class="stat-label">${action || '—'}</div>
                    <div class="stat-value">${count}</div>
                  </div>
                `).join('')}
              </div>
              
              <hr style="margin: 16px 0; border: none; border-top: 1px solid var(--line);" />
              
              ${history.length === 0 
                ? '<p class="muted">Нет событий rate limit</p>' 
                : `<table class="channels-table">
                    <thead>
                      <tr>
                        <th>Дата</th>
                        <th>Действие</th>
                        <th>Длительность</th>
                      </tr>
                    </thead>
                    <tbody>
                      ${history.map(h => `
                        <tr>
                          <td>${h.triggered_at ? new Date(h.triggered_at).toLocaleString('ru-RU') : '—'}</td>
                          <td>${escape(h.action || '—')}</td>
                          <td>${h.duration_seconds ? (h.duration_seconds / 3600).toFixed(1) + 'ч' : '—'}</td>
                        </tr>
                      `).join('')}
                    </tbody>
                  </table>`
              }
            </div>
          </div>
        </div>
      `;
      document.body.insertAdjacentHTML('beforeend', html);
    } catch (e) {
      toast('Ошибка: ' + e.message, 'error');
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
      this.form.owned_channel.value = p.owned_channel || '';
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
      // ✅ Сохраняем данные в Telegram (профиль)
      await API.put(`/accounts/${this.id}/profile`, payload);
      
      // ✅ Сохраняем owned_channel в БД
      const ownedChannel = this.form.owned_channel.value.trim();
      if (ownedChannel) {
        await API.post(`/accounts/${this.id}/set-owned-channel`, {channel: ownedChannel});
      }
      
      toast('Профиль обновлён', 'ok');
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

// Если API ID/Hash заданы глобально в .env — прячем ручные поля, показываем подсказку
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

// ---- Переключение режимов добавления: .session / номер ----
const PhoneAuth = {
  phone: '',
  reset() {
    document.getElementById('form-phone-step1')?.classList.remove('hidden');
    document.getElementById('form-phone-step2')?.classList.add('hidden');
    document.getElementById('form-phone-step3')?.classList.add('hidden');
    document.getElementById('form-phone-step1')?.reset();
    document.getElementById('form-phone-step2')?.reset();
    document.getElementById('form-phone-step3')?.reset();
  },
  show(step) {
    document.getElementById('form-phone-step1').classList.toggle('hidden', step !== 1);
    document.getElementById('form-phone-step2').classList.toggle('hidden', step !== 2);
    document.getElementById('form-phone-step3').classList.toggle('hidden', step !== 3);
  },
  async fillProxies() {
    const sel = document.getElementById('phone-proxy-select');
    if (!sel) return;
    try {
      const proxies = await API.get('/proxies');
      sel.innerHTML = '<option value="">Без прокси</option>' +
        proxies.map(p => `<option value="${p.id}">${escape((p.type || 'http').toUpperCase())} ${escape(p.ip)}:${escape(String(p.port))}</option>`).join('');
    } catch { /* необязательно */ }
  },
};

document.querySelectorAll('.modal-switch-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.modal-switch-btn').forEach(b => b.classList.toggle('active', b === btn));
    const mode = btn.dataset.mode;
    document.getElementById('mode-session').classList.toggle('hidden', mode !== 'session');
    document.getElementById('mode-phone').classList.toggle('hidden', mode !== 'phone');
    if (mode === 'phone') { PhoneAuth.reset(); PhoneAuth.fillProxies(); }
  });
});

// Прячем ручные API-поля в режиме номера, если ключи уже заданы
(async () => {
  try {
    const cfg = await API.get('/config-status');
    if (cfg && cfg.api_configured) {
      document.getElementById('phone-api-fields')?.classList.add('hidden');
      document.getElementById('phone-api-note')?.classList.remove('hidden');
    }
  } catch (_) { /* необязательно */ }
})();

// Шаг 1 — отправка кода
document.getElementById('form-phone-step1').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target;
  const phone = (f.phone.value || '').trim();
  if (!phone) { toast('Укажите номер', 'error'); return; }
  const body = { phone };
  const proxyId = f.proxy_id.value;
  if (proxyId) body.proxy_id = Number(proxyId);
  if (f.api_id.value) body.api_id = Number(f.api_id.value);
  if ((f.api_hash.value || '').trim()) body.api_hash = f.api_hash.value.trim();

  const submitBtn = f.querySelector('button[type="submit"]');
  submitBtn.disabled = true; submitBtn.textContent = 'Отправка…';
  try {
    const r = await API.post('/auth/send-code', body);
    PhoneAuth.phone = phone;
    PhoneAuth.hash = r.phone_code_hash;
    document.getElementById('phone-display').textContent = phone;
    toast('Код отправлен в Telegram', 'ok');
    PhoneAuth.show(2);
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    submitBtn.disabled = false; submitBtn.textContent = 'Отправить код';
  }
});

document.getElementById('phone-back-1').addEventListener('click', () => PhoneAuth.show(1));

// Шаг 2 — проверка кода
document.getElementById('form-phone-step2').addEventListener('submit', async (e) => {
  e.preventDefault();
  const code = (e.target.code.value || '').trim();
  if (!code) { toast('Введите код', 'error'); return; }
  const submitBtn = e.target.querySelector('button[type="submit"]');
  submitBtn.disabled = true; submitBtn.textContent = 'Проверка…';
  try {
    const r = await API.post('/auth/verify-code', {
      phone: PhoneAuth.phone, code, phone_code_hash: PhoneAuth.hash,
    });
    if (r.status === 'password_required') {
      toast('Нужен пароль 2FA', 'ok');
      PhoneAuth.show(3);
    } else {
      await PhoneAuth.finish();
    }
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    submitBtn.disabled = false; submitBtn.textContent = 'Подтвердить';
  }
});

// Шаг 3 — пароль 2FA
document.getElementById('form-phone-step3').addEventListener('submit', async (e) => {
  e.preventDefault();
  const password = e.target.password.value || '';
  if (!password) { toast('Введите пароль', 'error'); return; }
  const submitBtn = e.target.querySelector('button[type="submit"]');
  submitBtn.disabled = true; submitBtn.textContent = 'Вход…';
  try {
    await API.post('/auth/verify-password', { phone: PhoneAuth.phone, password });
    await PhoneAuth.finish();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    submitBtn.disabled = false; submitBtn.textContent = 'Войти';
  }
});

// Завершение: сохраняем сессию как аккаунт
PhoneAuth.finish = async function () {
  try {
    await API.post('/accounts', { phone: this.phone });
    toast('Аккаунт добавлен', 'ok');
    modal.classList.add('hidden');
    this.reset();
    Accounts.refresh();
  } catch (err) {
    toast('Авторизация прошла, но сохранить не удалось: ' + err.message, 'error');
  }
};

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
    try {
      const excluded = await API.get('/discovery/exclusions?limit=5000');
      document.getElementById('ch-excluded').textContent = Array.isArray(excluded) ? excluded.length : 0;
    } catch {
      document.getElementById('ch-excluded').textContent = '—';
    }
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
          <button type="button" class="btn btn-ghost btn-sm ${c.is_pinned ? 'pinned' : ''}" data-pin="${escape(c.channel)}" data-pinned="${c.is_pinned ? '1' : '0'}" title="${c.is_pinned ? 'Закреплён (защищён от очистки) — нажмите чтобы открепить' : 'Закрепить (защитить от очистки)'}" aria-label="${c.is_pinned ? 'Открепить канал' : 'Закрепить канал'}">${c.is_pinned ? '🔒' : '🔓'}</button>
          <button type="button" class="btn btn-ghost btn-sm" data-recheck="${escape(c.channel)}" title="Перепроверить" aria-label="Перепроверить канал">↻</button>
          <button type="button" class="btn btn-ghost btn-sm" data-comment="${escape(c.channel)}" title="Комментить сейчас" aria-label="Комментировать сейчас">✎</button>
          <button type="button" class="btn btn-ghost btn-sm" data-del="${escape(c.channel)}" title="Удалить" aria-label="Удалить канал">×</button>
        </td>
      </tr>
    `).join('');
    tbody.querySelectorAll('[data-pin]').forEach(b => {
      b.addEventListener('click', async () => {
        const channel = b.dataset.pin;
        const isPinned = b.dataset.pinned === '1';
        b.disabled = true;
        try {
          const path = `/discovery/channels/${encodeURIComponent(channel)}/pin`;
          if (isPinned) {
            await API.del(path);
            toast(`@${channel} откреплён`, 'ok');
          } else {
            await API.post(path);
            toast(`@${channel} закреплён (защищён от очистки)`, 'ok');
          }
          // Обновляем локальные данные и перерисовываем
          const item = this.data.find(c => c.channel === channel);
          if (item) item.is_pinned = isPinned ? 0 : 1;
          this.render();
        } catch (e) { toast(e.message, 'error'); b.disabled = false; }
      });
    });
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
document.getElementById('btn-cleanup-unpinned')?.addEventListener('click', async () => {
  const daysEl = document.getElementById('cleanup-unpinned-days');
  const raw = (daysEl?.value || '').trim();
  const days = raw === '' ? null : parseInt(raw, 10);
  const msg = days && days > 0
    ? `Удалить все незакреплённые каналы старше ${days} дн.? Закреплённые (🔒) останутся.`
    : 'Удалить ВСЕ незакреплённые каналы? Закреплённые (🔒) останутся.';
  if (!confirm(msg)) return;
  const btn = document.getElementById('btn-cleanup-unpinned');
  btn.disabled = true;
  try {
    const path = days && days > 0
      ? `/discovery/cleanup-unpinned?older_than_days=${days}`
      : '/discovery/cleanup-unpinned';
    const r = await API.post(path);
    toast(`Удалено незакреплённых каналов: ${r && r.deleted != null ? r.deleted : 0}`, 'ok');
    Channels.refresh();
  } catch (e) { toast(e.message, 'error'); }
  finally { btn.disabled = false; }
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
  _last: [],
  _accountsFilled: false,

  accLabel(l) {
    if (l.account_id == null) return 'sys';
    if (l.phone) return l.phone;
    return 'acc:' + l.account_id;
  },

  fillAccounts() {
    const sel = document.getElementById('logs-account');
    if (!sel) return;
    const prev = sel.value;
    const accs = Accounts.data || [];
    sel.innerHTML = '<option value="">Все аккаунты</option>' +
      accs.map(a => `<option value="${a.id}">${escape(a.phone || ('acc ' + a.id))}</option>`).join('');
    if (prev) sel.value = prev;
    this._accountsFilled = true;
  },

  async refreshSummary() {
    try {
      const counts = await API.get('/logs/summary?hours=24');
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v || 0; };
      set('lb-success', counts.success);
      set('lb-info', counts.info);
      set('lb-warning', counts.warning);
      set('lb-error', counts.error);
    } catch { /* ignore */ }
  },

  async refresh() {
    const c = document.getElementById('logs-container');
    if (!this._accountsFilled) this.fillAccounts();
    const lvl = document.getElementById('logs-level').value;
    const search = (document.getElementById('logs-search').value || '').trim();
    const acc = document.getElementById('logs-account').value;

    // Сохраняем позицию скролла, чтобы автообновление не дёргало чтение
    const atTop = c.scrollTop <= 8;
    const prevScroll = c.scrollTop;

    try {
      const params = new URLSearchParams();
      if (lvl) params.set('level', lvl);
      if (search) params.set('search', search);
      if (acc) params.set('account_id', acc);
      params.set('limit', '400');
      const data = await API.get('/logs?' + params.toString());
      const list = Array.isArray(data) ? data : (data.logs || []);
      this._last = list;
      this.refreshSummary();

      if (!list.length) { c.innerHTML = '<div class="logs-empty">— нет записей по фильтру —</div>'; return; }

      c.innerHTML = list.map((l, i) => {
        const ts = (l.timestamp || '').slice(11, 19) || '--:--:--';
        const date = (l.timestamp || '').slice(0, 10);
        let level = (l.level || 'info').toLowerCase();
        if (!['success', 'info', 'warning', 'error', 'debug', 'critical'].includes(level)) level = 'info';
        const acc = this.accLabel(l);
        const mod = l.module ? `<span class="log-mod">${escape(l.module)}</span>` : '';
        const trace = l.stack_trace
          ? `<details class="log-trace"><summary>stack trace</summary><pre>${escape(l.stack_trace)}</pre></details>`
          : '';
        return `<div class="log-line ${level}">
          <span class="log-time" title="${escape(date + ' ' + ts)}">${escape(ts)}</span>
          <span class="log-level ${level}">${escape(level)}</span>
          <span class="log-acc">${escape(acc)}</span>
          <span class="log-msg">${escape(l.message || '')}${mod}${trace}</span>
        </div>`;
      }).join('');

      // Восстанавливаем скролл: если были вверху (смотрим новые) — остаёмся вверху
      c.scrollTop = atTop ? 0 : prevScroll;
    } catch (e) {
      c.innerHTML = `<div class="logs-empty">Ошибка: ${escape(e.message)}</div>`;
    }
  },

  toText() {
    return (this._last || []).map(l =>
      `[${(l.timestamp || '').slice(0, 19)}] [${(l.level || 'info').toUpperCase()}] [${this.accLabel(l)}] ${l.message || ''}` +
      (l.stack_trace ? `\n${l.stack_trace}` : '')
    ).join('\n');
  },

  async copy() {
    try {
      await navigator.clipboard.writeText(this.toText());
      toast('Логи скопированы', 'ok');
    } catch { toast('Не удалось скопировать', 'error'); }
  },

  download() {
    const blob = new Blob([this.toText()], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `neurocore-logs-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
    a.click();
    URL.revokeObjectURL(url);
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
document.getElementById('logs-account').addEventListener('change', () => Logs.refresh());
document.getElementById('btn-copy-logs').addEventListener('click', () => Logs.copy());
document.getElementById('btn-download-logs').addEventListener('click', () => Logs.download());
document.getElementById('logs-autorefresh').addEventListener('change', (e) => {
  if (e.target.checked) Logs.startAuto(); else Logs.stopAuto();
});
// Дебаунс поиска
let _logsSearchTimer = null;
document.getElementById('logs-search').addEventListener('input', () => {
  clearTimeout(_logsSearchTimer);
  _logsSearchTimer = setTimeout(() => Logs.refresh(), 350);
});
// Клик по бейджу уровня — быстрый фильтр
document.querySelectorAll('#logs-badges .log-badge').forEach(b => {
  b.addEventListener('click', () => {
    const sel = document.getElementById('logs-level');
    sel.value = (sel.value === b.dataset.level) ? '' : b.dataset.level;
    Logs.refresh();
  });
});

// ============ DASHBOARD (ГЛАВНАЯ) ============
function gotoTab(name) {
  const btn = document.querySelector(`.tab[data-tab="${name}"]`);
  if (btn) btn.click();
}

const Dashboard = {
  _timer: null,

  async refresh() {
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

    // Аккаунты + статусы воркеров
    let accounts = [];
    try {
      accounts = await API.get('/accounts');
      Accounts.data = accounts;
    } catch (e) { console.log('[v0] dashboard accounts error:', e.message); }

    const running = accounts.filter(a => a.is_running).length;
    setText('dash-accounts', accounts.length);
    setText('dash-running', `${running}/${accounts.length}`);

    // Здоровье системы
    try {
      const h = await API.get('/health');
      setText('dash-watcher', h.watcher_running ? 'активен' : 'остановлен');
      setText('dash-pause', h.global_pause ? 'ВКЛ' : 'выкл');
    } catch { /* ignore */ }

    // Активность за 24ч
    try {
      const s = await API.get('/stats/24h');
      setText('dash-comments-24h', (s.comments_24h ?? 0).toLocaleString('ru-RU'));
      setText('dash-errors-today', (s.errors_today ?? 0).toLocaleString('ru-RU'));
    } catch { /* ignore */ }

    this.renderAccounts(accounts);
    await this.renderLogs();
    setText('dash-updated', 'обновлено ' + new Date().toLocaleTimeString('ru-RU'));
  },

  renderAccounts(accounts) {
    const box = document.getElementById('dash-accounts-list');
    if (!box) return;
    if (!accounts.length) {
      box.innerHTML = `<div class="empty">Нет аккаунтов. Импортируйте .session-файл во вкладке «Аккаунты».</div>`;
      return;
    }
    box.innerHTML = accounts.map(a => {
      const status = a.is_running ? 'running' : (a.status || 'stopped');
      const comments = (a.stats && (a.stats.total_comments_sent || a.stats.comments_sent)) || 0;
      return `
        <div class="dash-acc-row">
          <span class="account-status-dot ${escape(status)}" title="${escape(status)}"></span>
          <div class="dash-acc-main">
            <div class="dash-acc-phone">${escape(a.phone || '—')}</div>
            <div class="muted small">${escape(a.health_status || status)} · ${comments} комм.</div>
          </div>
          <button class="btn btn-sm ${a.is_running ? 'btn-ghost' : 'btn-primary'}"
                  data-dash-act="${a.is_running ? 'stop' : 'start'}" data-id="${a.id}">
            ${a.is_running ? 'Стоп' : 'Старт'}
          </button>
        </div>`;
    }).join('');
    box.querySelectorAll('[data-dash-act]').forEach(b => {
      b.addEventListener('click', async () => {
        const id = +b.dataset.id;
        const act = b.dataset.act || b.dataset.dashAct;
        b.disabled = true;
        try {
          await API.post(`/accounts/${id}/${act}`);
          toast(act === 'start' ? 'Воркер запущен' : 'Воркер остановлен', 'ok');
          this.refresh();
        } catch (e) { toast(e.message, 'error'); b.disabled = false; }
      });
    });
  },

  async renderLogs() {
    const box = document.getElementById('dash-logs');
    if (!box) return;
    try {
      const data = await API.get('/logs?limit=25');
      const list = Array.isArray(data) ? data : (data.logs || []);
      if (!list.length) { box.innerHTML = '<div class="logs-empty">— нет записей —</div>'; return; }
      box.innerHTML = list.map(l => {
        const ts = (l.timestamp || '').slice(11, 19) || '--:--:--';
        let level = (l.level || 'info').toLowerCase();
        if (!['success', 'info', 'warning', 'error', 'debug', 'critical'].includes(level)) level = 'info';
        return `<div class="log-line ${level}">
          <span class="log-time">${escape(ts)}</span>
          <span class="log-level ${level}">${escape(level)}</span>
          <span class="log-msg">${escape(l.message || '')}</span>
        </div>`;
      }).join('');
    } catch (e) {
      box.innerHTML = `<div class="logs-empty">Ошибка: ${escape(e.message)}</div>`;
    }
  },

  startAuto() {
    this.stopAuto();
    this._timer = setInterval(() => {
      const active = document.getElementById('tab-dashboard')?.classList.contains('active');
      if (active) this.refresh();
    }, 8000);
  },
  stopAuto() { if (this._timer) clearInterval(this._timer); this._timer = null; },
};
document.getElementById('btn-refresh-dashboard')?.addEventListener('click', () => Dashboard.refresh());
document.querySelectorAll('[data-goto-tab]').forEach(b => {
  b.addEventListener('click', () => gotoTab(b.dataset.gotoTab));
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

// ============ SETTINGS ============
const Settings = {
  fields() { return Array.from(document.querySelectorAll('[data-setting]')); },

  async load() {
    let data;
    try {
      data = await API.get('/settings');
    } catch (e) {
      console.log('[v0] Settings load error:', e.message);
      return;
    }
    for (const el of this.fields()) {
      const key = el.dataset.setting;
      if (!(key in data)) continue;
      const val = data[key];
      if (el.type === 'checkbox') {
        el.checked = !!val;
      } else if (val === null || val === undefined) {
        el.value = '';
      } else {
        el.value = val;
      }
    }
    // Глобальная пауза
    const pause = document.getElementById('global-pause-toggle');
    if (pause && 'global_pause' in data) pause.checked = !!data.global_pause;
    // Включаем автосохранение всех полей
    this.wireAutoSave();
  },

  collect() {
    const payload = {};
    for (const el of this.fields()) {
      const key = el.dataset.setting;
      if (el.type === 'checkbox') {
        payload[key] = el.checked;
      } else if (el.type === 'number') {
        const raw = el.value.trim();
        if (raw === '') continue;            // пустое число не отправляем
        const num = Number(raw);
        if (!Number.isNaN(num)) payload[key] = num;
      } else {
        payload[key] = el.value;
      }
    }
    return payload;
  },

  async save() {
    const btn = document.getElementById('btn-save-settings');
    const payload = this.collect();
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = 'Сохранение…';
    try {
      await API.post('/settings', payload);
      toast('Все настройки сохранены', 'ok');
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  },

  // Человекочитаемое название поля для уведомлений
  fieldLabel(el) {
    const row = el.closest('.toggle-row, .form-row');
    if (row) {
      const t = row.querySelector('.toggle-title, label');
      if (t) return t.textContent.trim().replace(/\s+/g, ' ');
    }
    return el.dataset.setting;
  },

  // Автосохранение одного поля при изменении
  async autoSave(el) {
    const key = el.dataset.setting;
    let value;
    if (el.type === 'checkbox') {
      value = el.checked;
    } else if (el.type === 'number') {
      const raw = el.value.trim();
      if (raw === '') return;                 // пустое число не трогаем
      const num = Number(raw);
      if (Number.isNaN(num)) { toast('Некорректное число', 'error'); return; }
      value = num;
    } else {
      value = el.value;
    }
    const name = this.fieldLabel(el);
    try {
      await API.post('/settings', { [key]: value });
      if (el.type === 'checkbox') {
        toast(`«${name}» — ${value ? 'включено' : 'выключено'}`, 'ok');
      } else {
        toast(`«${name}» сохранено`, 'ok');
      }
    } catch (e) {
      toast(`Не удалось сохранить «${name}»: ${e.message}`, 'error');
      // Откатываем визуальное состояние переключателя
      if (el.type === 'checkbox') el.checked = !el.checked;
    }
  },

  // Навешивает автосохранение на все поля с data-setting
  wireAutoSave() {
    for (const el of this.fields()) {
      if (el.dataset.autosaveWired) continue;
      el.dataset.autosaveWired = '1';
      const evt = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'change';
      el.addEventListener(evt, () => this.autoSave(el));
    }
  },

  async testAI() {
    const out = document.getElementById('settings-test-result');
    const btn = document.getElementById('btn-test-ai');
    btn.disabled = true;
    if (out) out.innerHTML = '<span class="muted">Проверяю нейросеть…</span>';
    try {
      // Сначала сохраняем ключ/модель, чтобы тест шёл по текущим значениям
      await API.post('/settings', this.collect());
      const r = await API.post('/settings/test-ai');
      const ok = r.status === 'ok' || r.status === 'success';
      if (out) out.innerHTML = `<span class="pill ${ok ? 'good' : 'bad'}">${escape(r.message || (ok ? 'OK' : 'Ошибка'))}</span>`;
      toast(r.message || (ok ? 'Нейросеть отвечает' : 'Ошибка нейросети'), ok ? 'ok' : 'error');
    } catch (e) {
      if (out) out.innerHTML = `<span class="pill bad">Ошибка: ${escape(e.message)}</span>`;
      toast(e.message, 'error');
    } finally {
      btn.disabled = false;
    }
  },
};

document.getElementById('btn-save-settings')?.addEventListener('click', () => Settings.save());
document.getElementById('btn-save-behavior')?.addEventListener('click', () => Settings.save());
document.getElementById('btn-test-ai')?.addEventListener('click', () => Settings.testAI());
document.getElementById('global-pause-toggle')?.addEventListener('change', async (e) => {
  try {
    await API.post('/settings', { global_pause: e.target.checked });
    toast(e.target.checked ? 'Глобальная пауза включена' : 'Глобальная пауза выключена', 'ok');
  } catch (err) {
    toast(err.message, 'error');
    e.target.checked = !e.target.checked;
  }
});

// ============ SHARED HELPERS ============
function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

// Заполняет <select> списком аккаунтов из Accounts.data
function accSelectFill(selId, { includeAll = false } = {}) {
  const sel = document.getElementById(selId);
  if (!sel) return;
  const prev = sel.value;
  const accs = Accounts.data || [];
  let html = includeAll ? '<option value="">Все аккаунты</option>' : '';
  html += accs.map(a =>
    `<option value="${a.id}">${escape(a.phone || ('acc ' + a.id))}${a.is_running ? '' : ' (не запущен)'}</option>`
  ).join('');
  sel.innerHTML = html || '<option value="">Нет аккаунтов</option>';
  if (prev && accs.some(a => String(a.id) === prev)) sel.value = prev;
}

function accPhone(id) {
  const a = (Accounts.data || []).find(x => String(x.id) === String(id));
  return a ? (a.phone || ('acc ' + id)) : ('acc ' + id);
}

// ============ STATS ============
const Stats = {
  async refresh() {
    try {
      const g = await API.get('/stats/global');
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = (v ?? 0).toLocaleString('ru-RU'); };
      set('st-total-channels', g.total_channels);
      set('st-open-channels', g.open_comments_channels);
      set('st-accounts', g.total_accounts);
      set('st-total-comments', g.total_comments);
      set('st-total-likes', g.total_likes);
      set('st-new-today', g.new_channels_today);
    } catch (e) { toast(e.message, 'error'); }

    try {
      const h = await API.get('/stats/24h');
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = (v ?? 0).toLocaleString('ru-RU'); };
      set('st-comments-24h', h.comments_24h);
      set('st-success-today', h.success_today);
      set('st-errors-today', h.errors_today);
      set('st-likes-24h', h.likes_24h);
      set('st-channels-24h', h.channels_commented_24h);
      set('st-new-24h', h.new_channels_24h);

      const tbody = document.getElementById('st-accounts-tbody');
      const act = h.accounts_activity || [];
      if (!act.length) {
        tbody.innerHTML = `<tr><td colspan="2" class="empty">Нет активности</td></tr>`;
      } else {
        tbody.innerHTML = act.map(a =>
          `<tr><td>${escape(accPhone(a.account_id))}</td><td>${(a.comments || 0).toLocaleString('ru-RU')}</td></tr>`
        ).join('');
      }
    } catch (e) { /* ignore */ }
  },
};
document.getElementById('btn-refresh-stats')?.addEventListener('click', () => Stats.refresh());
document.getElementById('btn-clear-stats')?.addEventListener('click', async () => {
  if (!confirm('Полностью очистить статистику, комментарии, логи и баны? Действие необратимо.')) return;
  try {
    const r = await API.post('/admin/clear-stats');
    toast((r && r.message) || 'Очищено', 'ok');
    Stats.refresh();
  } catch (e) { toast(e.message, 'error'); }
});

// ============ COMMENTS ============
const Comments = {
  data: [],
  expanded: new Set(),
  COLLAPSE_AT: 160,
  async refresh() {
    const tbody = document.getElementById('comments-tbody');
    const accId = document.getElementById('cm-acc-select').value;
    try {
      this.data = await API.get('/comments' + (accId ? `?account_id=${accId}` : ''));
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Ошибка: ${escape(e.message)}</td></tr>`;
      return;
    }
    this.render();
  },
  renderTextCell(c) {
    const full = c.comment_text || '';
    const long = full.length > this.COLLAPSE_AT || full.split('\n').length > 3;
    const open = this.expanded.has(c.id);
    const clampedClass = long && !open ? ' is-clamped' : '';
    const btn = long
      ? `<button type="button" class="comment-expand" data-expand="${c.id}" aria-expanded="${open ? 'true' : 'false'}">${open ? 'Свернуть' : 'Показать полностью'}</button>`
      : '';
    return `<div class="comment-text-cell">
      <div class="comment-text${clampedClass}" id="comment-text-${c.id}">${escape(full)}</div>
      ${btn}
    </div>`;
  },
  render() {
    const q = (document.getElementById('comments-search').value || '').toLowerCase().trim();
    const tbody = document.getElementById('comments-tbody');
    let rows = this.data;
    if (q) rows = rows.filter(c =>
      (c.comment_text || '').toLowerCase().includes(q) ||
      (c.channel || '').toLowerCase().includes(q));
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Нет комментариев</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(c => {
      const ch = escape(c.channel || '—');
      const chCell = c.link ? `<a href="${escape(c.link)}" target="_blank" rel="noopener">${ch}</a>` : ch;
      const typeLabel = c.message_type === 'chat' ? '<span class="pill">чат</span>' : '<span class="pill good">коммент</span>';
      return `
        <tr>
          <td>${chCell}<div class="muted small">${escape(c.account_phone || '')}</div></td>
          <td>${this.renderTextCell(c)}</td>
          <td>${typeLabel}</td>
          <td class="muted small">${escape((c.sent_at || '').slice(0, 16))}</td>
          <td>
            <button type="button" class="btn btn-ghost btn-sm" data-edit="${c.id}" title="Редактировать" aria-label="Редактировать комментарий">✎</button>
            <button type="button" class="btn btn-ghost btn-sm" data-del="${c.id}" title="Удалить" aria-label="Удалить комментарий">×</button>
          </td>
        </tr>`;
    }).join('');
    tbody.querySelectorAll('[data-expand]').forEach(b => {
      b.addEventListener('click', () => {
        const id = +b.dataset.expand;
        if (this.expanded.has(id)) this.expanded.delete(id);
        else this.expanded.add(id);
        this.render();
      });
    });
    tbody.querySelectorAll('[data-edit]').forEach(b => {
      b.addEventListener('click', () => this.edit(+b.dataset.edit));
    });
    tbody.querySelectorAll('[data-del]').forEach(b => {
      b.addEventListener('click', () => this.remove(+b.dataset.del));
    });
  },
  async edit(id) {
    const c = this.data.find(x => x.id === id);
    const current = c ? c.comment_text : '';
    const next = prompt('Новый текст комментария:', current || '');
    if (next === null || next.trim() === '' || next === current) return;
    try {
      const r = await API.put(`/comments/${id}`, { new_text: next.trim() });
      toast((r && r.message) || 'Отредактировано', 'ok');
      this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },
  async remove(id) {
    if (!confirm('Удалить комментарий (в том числе из Telegram, если аккаунт запущен)?')) return;
    try {
      await API.del(`/comments/${id}`);
      toast('Удалён', 'ok');
      this.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },
};
document.getElementById('btn-refresh-comments')?.addEventListener('click', () => Comments.refresh());
document.getElementById('cm-acc-select')?.addEventListener('change', () => Comments.refresh());
document.getElementById('comments-search')?.addEventListener('input', () => Comments.render());

// ============ PENDING (заявки на модерации) ============
const Pending = {
  async refresh() {
    const tbody = document.getElementById('pending-tbody');
    if (!tbody) return;
    let rows;
    try {
      rows = await API.get('/discovery/pending');
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Ошибка: ${escape(e.message)}</td></tr>`;
      return;
    }
    const countEl = document.getElementById('pending-count');
    if (countEl) countEl.textContent = rows.length;
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">Нет заявок</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(p => `
      <tr>
        <td>${escape(p.channel_title || ('+' + (p.invite_hash || '')))}</td>
        <td class="muted small">${escape(accPhone(p.account_id))}</td>
        <td><span class="pill">${escape(p.status || 'pending')}</span></td>
        <td class="muted small">${escape((p.requested_at || '').slice(0, 16))}</td>
        <td class="muted small">${p.check_count || 0}</td>
      </tr>`).join('');
  },
};

// ============ CHANNEL FILTER ============
const ChannelFilter = {
  _poll: null,
  criteria() {
    return {
      min_subscribers: +document.getElementById('flt-min-subs').value || 0,
      min_avg_views: +document.getElementById('flt-min-views').value || 0,
      max_days_since_last_post: +document.getElementById('flt-max-days').value || 7,
      min_posts_per_week: +document.getElementById('flt-min-posts').value || 0,
      require_open_comments: document.getElementById('flt-open-comments').checked,
      junk_filter: document.getElementById('flt-junk').checked,
    };
  },
  async start() {
    const raw = (document.getElementById('flt-channels').value || '').trim();
    const channels = raw ? raw.split('\n').map(s => s.trim()).filter(Boolean) : null;
    try {
      const r = await API.post('/api/channels/filter/start', { channels, criteria: this.criteria() });
      toast(`Фильтрация запущена (${r.total || 0} каналов)`, 'ok');
      document.getElementById('filter-results-wrap').style.display = 'none';
      this.startPolling();
    } catch (e) { toast(e.message, 'error'); }
  },
  startPolling() {
    this.stopPolling();
    this._poll = setInterval(() => this.pollProgress(), 1500);
  },
  stopPolling() { if (this._poll) { clearInterval(this._poll); this._poll = null; } },
  async pollProgress() {
    try {
      const p = await API.get('/api/channels/filter/progress');
      const el = document.getElementById('filter-progress');
      el.textContent = `Обработано ${p.processed || 0}/${p.total || 0} · прошло ${p.passed || 0} · отсеяно ${p.rejected || 0} · ошибок ${p.errors || 0}`;
      if (!p.running && (p.processed || 0) >= (p.total || 0) && (p.total || 0) > 0) {
        this.stopPolling();
        this.loadResults();
      }
    } catch (e) { this.stopPolling(); }
  },
  async loadResults() {
    try {
      const r = await API.get('/api/channels/filter/results');
      const wrap = document.getElementById('filter-results-wrap');
      const tbody = document.getElementById('filter-results-tbody');
      if (!r.results || !r.results.length) { wrap.style.display = 'none'; return; }
      wrap.style.display = '';
      tbody.innerHTML = r.results.slice(0, 500).map(x => {
        const cls = x.status === 'passed' ? 'good' : (x.status === 'rejected' ? 'bad' : '');
        return `<tr>
          <td>${escape(x.channel || '')}</td>
          <td><span class="pill ${cls}">${escape(x.status || '')}</span></td>
          <td class="muted small">${escape(x.reason || '')}</td>
        </tr>`;
      }).join('');
    } catch (e) { /* ignore */ }
  },
  async stop() {
    try { await API.post('/api/channels/filter/stop'); this.stopPolling(); toast('Остановлено', 'ok'); }
    catch (e) { toast(e.message, 'error'); }
  },
  async apply() {
    if (!confirm('Удалить все отсеянные (rejected) каналы из базы?')) return;
    try {
      const r = await API.post('/api/channels/filter/apply');
      toast(`Удалено каналов: ${r.removed ?? r.deleted ?? 0}`, 'ok');
      Channels.refresh();
    } catch (e) { toast(e.message, 'error'); }
  },
};
document.getElementById('btn-filter-start')?.addEventListener('click', (e) => { e.preventDefault(); ChannelFilter.start(); });
document.getElementById('btn-filter-stop')?.addEventListener('click', (e) => { e.preventDefault(); ChannelFilter.stop(); });
document.getElementById('btn-filter-apply')?.addEventListener('click', (e) => { e.preventDefault(); ChannelFilter.apply(); });

// ============ INVITER ============
const Inviter = {
  accId() { return document.getElementById('inv-acc-select').value; },
  users: [],
  async refresh() {
    accSelectFill('inv-acc-select');
    const id = this.accId();
    if (!id) {
      this.users = [];
      this.renderUsers();
      this.updateStartEnabled();
      return;
    }
    try {
      const s = await API.get(`/accounts/${id}/inviter/stats`);
      document.getElementById('inv-parsed').textContent = (s.total ?? 0);
      document.getElementById('inv-invited').textContent = (s.success ?? 0);
      document.getElementById('inv-errors').textContent = (s.errors ?? 0);
      document.getElementById('inv-today').textContent = (s.today_count ?? 0);
    } catch (e) { /* ignore */ }
    await Promise.all([this.loadChats(), this.loadUsers()]);
    this.updateStartEnabled();
  },
  async loadChats() {
    const id = this.accId();
    const sel = document.getElementById('inv-chats-select');
    if (!sel) return;
    if (!id) { sel.innerHTML = '<option value="">Выберите аккаунт</option>'; return; }
    sel.innerHTML = '<option value="">— загрузка —</option>';
    try {
      const chats = await API.get(`/accounts/${id}/inviter/chats`);
      sel.innerHTML = '<option value="">— выберите чат-источник —</option>' +
        (chats || []).map(c => {
          const uname = c.username ? ('@' + String(c.username).replace(/^@/, '')) : '';
          // Prefer @username for source parse; keep numeric id in data-id for target resolve
          const val = uname || String(c.id ?? '');
          const label = (c.title || uname || c.id) + (c.id != null ? ` (${c.id})` : '');
          return `<option value="${escape(val)}" data-id="${escape(String(c.id ?? ''))}">${escape(label)}</option>`;
        }).join('');
    } catch (e) { sel.innerHTML = '<option value="">Не удалось загрузить (аккаунт запущен?)</option>'; }
  },
  async loadUsers() {
    const id = this.accId();
    if (!id) { this.users = []; this.renderUsers(); return; }
    try {
      this.users = await API.get(`/accounts/${id}/inviter/users?limit=200`) || [];
    } catch (e) {
      this.users = [];
    }
    this.renderUsers();
  },
  renderUsers() {
    const tbody = document.getElementById('inv-users-tbody');
    const countEl = document.getElementById('inv-users-count');
    if (!tbody) return;
    const n = this.users.length;
    if (countEl) countEl.textContent = n ? `${n} (показано до 200)` : '0';
    if (!n) {
      tbody.innerHTML = `<tr><td colspan="4" class="empty">Нет спарсенных кандидатов</td></tr>`;
      return;
    }
    tbody.innerHTML = this.users.map(u => {
      const name = [u.first_name, u.last_name].filter(Boolean).join(' ') || '—';
      const uname = u.username ? '@' + String(u.username).replace(/^@/, '') : '—';
      const src = u.source_chat_title || u.source_chat_id || '—';
      return `<tr>
        <td class="mono">${escape(String(u.user_id ?? u.id ?? '—'))}</td>
        <td>${escape(uname)}</td>
        <td>${escape(name)}</td>
        <td class="muted small">${escape(String(src))}</td>
      </tr>`;
    }).join('');
  },
  updateStartEnabled() {
    const btn = document.getElementById('btn-invite-start');
    if (!btn) return;
    const source = (document.getElementById('inv-source-override')?.value
      || document.getElementById('inv-source-chat')?.value || '').trim();
    const target = (document.getElementById('inv-target-channel')?.value || '').trim();
    const hasCandidates = this.users.length > 0 || !!source;
    btn.disabled = !target || !hasCandidates;
  },
};

document.getElementById('btn-refresh-inviter')?.addEventListener('click', () => Inviter.refresh());
document.getElementById('inv-acc-select')?.addEventListener('change', () => Inviter.refresh());
document.getElementById('inv-chats-select')?.addEventListener('change', (e) => {
  const sourceInput = document.getElementById('inv-source-chat');
  if (sourceInput && e.target.value) sourceInput.value = e.target.value;
  const override = document.getElementById('inv-source-override');
  if (override && e.target.value && !override.value) override.value = e.target.value;
  Inviter.updateStartEnabled();
});
['inv-source-chat', 'inv-source-override', 'inv-target-channel'].forEach(id => {
  document.getElementById(id)?.addEventListener('input', () => Inviter.updateStartEnabled());
});
document.getElementById('form-parse-users')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = Inviter.accId();
  if (!id) { toast('Выберите аккаунт', 'error'); return; }
  const chat = (e.target.chat_id.value || '').trim();
  const status = document.getElementById('parse-status');
  status.textContent = 'Парсинг…';
  try {
    const r = await API.post(`/accounts/${id}/inviter/parse`, { chat_id: chat });
    status.textContent = `Спарсено: ${r.parsed_count ?? 0}`;
    toast(`Спарсено пользователей: ${r.parsed_count ?? 0}`, 'ok');
    await Inviter.refresh();
  } catch (err) { status.textContent = ''; toast(err.message, 'error'); }
});
document.getElementById('form-invite-start')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = Inviter.accId();
  if (!id) { toast('Выберите аккаунт', 'error'); return; }
  const targetRaw = (e.target.channel_id.value || '').trim();
  const sourceRaw = (e.target.source_chat_id.value || '').trim() || null;
  const dailyLimit = e.target.daily_limit.value ? +e.target.daily_limit.value : null;
  const candidateCount = Inviter.users.length;
  if (!targetRaw) { toast('Укажите целевой канал/группу', 'error'); return; }
  if (!sourceRaw && !candidateCount) {
    toast('Нет источника и нет спарсенных кандидатов', 'error');
    return;
  }

  const limitLabel = dailyLimit != null ? dailyLimit : 'по умолчанию';
  const srcLabel = sourceRaw || 'уже спарсенные кандидаты';
  if (!confirm(
    `Запустить инвайт?\n\nИсточник: ${srcLabel}\nЦель: ${targetRaw}\nКандидатов в списке: ${candidateCount}\nЛимит: ${limitLabel}\n\nЦель будет проверена Telegram перед запуском. Ограничения не обходятся. Продолжить?`
  )) return;

  const body = {
    channel_id: targetRaw,
    source_chat_id: sourceRaw || null,
    daily_limit: dailyLimit,
  };
  try {
    const r = await API.post(`/accounts/${id}/inviter/start`, body);
    toast((r && r.message) || 'Инвайт запущен', 'ok');
    Inviter.refresh();
  } catch (err) { toast(err.message, 'error'); }
});

// ============ MASS SEND ============
const MassSend = {
  accId() { return document.getElementById('ms-acc-select').value; },
  dmPreview: null,
  groupPreview: null,
  _timers: {},

  async refresh() {
    accSelectFill('ms-acc-select');
    const tbody = document.getElementById('ms-campaigns-tbody');
    const id = this.accId();
    if (!id) { tbody.innerHTML = `<tr><td colspan="6" class="empty">Выберите аккаунт</td></tr>`; return; }
    try {
      const camps = await API.get(`/accounts/${id}/mass-send/campaigns`);
      if (!camps || !camps.length) { tbody.innerHTML = `<tr><td colspan="6" class="empty">Нет кампаний</td></tr>`; return; }
      const rows = await Promise.all(camps.map(async c => {
        let st = {};
        try { st = await API.get(`/accounts/${id}/mass-send/campaigns/${c.id}/stats`); } catch {}
        return `<tr>
          <td>${c.id}</td>
          <td>${escape(c.name || '—')}</td>
          <td>${escape(c.target_type || '—')}</td>
          <td><span class="pill">${escape(c.status || '—')}</span></td>
          <td>${st.sent ?? 0}</td>
          <td>${st.errors ?? 0}</td>
        </tr>`;
      }));
      tbody.innerHTML = rows.join('');
    } catch (e) { tbody.innerHTML = `<tr><td colspan="6" class="empty">Ошибка: ${escape(e.message)}</td></tr>`; }
  },

  /** Split raw text into tokens (comma / whitespace / newlines). */
  tokenize(raw) {
    return (raw || '')
      .split(/[\n,;]+/)
      .map(s => s.trim())
      .filter(Boolean);
  },

  /**
   * Normalize a single target token into a canonical string id/username.
   * Accepts: plain IDs, @username, t.me links, tg://user?id=
   * Does NOT coerce usernames to Number.
   */
  normalizeToken(token, targetType) {
    let s = String(token || '').trim();
    if (!s) return { ok: false, value: s, reason: 'пусто' };

    // tg://user?id=123456
    let m = s.match(/^tg:\/\/user\?(?:.*&)?id=(\d+)/i);
    if (m) {
      if (targetType === 'group') return { ok: false, value: s, reason: 'это пользователь, а не группа' };
      return { ok: true, value: m[1], kind: 'user_id' };
    }

    // strip wrapping angle brackets
    s = s.replace(/^<|>$/g, '');

    // Internal group link: t.me/c/<id>/<message> → -100<id>
    m = s.match(/^(?:https?:\/\/)?(?:www\.)?t\.me\/c\/(\d+)(?:\/\d+)?/i);
    if (m) {
      if (targetType === 'user') return { ok: false, value: s, reason: 'это группа, а не пользователь' };
      return { ok: true, value: `-100${m[1]}`, kind: 'chat_id' };
    }

    // URL: https://t.me/username or t.me/username
    m = s.match(/^(?:https?:\/\/)?(?:www\.)?(?:t\.me|telegram\.me)\/([A-Za-z0-9_]+)/i);
    if (m) {
      const u = m[1];
      if (/^joinchat$/i.test(u) || u.startsWith('+')) {
        return { ok: false, value: s, reason: 'invite-ссылки не поддерживаются' };
      }
      return { ok: true, value: '@' + u, kind: 'username' };
    }

    // @username
    if (s.startsWith('@')) {
      const u = s.slice(1);
      if (!/^[A-Za-z0-9_]{4,}$/i.test(u) && !/^[A-Za-z0-9_]+$/i.test(u)) {
        return { ok: false, value: s, reason: 'некорректный username' };
      }
      return { ok: true, value: '@' + u, kind: 'username' };
    }

    // plain numeric id: положительный user id, отрицательный chat/channel id
    if (/^-?\d+$/.test(s)) {
      const isGroupId = s.startsWith('-');
      if (targetType === 'user' && isGroupId) {
        return { ok: false, value: s, reason: 'это группа, а не пользователь' };
      }
      if (targetType === 'group' && !isGroupId) {
        return { ok: false, value: s, reason: 'для группы нужен @username или id вида -100…' };
      }
      return { ok: true, value: s, kind: isGroupId ? 'chat_id' : 'user_id' };
    }

    // bare username without @
    if (/^[A-Za-z][A-Za-z0-9_]{3,}$/.test(s)) {
      return { ok: true, value: '@' + s, kind: 'username' };
    }

    return { ok: false, value: s, reason: 'не распознано' };
  },

  localPreview(raw, targetType) {
    const tokens = this.tokenize(raw);
    const valid = [];
    const invalid = [];
    const seen = new Map();
    const duplicates = [];
    for (const t of tokens) {
      const n = this.normalizeToken(t, targetType);
      if (!n.ok) {
        invalid.push({ raw: t, reason: n.reason });
        continue;
      }
      const key = n.value.toLowerCase();
      if (seen.has(key)) {
        duplicates.push({ raw: t, value: n.value });
        continue;
      }
      seen.set(key, true);
      valid.push({ raw: t, value: n.value, kind: n.kind });
    }
    return {
      valid_count: valid.length,
      invalid_count: invalid.length,
      duplicate_count: duplicates.length,
      valid,
      invalid,
      duplicates,
      sample: valid.slice(0, 8).map(v => v.value),
    };
  },

  async serverPreview(raw, targetType) {
    const tokens = this.tokenize(raw);
    if (!tokens.length) return null;
    try {
      const r = await API.post('/telegram-targets/preview', {
        targets: tokens,
        target_type: targetType, // 'user' | 'group'
      });
      return r;
    } catch (e) {
      // Endpoint may be unavailable — fall back to local only
      return { _local_only: true, error: e.message };
    }
  },

  mergePreview(local, server) {
    if (!server || server._local_only) return { ...local, source: 'local', server_error: server && server.error };
    // Prefer server counts when present
    return {
      valid_count: server.valid_count ?? server.valid?.length ?? local.valid_count,
      invalid_count: server.invalid_count ?? server.invalid?.length ?? local.invalid_count,
      duplicate_count: server.duplicate_count ?? server.duplicates?.length ?? local.duplicate_count,
      valid: server.valid || local.valid,
      invalid: server.invalid || local.invalid,
      duplicates: server.duplicates || local.duplicates,
      sample: server.sample || local.sample,
      source: 'server',
    };
  },

  renderPreview(elId, preview, btnId) {
    const root = document.getElementById(elId);
    const btn = document.getElementById(btnId);
    if (!root) return;
    const stats = root.querySelector('.target-preview-stats');
    const sample = root.querySelector('.target-preview-sample');
    if (!preview || (preview.valid_count === 0 && preview.invalid_count === 0 && preview.duplicate_count === 0)) {
      stats.innerHTML = '<span class="muted">Введите цели для проверки</span>';
      sample.hidden = true;
      sample.innerHTML = '';
      if (btn) btn.disabled = true;
      return;
    }
    stats.innerHTML = `
      <span class="stat-ok">валидных: <b>${preview.valid_count}</b></span>
      <span class="stat-bad">невалидных: <b>${preview.invalid_count}</b></span>
      <span class="stat-dup">дубликатов: <b>${preview.duplicate_count}</b></span>
      ${preview.source === 'local' ? '<span class="muted">(локальная проверка)</span>' : ''}
    `;
    const items = [];
    const validList = preview.valid || [];
    const sampleVals = preview.sample
      || validList.slice(0, 8).map(v => (typeof v === 'string' ? v : (v.value || v.raw || v)));
    sampleVals.slice(0, 8).forEach(v => {
      items.push(`<li>${escape(String(v))}</li>`);
    });
    (preview.invalid || []).slice(0, 5).forEach(v => {
      const label = typeof v === 'string' ? v : `${v.raw || v.value || ''}${v.reason ? ' — ' + v.reason : ''}`;
      items.push(`<li class="is-invalid">${escape(String(label))}</li>`);
    });
    if (items.length) {
      sample.hidden = false;
      sample.innerHTML = items.join('');
    } else {
      sample.hidden = true;
      sample.innerHTML = '';
    }
    const ok = preview.valid_count > 0 && preview.invalid_count === 0;
    if (btn) btn.disabled = !ok;
  },

  schedulePreview(kind) {
    clearTimeout(this._timers[kind]);
    this._timers[kind] = setTimeout(() => this.runPreview(kind), 350);
  },

  async runPreview(kind) {
    const isDm = kind === 'dm';
    const raw = document.getElementById(isDm ? 'ms-dm-targets' : 'ms-group-targets')?.value || '';
    const targetType = isDm ? 'user' : 'group';
    const local = this.localPreview(raw, targetType);
    // Always show local immediately
    const elId = isDm ? 'ms-dm-preview' : 'ms-group-preview';
    const btnId = isDm ? 'btn-ms-dm-start' : 'btn-ms-group-start';
    this.renderPreview(elId, { ...local, source: 'local' }, btnId);
    if (isDm) this.dmPreview = local; else this.groupPreview = local;

    if (!raw.trim()) return;
    const server = await this.serverPreview(raw, targetType);
    const merged = this.mergePreview(local, server);
    // If server returned structured valid list of strings, map targets for submit
    if (server && !server._local_only) {
      // normalize valid entries to {value}
      const normValid = (merged.valid || []).map(v => {
        if (typeof v === 'string' || typeof v === 'number') return { value: String(v) };
        return { value: String(v.value ?? v.id ?? v.raw ?? v) };
      });
      merged.valid = normValid;
      // Prefer server invalid count strictly for enabling
    }
    if (isDm) this.dmPreview = merged; else this.groupPreview = merged;
    this.renderPreview(elId, merged, btnId);
  },

  /** Build payload targets: keep string IDs/usernames; never Number()-coerce DM targets. */
  targetsForSubmit(preview, { asNumber = false } = {}) {
    const list = (preview && preview.valid) || [];
    return list.map(v => {
      const val = typeof v === 'string' || typeof v === 'number' ? String(v) : String(v.value ?? v);
      if (asNumber && /^-?\d+$/.test(val)) return Number(val);
      // DM: keep numeric ids as strings OR numbers? Task: do not coerce DM targets to Number.
      // So always return string for DM. For groups, strings are fine (chat_ids: List).
      return val;
    });
  },
};

document.getElementById('btn-refresh-masssend')?.addEventListener('click', () => MassSend.refresh());
document.getElementById('ms-acc-select')?.addEventListener('change', () => MassSend.refresh());
document.getElementById('ms-dm-targets')?.addEventListener('input', () => MassSend.schedulePreview('dm'));
document.getElementById('ms-group-targets')?.addEventListener('input', () => MassSend.schedulePreview('group'));

document.getElementById('btn-ms-load-parsed')?.addEventListener('click', async () => {
  const id = MassSend.accId();
  if (!id) { toast('Выберите аккаунт', 'error'); return; }
  try {
    const users = await API.get(`/accounts/${id}/inviter/users?limit=1000`) || [];
    const ta = document.getElementById('ms-dm-targets');
    // существующие строки
    const existing = (ta.value || '').split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    const seen = new Set(existing.map(s => s.toLowerCase()));
    let added = 0;
    users.forEach(u => {
      const uname = (u.username || '').trim();
      const target = uname ? ('@' + uname.replace(/^@/, '')) : (u.user_id != null ? String(u.user_id) : '');
      if (!target) return;
      const key = target.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      existing.push(target);
      added++;
    });
    ta.value = existing.join('\n');
    MassSend.schedulePreview('dm');
    toast(`Добавлено спарсенных: ${added}`, 'ok');
  } catch (err) { toast(err.message, 'error'); }
});

document.getElementById('form-ms-dm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = MassSend.accId();
  if (!id) { toast('Выберите аккаунт', 'error'); return; }
  await MassSend.runPreview('dm');
  const preview = MassSend.dmPreview;
  const useParsed = document.getElementById('ms-dm-use-parsed')?.checked || false;
  const parseChat = (document.getElementById('ms-dm-parse-chat')?.value || '').trim();
  const hasParsedSource = useParsed || !!parseChat;
  if ((!preview || preview.valid_count <= 0) && !hasParsedSource) { toast('Нет валидных получателей', 'error'); return; }
  if (preview && preview.invalid_count > 0) { toast('Исправьте невалидные цели перед запуском', 'error'); return; }
  const user_ids = (preview && preview.valid_count > 0) ? MassSend.targetsForSubmit(preview, { asNumber: false }) : [];
  const confirmMsg = hasParsedSource
    ? `Запустить ЛС-рассылку?\n\nЯвных получателей: ${user_ids.length}${parseChat ? `\nБудут спарсены участники: ${parseChat}` : ''}${useParsed ? '\n+ все спарсенные из базы' : ''}\n\nЛимит/час соблюдается. Продолжить?`
    : `Запустить ЛС-рассылку на ${user_ids.length} получателей?\n\nЛимит/час соблюдается. Продолжить?`;
  if (!confirm(confirmMsg)) return;
  const body = {
    user_ids,
    message_template: e.target.message_template.value,
    hourly_limit: e.target.hourly_limit.value ? +e.target.hourly_limit.value : null,
    use_parsed: useParsed,
  };
  if (parseChat) body.parse_from_chat = parseChat;
  const f = e.target.media.files[0];
  if (f) body.media_base64 = await fileToBase64(f);
  try {
    const r = await API.post(`/accounts/${id}/mass-send/dm`, body);
    toast(`ЛС-рассылка запущена (кампания #${r.campaign_id})`, 'ok');
    MassSend.refresh();
  } catch (err) { toast(err.message, 'error'); }
});

document.getElementById('form-ms-groups')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = MassSend.accId();
  if (!id) { toast('Выберите аккаунт', 'error'); return; }
  await MassSend.runPreview('group');
  const preview = MassSend.groupPreview;
  if (!preview || preview.valid_count <= 0) { toast('Нет валидных групп', 'error'); return; }
  if (preview.invalid_count > 0) { toast('Исправьте невалидные цели перед запуском', 'error'); return; }
  const chat_ids = MassSend.targetsForSubmit(preview, { asNumber: false });
  if (!confirm(`Запустить рассылку в ${chat_ids.length} групп(ы)?\n\nЛимит/час соблюдается. Продолжить?`)) return;
  const body = {
    chat_ids,
    message_template: e.target.message_template.value,
    hourly_limit: e.target.hourly_limit.value ? +e.target.hourly_limit.value : null,
  };
  const f = e.target.media.files[0];
  if (f) body.media_base64 = await fileToBase64(f);
  try {
    const r = await API.post(`/accounts/${id}/mass-send/groups`, body);
    toast(`Рассылка в группы запущена (кампания #${r.campaign_id})`, 'ok');
    MassSend.refresh();
  } catch (err) { toast(err.message, 'error'); }
});

// ✅ NEW: Парсер групп для рассылок
document.getElementById('btn-parser-search')?.addEventListener('click', async () => {
  const keywords = (document.getElementById('parser-keywords')?.value || '').trim().split(',').map(k => k.trim()).filter(Boolean);
  if (!keywords.length) { toast('Введите ключевые слова', 'error'); return; }
  const status = document.getElementById('parser-status');
  status.textContent = '🔍 Поиск запущен... результаты обновятся в течение нескольких минут';
  try {
    await API.post(`/discovery/chats/search`, { keywords });
    toast('✅ Поиск запущен в фоне', 'ok');
  } catch (err) {
    status.textContent = '❌ Ошибка: ' + err.message;
    toast(err.message, 'error');
  }
});

document.getElementById('btn-parser-load')?.addEventListener('click', async () => {
  try {
    const data = await API.get(`/discovery/chats`);
    const chats = data.chats || [];
    if (!chats.length) { toast('Спарсенные группы не найдены', 'info'); return; }
    
    const textarea = document.getElementById('ms-group-targets');
    const existing = (textarea.value || '').split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
    const seen = new Set(existing.map(s => s.toLowerCase()));
    
    let added = 0;
    chats.forEach(ch => {
      const chatId = `-${ch.chat}`;
      const key = chatId.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      existing.push(chatId);
      added++;
    });
    
    textarea.value = existing.join('\n');
    MassSend.schedulePreview('group');
    toast(`Загружено групп: ${added}`, 'ok');
  } catch (err) { toast(err.message, 'error'); }
});

document.getElementById('btn-parser-clear')?.addEventListener('click', async () => {
  if (!confirm('Очистить кэш найденных групп?')) return;
  try {
    await API.post(`/discovery/chats/clear`);
    toast('✅ Кэш очищен', 'ok');
  } catch (err) { toast(err.message, 'error'); }
});

// ============ OWN CHANNELS ============
const OwnChannels = {
  data: [],
  selectedChannel: null,
  accId() { return document.getElementById('own-acc-select').value; },
  async refresh() {
    accSelectFill('own-acc-select');
    const tbody = document.getElementById('own-channels-tbody');
    const id = this.accId();
    if (!id) { tbody.innerHTML = `<tr><td colspan="3" class="empty">Выберите аккаунт</td></tr>`; return; }
    try {
      this.data = await API.get(`/accounts/${id}/channels`);
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="3" class="empty">Ошибка: ${escape(e.message)}</td></tr>`;
      return;
    }
    if (!this.data.length) { tbody.innerHTML = `<tr><td colspan="3" class="empty">Нет каналов</td></tr>`; return; }
    tbody.innerHTML = this.data.map(c => `
      <tr>
        <td>${escape(c.title || c.username || '—')}<div class="muted small">@${escape(c.username || '')}</div></td>
        <td class="muted small">${escape(String(c.channel_id || ''))}</td>
        <td><button class="btn btn-ghost btn-sm" data-select="${c.channel_id}" data-label="${escape(c.title || c.username || '')}">Выбрать</button></td>
      </tr>`).join('');
    tbody.querySelectorAll('[data-select]').forEach(b => {
      b.addEventListener('click', () => {
        this.selectedChannel = +b.dataset.select;
        document.getElementById('own-post-channel-label').textContent = b.dataset.label;
        this.loadQueue();
      });
    });
  },
  async loadQueue() {
    const id = this.accId();
    const list = document.getElementById('own-queue-list');
    if (!id || !this.selectedChannel) { list.innerHTML = '<span class="muted small">—</span>'; return; }
    try {
      const posts = await API.get(`/accounts/${id}/channel/${this.selectedChannel}/queue`);
      if (!posts.length) { list.innerHTML = '<span class="muted small">Очередь пуста</span>'; return; }
      list.innerHTML = posts.map(p =>
        `<div class="mini-list-item">${escape((p.content_text || '').slice(0, 100))}<div class="muted small">${escape(p.scheduled_at || 'сразу')}</div></div>`
      ).join('');
    } catch (e) { list.innerHTML = `<span class="muted small">Ошибка: ${escape(e.message)}</span>`; }
  },
};
document.getElementById('btn-refresh-own')?.addEventListener('click', () => OwnChannels.refresh());
document.getElementById('own-acc-select')?.addEventListener('change', () => OwnChannels.refresh());
document.getElementById('form-create-channel')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = OwnChannels.accId();
  if (!id) { toast('Выберите аккаунт', 'error'); return; }
  const status = document.getElementById('create-channel-status');
  const body = {
    title: e.target.title.value.trim(),
    about: e.target.about.value.trim(),
    username_base: e.target.username_base.value.trim(),
    topic: e.target.topic.value.trim(),
    publish_warmup: !!e.target.publish_warmup?.checked,
  };
  const f = e.target.avatar.files[0];
  if (f) body.avatar_base64 = await fileToBase64(f);
  status.textContent = 'Создание… (может занять до минуты)';
  try {
    const r = await API.post(`/accounts/${id}/channel/create`, body);
    status.textContent = 'Канал создан!';
    toast('Канал создан: ' + (r.username ? '@' + r.username : r.title || ''), 'ok');
    e.target.reset();
    OwnChannels.refresh();
  } catch (err) { status.textContent = ''; toast(err.message, 'error'); }
});
document.getElementById('form-post-channel')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = OwnChannels.accId();
  if (!id || !OwnChannels.selectedChannel) { toast('Выберите канал в таблице', 'error'); return; }
  const body = { text: e.target.text.value, format_type: 'md' };
  const f = e.target.media.files[0];
  if (f) {
    body.media_base64 = await fileToBase64(f);
    body.media_type = f.type.startsWith('video') ? 'video' : 'photo';
  }
  try {
    await API.post(`/accounts/${id}/channel/${OwnChannels.selectedChannel}/post`, body);
    toast('Опубликовано', 'ok');
    e.target.reset();
  } catch (err) { toast(err.message, 'error'); }
});
document.getElementById('btn-queue-post')?.addEventListener('click', async () => {
  const id = OwnChannels.accId();
  if (!id || !OwnChannels.selectedChannel) { toast('Выберите канал в таблице', 'error'); return; }
  const form = document.getElementById('form-post-channel');
  const body = { text: form.text.value, format_type: 'md' };
  const f = form.media.files[0];
  if (f) {
    body.media_base64 = await fileToBase64(f);
    body.media_type = f.type.startsWith('video') ? 'video' : 'photo';
  }
  try {
    await API.post(`/accounts/${id}/channel/${OwnChannels.selectedChannel}/queue`, body);
    toast('Добавлено в очередь', 'ok');
    form.reset();
    OwnChannels.loadQueue();
  } catch (err) { toast(err.message, 'error'); }
});
document.getElementById('form-generate-post')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const id = OwnChannels.accId();
  if (!id || !OwnChannels.selectedChannel) { toast('Выберите канал в таблице', 'error'); return; }
  const topic = e.target.topic.value.trim();
  if (!topic) { toast('Укажите тему', 'error'); return; }
  try {
    const r = await API.post(`/accounts/${id}/channel/${OwnChannels.selectedChannel}/generate-post`, { topic });
    toast('Сгенерировано и опубликовано', 'ok');
    e.target.reset();
  } catch (err) { toast(err.message, 'error'); }
});

// ============ BOOT ============

// ✅ NEW: Обновление таймаутов каждую секунду
function updateTimeoutBadges() {
  document.querySelectorAll('.timeout-badge').forEach(badge => {
    let remaining = parseInt(badge.dataset.remaining) || 0;
    if (remaining > 0) {
      remaining--;
      badge.dataset.remaining = remaining;
      const h = Math.floor(remaining / 3600);
      const m = Math.floor((remaining % 3600) / 60);
      const s = remaining % 60;
      let time = '';
      if (h > 0) {
        time = `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
      } else {
        time = `${m}:${String(s).padStart(2, '0')}`;
      }
      badge.textContent = `⏸ ${time}`;
      if (remaining <= 0) badge.remove();
    }
  });
}

// ✅ NEW: Smart check для rate limit'ов (каждые 60 сек)
async function smartRateLimitCheck() {
  try {
    const accounts = document.querySelectorAll('[data-account-id]');
    for (const el of accounts) {
      const accountId = el.dataset.accountId;
      if (!accountId) continue;
      
      try {
        const result = await API.get(`/accounts/${accountId}/rate-limit-check`);
        
        // Если был разблокирован - обновить список
        if (result.status === 'ready' && result.auto_restarted) {
          // Нежно обновить карточку после перезагрузки
          setTimeout(() => Accounts.refresh(), 2000);
        }
      } catch (e) {
        // Ошибка проверки - не кричим, просто пропускаем
      }
    }
  } catch (e) {
    // Silent fail
  }
}


async function boot() {
  await Settings.load();
  await Accounts.refresh();
  await refreshStatus();
  setInterval(refreshStatus, 5000);
  setInterval(updateTimeoutBadges, 1000); // ✅ обновление таймаутов
  setInterval(smartRateLimitCheck, 60000); // ✅ NEW: проверка rate limit каждые 60 сек
  setInterval(() => {
    if (document.querySelector('.tab-pane.active').id === 'tab-accounts') Accounts.refresh();
  }, 10000);
  Logs.startAuto();
  Dashboard.refresh();
  Dashboard.startAuto();
  // ленивая загрузка каналов при первом переключении на вкладку
}

(async () => {
  const ok = await Login.check();
  if (ok) boot();
})();
