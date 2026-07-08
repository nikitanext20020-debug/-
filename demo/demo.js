/* ===============================================================
   NEURO.CORE — DEMO engine (полностью виртуально, без бэкенда)
   Все данные фейковые, ничего никуда не отправляется.
   =============================================================== */

// ============ HELPERS ============
function escape(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function rand(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function randInt(a, b) { return Math.floor(Math.random() * (b - a + 1)) + a; }
function nowTime() {
  const d = new Date();
  return String(d.getHours()).padStart(2, '0') + ':' +
         String(d.getMinutes()).padStart(2, '0') + ':' +
         String(d.getSeconds()).padStart(2, '0');
}

// ============ TOAST ============
const toastEl = document.getElementById('toast');
function toast(msg, kind = '') {
  toastEl.className = 'toast show';
  if (kind) toastEl.classList.add(`toast-${kind}`);
  toastEl.textContent = msg;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toastEl.classList.remove('show'), 3000);
}

// ============ MOCK DATA ============
const ACCOUNTS = [
  { id: 1, phone: '+7 995 214-88-01', session: 'session_79952148801', running: true,  status: 'running',   comments: 1284, banned: 1, health: 'healthy' },
  { id: 2, phone: '+7 921 553-10-77', session: 'session_79215531077', running: true,  status: 'running',   comments: 967,  banned: 0, health: 'healthy' },
  { id: 3, phone: '+7 903 448-92-15', session: 'session_79034489215', running: true,  status: 'running',   comments: 1542, banned: 3, health: 'healthy' },
  { id: 4, phone: '+7 916 720-34-60', session: 'session_79167203460', running: false, status: 'spamblock', comments: 421,  banned: 7, health: 'spamblock' },
  { id: 5, phone: '+7 999 108-55-42', session: 'session_79991085542', running: true,  status: 'running',   comments: 733,  banned: 1, health: 'healthy' },
  { id: 6, phone: '+7 985 361-77-09', session: 'session_79853617709', running: false, status: 'frozen',    comments: 88,   banned: 0, health: 'warmup' },
  { id: 7, phone: '+7 927 640-22-31', session: 'session_79276402231', running: true,  status: 'running',   comments: 1105, banned: 2, health: 'healthy' },
  { id: 8, phone: '+7 962 815-49-93', session: 'session_79628154993', running: false, status: 'stopped',   comments: 356,  banned: 0, health: 'healthy' },
];

const CHANNELS = [
  { channel: 'tech_insider_ru', title: 'Tech Insider', subs: 184200, can_comment: true,  source: 'search',    last_checked: '2026-07-08 14:22' },
  { channel: 'crypto_daily',    title: 'Крипта Дейли', subs: 92800,  can_comment: true,  source: 'search',    last_checked: '2026-07-08 14:20' },
  { channel: 'startup_hub',     title: 'Стартап Хаб',  subs: 41500,  can_comment: true,  source: 'discovery', last_checked: '2026-07-08 14:19' },
  { channel: 'it_vacancies',    title: 'IT Вакансии',  subs: 210300, can_comment: true,  source: 'manual',    last_checked: '2026-07-08 14:18' },
  { channel: 'design_weekly',   title: 'Design Weekly',subs: 33900,  can_comment: false, source: 'search',    last_checked: '2026-07-08 13:59' },
  { channel: 'marketing_pro',   title: 'Маркетинг PRO',subs: 76200,  can_comment: true,  source: 'search',    last_checked: '2026-07-08 14:15' },
  { channel: 'finance_ru',      title: 'Финансы РУ',   subs: 156700, can_comment: true,  source: 'discovery', last_checked: '2026-07-08 14:10' },
  { channel: 'gamedev_talks',   title: 'GameDev Talks',subs: 28400,  can_comment: true,  source: 'search',    last_checked: '2026-07-08 14:08' },
  { channel: 'ai_frontier',     title: 'AI Frontier',  subs: 118900, can_comment: true,  source: 'search',    last_checked: '2026-07-08 14:21' },
  { channel: 'travel_notes',    title: 'Заметки Путешественника', subs: 54300, can_comment: true, source: 'discovery', last_checked: '2026-07-08 14:05' },
  { channel: 'science_pop',     title: 'Наука Популярно', subs: 88100, can_comment: true, source: 'search',   last_checked: '2026-07-08 14:12' },
  { channel: 'auto_news_ru',    title: 'Авто Новости', subs: 67500,  can_comment: false, source: 'search',    last_checked: '2026-07-08 13:47' },
  { channel: 'cook_everyday',   title: 'Готовим Каждый День', subs: 143200, can_comment: true, source: 'discovery', last_checked: '2026-07-08 14:17' },
  { channel: 'books_club',      title: 'Книжный Клуб', subs: 39600,  can_comment: true,  source: 'manual',    last_checked: '2026-07-08 14:02' },
  { channel: 'fitness_daily',   title: 'Фитнес Дейли', subs: 71800,  can_comment: true,  source: 'search',    last_checked: '2026-07-08 14:14' },
];

const COMMENTS = [
  'Полезный разбор, спасибо! Как раз искал что-то подобное.',
  'Согласен с автором, сам недавно с этим столкнулся.',
  'О, не знал что так можно. Надо попробовать на выходных.',
  'Актуально прям сейчас, забрал в закладки.',
  'Спорный момент, но в целом мысль здравая.',
  'Классный пост, ждём продолжения темы!',
  'А есть пример посложнее? Хотелось бы разобраться глубже.',
  'Пробовал этот подход — реально работает, подтверждаю.',
  'Топ контент, как всегда 🔝',
  'Интересно, а как это масштабируется на большие проекты?',
  'Спасибо, что делитесь. Многим будет полезно.',
  'Вот это поворот, не ожидал такого вывода.',
  'По опыту скажу — тут ещё важен второй момент.',
  'Отличное объяснение, наконец-то дошло 👍',
  'Сохранил, потом перечитаю внимательнее.',
];

const POSTS = [
  'опубликовал новый пост',
  'выложил разбор темы',
  'опубликовал новость дня',
  'выложил гайд',
  'запостил обзор',
];

// счётчики
let counters = {
  commentsToday: 342,
  commentsTotal: 48713,
  reactions: 1890,
  likes: 764,
  quick: 298,
  ok: 0, fail: 0,
};
let week = [220, 310, 285, 402, 366, 448, counters.commentsToday]; // Пн..Вс

// ============ TABS ============
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ============ ACCOUNTS ============
const Accounts = {
  render() {
    const grid = document.getElementById('accounts-grid');
    document.getElementById('m-accounts').textContent = ACCOUNTS.length;
    document.getElementById('m-running').textContent = ACCOUNTS.filter(a => a.running).length;
    document.getElementById('m-channels').textContent = CHANNELS.length;
    grid.innerHTML = ACCOUNTS.map(a => this.card(a)).join('');
    grid.querySelectorAll('[data-act]').forEach(b => {
      b.addEventListener('click', () => this.action(b.dataset.act, +b.dataset.id));
    });
  },
  card(a) {
    const status = a.running ? 'running' : a.status;
    return `
      <div class="account-card ${a.running ? 'running' : ''}">
        <div class="account-card-head">
          <div>
            <div class="account-phone">${escape(a.phone)}</div>
            <div class="muted small">${escape(a.session)}</div>
          </div>
          <span class="account-status-dot ${escape(status)}" title="${escape(status)}"></span>
        </div>
        <div class="account-meta"><span>Комментариев</span><span>${a.comments.toLocaleString('ru-RU')}</span></div>
        <div class="account-meta"><span>Забанен в каналах</span><span>${a.banned}</span></div>
        <div class="account-meta"><span>Health</span><span>${escape(a.health)}</span></div>
        <div class="account-actions">
          ${a.running
            ? `<button class="btn btn-ghost btn-sm" data-act="stop" data-id="${a.id}">Стоп</button>`
            : `<button class="btn btn-primary btn-sm" data-act="start" data-id="${a.id}">Старт</button>`}
          <button class="btn btn-ghost btn-sm" data-act="reset" data-id="${a.id}">Сбросить баны</button>
          <button class="btn btn-danger btn-sm" data-act="delete" data-id="${a.id}">Удалить</button>
        </div>
      </div>`;
  },
  action(act, id) {
    const a = ACCOUNTS.find(x => x.id === id);
    if (!a) return;
    if (act === 'start') { a.running = true; a.status = 'running'; toast(`Воркер ${a.phone} запущен`, 'ok'); }
    else if (act === 'stop') { a.running = false; a.status = 'stopped'; toast(`Воркер ${a.phone} остановлен`, 'ok'); }
    else if (act === 'reset') { a.banned = 0; toast('Баны сброшены', 'ok'); }
    else if (act === 'delete') {
      if (!confirm(`Удалить аккаунт ${a.phone}?`)) return;
      ACCOUNTS.splice(ACCOUNTS.indexOf(a), 1);
      toast('Аккаунт удалён', 'ok');
    }
    this.render();
    updateStatusBar();
  },
};
document.getElementById('btn-refresh-accounts').addEventListener('click', () => { Accounts.render(); toast('Обновлено', 'ok'); });

// add-account modal (демо: имитация импорта)
const modal = document.getElementById('modal-add-account');
document.getElementById('btn-add-account').addEventListener('click', () => modal.classList.remove('hidden'));
modal.querySelector('.modal-close').addEventListener('click', () => modal.classList.add('hidden'));
modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.add('hidden'); });
document.getElementById('form-import-session').addEventListener('submit', (e) => {
  e.preventDefault();
  const nextId = Math.max(0, ...ACCOUNTS.map(a => a.id)) + 1;
  const phone = '+7 9' + randInt(10, 99) + ' ' + randInt(100, 999) + '-' + randInt(10, 99) + '-' + randInt(10, 99);
  ACCOUNTS.push({ id: nextId, phone, session: 'session_' + phone.replace(/\D/g, ''), running: false, status: 'stopped', comments: 0, banned: 0, health: 'warmup' });
  toast('Сессия импортирована (демо)', 'ok');
  modal.classList.add('hidden');
  Accounts.render();
  updateStatusBar();
});

// ============ CHANNELS ============
const Channels = {
  render() {
    const q = (document.getElementById('channels-search').value || '').toLowerCase().trim();
    const tbody = document.getElementById('channels-tbody');
    document.getElementById('ch-total').textContent = CHANNELS.length;
    document.getElementById('ch-open').textContent = CHANNELS.filter(c => c.can_comment).length;
    let rows = CHANNELS;
    if (q) rows = rows.filter(c => c.channel.toLowerCase().includes(q) || c.title.toLowerCase().includes(q));
    if (!rows.length) { tbody.innerHTML = `<tr><td colspan="7" class="empty">Ничего не найдено</td></tr>`; return; }
    tbody.innerHTML = rows.map(c => `
      <tr>
        <td><a href="https://t.me/${escape(c.channel)}" target="_blank" rel="noopener">@${escape(c.channel)}</a></td>
        <td>${escape(c.title)}</td>
        <td class="muted">${c.subs.toLocaleString('ru-RU')}</td>
        <td>${c.can_comment ? '<span class="pill good">открыты</span>' : '<span class="pill bad">закрыты</span>'}</td>
        <td class="muted small">${escape(c.source)}</td>
        <td class="muted small">${escape(c.last_checked)}</td>
        <td><button class="btn btn-ghost btn-sm" data-del="${escape(c.channel)}">×</button></td>
      </tr>`).join('');
    tbody.querySelectorAll('[data-del]').forEach(b => {
      b.addEventListener('click', () => {
        const i = CHANNELS.findIndex(c => c.channel === b.dataset.del);
        if (i >= 0 && confirm(`Удалить @${b.dataset.del}?`)) { CHANNELS.splice(i, 1); this.render(); toast('Удалён', 'ok'); }
      });
    });
  },
};
document.getElementById('btn-refresh-channels').addEventListener('click', () => { Channels.render(); toast('Обновлено', 'ok'); });
document.getElementById('channels-search').addEventListener('input', () => Channels.render());
document.getElementById('btn-cleanup-channels').addEventListener('click', () => {
  if (!confirm('Удалить все каналы с закрытыми комментариями?')) return;
  for (let i = CHANNELS.length - 1; i >= 0; i--) if (!CHANNELS[i].can_comment) CHANNELS.splice(i, 1);
  Channels.render();
  toast('Закрытые каналы удалены', 'ok');
});

// ============ SETTINGS ============
document.addEventListener('change', (e) => {
  const el = e.target;
  if (!el.matches('[data-setting]') || el.type !== 'checkbox') return;
  const hint = document.getElementById('behavior-saved-hint');
  if (hint) { hint.textContent = '✓ Сохранено'; clearTimeout(hint._t); hint._t = setTimeout(() => hint.textContent = '', 1600); }
});
document.getElementById('global-pause-toggle').addEventListener('change', (e) => {
  document.getElementById('m-pause').textContent = e.target.checked ? 'ВКЛ' : 'выкл';
  toast(e.target.checked ? 'Глобальная пауза включена' : 'Пауза снята', 'ok');
});
document.getElementById('btn-save-settings').addEventListener('click', () => toast('Настройки сохранены', 'ok'));
document.getElementById('btn-test-ai').addEventListener('click', () => {
  const out = document.getElementById('settings-test-result');
  out.innerHTML = '<span class="muted">Проверяю…</span>';
  setTimeout(() => {
    out.innerHTML = `<span class="pill good">Нейросеть отвечает</span> <span class="muted small">google/gemini-3-flash-preview · 412 ms</span>`;
  }, 900);
});

// ============ LOGS ============
const LOG_TEMPLATES = [
  { lvl: 'info', msg: (c, ph) => `Комментарий отправлен в @${c}` },
  { lvl: 'info', msg: (c) => `Найден новый пост в @${c}, ставлю в очередь` },
  { lvl: 'info', msg: (c) => `Заглушка «_» отправлена в @${c} (быстрый режим)` },
  { lvl: 'info', msg: (c) => `Заглушка отредактирована в текст в @${c}` },
  { lvl: 'info', msg: (c) => `Реакция поставлена на пост @${c}` },
  { lvl: 'info', msg: (c) => `Вступил в обсуждение @${c}` },
  { lvl: 'warning', msg: (c) => `@${c}: комментарии закрыты, пропускаю` },
  { lvl: 'warning', msg: () => `PeerFloodError → иду в @SpamBot` },
  { lvl: 'info', msg: () => `@SpamBot: мягкий блок снят, продолжаю` },
];
const logs = [];
const Logs = {
  seed() {
    for (let i = 0; i < 40; i++) this.push(true);
    this.render();
  },
  push(silent) {
    const t = rand(LOG_TEMPLATES);
    const c = rand(CHANNELS).channel;
    const acc = rand(ACCOUNTS);
    logs.push({ time: nowTime(), level: t.lvl, acc: acc ? acc.id : null, message: t.msg(c) });
    if (logs.length > 300) logs.shift();
    if (!silent && document.getElementById('logs-autorefresh').checked) this.render();
  },
  render() {
    const c = document.getElementById('logs-container');
    const lvl = document.getElementById('logs-level').value;
    let list = logs;
    if (lvl) list = list.filter(l => l.level === lvl);
    c.innerHTML = list.slice().reverse().map(l => `
      <div class="log-line">
        <span class="log-time">${escape(l.time)}</span>
        <span class="log-level ${escape(l.level)}">${escape(l.level)}</span>
        <span class="log-acc">${l.acc != null ? 'acc:' + l.acc : 'sys'}</span>
        <span class="log-msg">${escape(l.message)}</span>
      </div>`).join('');
  },
};
document.getElementById('btn-refresh-logs').addEventListener('click', () => Logs.render());
document.getElementById('logs-level').addEventListener('change', () => Logs.render());

// ============ STATUS BAR ============
function updateStatusBar() {
  const running = ACCOUNTS.filter(a => a.running).length;
  document.querySelector('#status-workers .status-label').textContent = `${running}/${ACCOUNTS.length} workers`;
  document.getElementById('m-active').textContent = running;
  document.getElementById('m-live-channels').textContent = CHANNELS.filter(c => c.can_comment).length;
}

// ============ COUNTERS + CHART ============
function renderCounters() {
  document.getElementById('m-comments-today').textContent = counters.commentsToday.toLocaleString('ru-RU');
  document.getElementById('m-comments-total').textContent = counters.commentsTotal.toLocaleString('ru-RU');
  document.getElementById('s-reactions').textContent = counters.reactions.toLocaleString('ru-RU');
  document.getElementById('s-likes').textContent = counters.likes.toLocaleString('ru-RU');
  document.getElementById('s-quick').textContent = counters.quick.toLocaleString('ru-RU');
  const total = counters.ok + counters.fail;
  document.getElementById('s-rate').textContent = total ? Math.round(counters.ok / total * 100) + '%' : '98%';
}
function renderChart() {
  const days = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  const max = Math.max(...week) * 1.1;
  document.getElementById('chart-bars').innerHTML = week.map((v, i) => `
    <div class="chart-col">
      <div class="chart-bar" style="height:${Math.max(4, v / max * 100)}%" title="${v}"></div>
      <div class="chart-x">${days[i]}</div>
    </div>`).join('');
}

// ============ LIVE FEED ENGINE ============
const feedList = document.getElementById('feed-list');

function channelInitials(title) {
  return title.split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
}

function addFeedComment() {
  const ch = rand(CHANNELS.filter(c => c.can_comment));
  const acc = rand(ACCOUNTS.filter(a => a.running));
  if (!ch || !acc) return;
  const quick = document.querySelector('[data-setting="quick_comment_mode"]').checked;
  const asChannel = document.querySelector('[data-setting="comment_as_channel"]').checked;
  const text = rand(COMMENTS);

  const item = document.createElement('div');
  item.className = 'feed-item is-new';
  const author = asChannel && Math.random() > 0.4
    ? `от имени <b>@neuro_news</b>`
    : `аккаунт <b>${escape(acc.phone)}</b>`;

  item.innerHTML = `
    <div class="feed-avatar">${escape(channelInitials(ch.title))}</div>
    <div class="feed-body">
      <div class="feed-top">
        <span class="feed-channel">@${escape(ch.channel)}</span>
        <span class="feed-time">${nowTime()}</span>
      </div>
      <div class="feed-action">${author} · ${rand(POSTS)} · комментирует</div>
      <div class="feed-comment ${quick ? 'stub' : ''}">${quick ? '_<span class="cursor"></span>' : escape(text)}</div>
      <div class="feed-tags"></div>
    </div>`;

  feedList.prepend(item);
  while (feedList.children.length > 40) feedList.removeChild(feedList.lastChild);
  setTimeout(() => item.classList.remove('is-new'), 1500);

  const tags = item.querySelector('.feed-tags');
  const commentEl = item.querySelector('.feed-comment');

  // реакция (иногда)
  if (document.querySelector('[data-setting="react_to_posts"]').checked && Math.random() > 0.5) {
    tags.innerHTML += `<span class="feed-tag info"><span class="tdot"></span>реакция</span>`;
    counters.reactions++;
  }

  if (quick) {
    tags.innerHTML += `<span class="feed-tag warn"><span class="tdot"></span>заглушка «_»</span>`;
    counters.quick++;
    // редактируем заглушку в текст
    setTimeout(() => {
      commentEl.classList.remove('stub');
      commentEl.textContent = text;
      tags.innerHTML += `<span class="feed-tag ok"><span class="tdot"></span>отредактировано в текст</span>`;
      finalizeComment(ch, acc, tags, asChannel);
    }, randInt(700, 1500));
  } else {
    finalizeComment(ch, acc, tags, asChannel);
  }
}

function finalizeComment(ch, acc, tags, asChannel) {
  tags.innerHTML += `<span class="feed-tag ok"><span class="tdot"></span>отправлено</span>`;
  if (asChannel) tags.innerHTML += `<span class="feed-tag info"><span class="tdot"></span>send as канал</span>`;
  if (document.querySelector('[data-setting="like_other_comments"]').checked && Math.random() > 0.6) {
    tags.innerHTML += `<span class="feed-tag"><span class="tdot"></span>лайк чужого коммента</span>`;
    counters.likes++;
  }
  acc.comments++;
  counters.commentsToday++;
  counters.commentsTotal++;
  counters.ok++;
  week[6] = counters.commentsToday;
  Logs.push();
  renderCounters();
  renderChart();
  updateStatusBar();
}

function tick() {
  if (document.getElementById('global-pause-toggle').checked) { scheduleTick(); return; }
  const r = Math.random();
  if (r > 0.15) {
    addFeedComment();
  } else {
    // редкое событие — флуд/спамблок
    const acc = rand(ACCOUNTS.filter(a => a.running));
    if (acc) {
      const item = document.createElement('div');
      item.className = 'feed-item is-new';
      item.innerHTML = `
        <div class="feed-avatar" style="background:linear-gradient(135deg,var(--warn),#ff7a45)">!</div>
        <div class="feed-body">
          <div class="feed-top"><span class="feed-channel">${escape(acc.phone)}</span><span class="feed-time">${nowTime()}</span></div>
          <div class="feed-action">PeerFloodError — обращаюсь к <b>@SpamBot</b></div>
          <div class="feed-tags">
            <span class="feed-tag warn"><span class="tdot"></span>флуд-лимит</span>
            <span class="feed-tag ok"><span class="tdot"></span>мягкий блок снят</span>
          </div>
        </div>`;
      feedList.prepend(item);
      while (feedList.children.length > 40) feedList.removeChild(feedList.lastChild);
      setTimeout(() => item.classList.remove('is-new'), 1500);
      Logs.push();
    }
  }
  scheduleTick();
}
function scheduleTick() {
  setTimeout(tick, randInt(1400, 3200));
}

// ============ BOOT ============
Accounts.render();
Channels.render();
Logs.seed();
updateStatusBar();
renderCounters();
renderChart();
// первые события сразу, чтобы лента не была пустой
for (let i = 0; i < 6; i++) setTimeout(addFeedComment, i * 250);
scheduleTick();
setInterval(() => { Logs.push(); }, 5000);
