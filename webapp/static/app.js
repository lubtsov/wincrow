/* Оболочка Mini App: подпись, запросы, вкладки и общие мелочи.

   Экранов три (слоты, кейс, профиль), и все они живут в одной странице: разные
   URL здесь ни к чему, а перезагрузка внутри Telegram выглядит как мигание.
   Каждый экран — отдельный файл, который регистрируется в WC.register().

   Ни один экран не считает деньги. Всё, что связано с балансом, приезжает от
   сервера уже посчитанным (webapp/server.py), а клиент это рисует. */

window.WC = {
  tg: window.Telegram && window.Telegram.WebApp,
  initData: (window.Telegram && window.Telegram.WebApp
             && window.Telegram.WebApp.initData) || '',
  views: {},
  active: null,

  /* --- запросы ---------------------------------------------------------- */

  api: function (path, payload) {
    return fetch(path, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'tma ' + WC.initData,
      },
      body: JSON.stringify(Object.assign({ initData: WC.initData }, payload || {})),
    }).then(async function (response) {
      let data = {};
      try { data = await response.json(); } catch (e) { data = { error: 'bad-json' }; }
      if (!response.ok) {
        const err = new Error(data.error || String(response.status));
        err.code = data.error;
        err.status = response.status;
        throw err;
      }
      return data;
    });
  },

  errorText: function (err) {
    const known = {
      'no-init-data': 'Telegram не передал подпись — открой приложение из бота заново.',
      'bad-init-data': 'Подпись не сошлась. Закрой и открой приложение заново.',
      'init-data-expired': 'Экран открыт слишком давно. Закрой и открой заново.',
      'banned': 'Доступ к боту закрыт.',
      'bad-request': 'Сервер не понял запрос.',
    };
    return known[err && err.code] || 'Сервер не ответил. Попробуй ещё раз.';
  },

  /* --- мелочи ----------------------------------------------------------- */

  wait: function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); },

  haptic: function (kind) {
    try { WC.tg.HapticFeedback.notificationOccurred(kind); } catch (e) { /* не критично */ }
  },

  impact: function (kind) {
    try { WC.tg.HapticFeedback.impactOccurred(kind); } catch (e) { /* не критично */ }
  },

  esc: function (text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },

  money: function (cents) {
    const sign = cents < 0 ? '-' : '';
    const abs = Math.abs(Math.round(cents));
    return sign + '$' + Math.floor(abs / 100) + '.' + String(abs % 100).padStart(2, '0');
  },

  clock: function (seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const pad = function (n) { return String(n).padStart(2, '0'); };
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? h + ':' + pad(m) + ':' + pad(s) : pad(m) + ':' + pad(s);
  },

  setBalance: function (text, bump) {
    const wallet = document.querySelector('.wallet');
    document.getElementById('balance').textContent = text;
    if (!bump) return;
    wallet.classList.remove('bump');
    void wallet.offsetWidth;                 // перезапуск CSS-анимации
    wallet.classList.add('bump');
  },

  tile: function (title, text, cls) {
    return '<div class="tile ' + (cls || '') + '"><h2>' + title + '</h2><p>' +
           text + '</p></div>';
  },

  confetti: function (count) {
    const box = document.createElement('div');
    box.className = 'confetti';
    const colors = ['#ffcf5c', '#7c5cff', '#4ade80', '#ff9f43', '#f2607d'];
    const total = count || 14;
    for (let i = 0; i < total; i++) {
      const piece = document.createElement('i');
      const angle = (Math.PI * 2 * i) / total;
      const spread = 120 + Math.random() * 90;
      piece.style.setProperty('--dx', Math.cos(angle) * spread + 'px');
      piece.style.setProperty('--dy', Math.sin(angle) * spread + 'px');
      piece.style.background = colors[i % colors.length];
      piece.style.animationDelay = (i % 5) * 0.04 + 's';
      box.appendChild(piece);
    }
    document.body.appendChild(box);
    setTimeout(function () { box.remove(); }, 1500);
  },

  /* --- нижний лист (правила, выплаты) ----------------------------------- */

  sheet: function (html) {
    const sheet = document.getElementById('sheet');
    document.getElementById('sheet-body').innerHTML =
      '<button class="sheet-close" type="button" aria-label="Закрыть">✕</button>' + html;
    sheet.hidden = false;
    requestAnimationFrame(function () { sheet.classList.add('open'); });
  },

  closeSheet: function () {
    const sheet = document.getElementById('sheet');
    sheet.classList.remove('open');
    setTimeout(function () { sheet.hidden = true; }, 220);
  },

  /* --- заметная ошибка ---------------------------------------------------- */

  banner: function (text, kind) {
    /* Плашка поверх шапки. Нужна потому, что «ничего не произошло» — худшее,
       что может показать приложение: отказ сервера должен быть виден сразу, а не
       мелкой строкой где-то внизу экрана. */
    const box = document.getElementById('banner');
    if (!box) return;
    box.className = 'banner ' + (kind || 'bad');
    box.textContent = text;
    box.hidden = false;
    clearTimeout(WC._bannerTimer);
    WC._bannerTimer = setTimeout(function () { box.hidden = true; }, 4200);
  },

  /* --- вкладки ---------------------------------------------------------- */

  register: function (name, view) { WC.views[name] = view; },

  tab: function (name) {
    if (!WC.views[name]) return;
    // Уходящий экран может держать таймеры (живой множитель краша) — гасим.
    const leaving = WC.active && WC.active !== name && WC.views[WC.active];
    if (leaving && leaving.leave) leaving.leave();
    WC.active = name;
    Object.keys(WC.views).forEach(function (key) {
      document.getElementById('view-' + key).hidden = key !== name;
    });
    Array.prototype.forEach.call(document.querySelectorAll('#tabs button'),
      function (button) {
        button.classList.toggle('on', button.dataset.tab === name);
      });
    try { localStorage.setItem('wc.tab', name); } catch (e) { /* приватный режим */ }
    WC.views[name].open();
  },

  /* --- старт ------------------------------------------------------------ */

  boot: function () {
    const tg = WC.tg;
    if (tg) {
      tg.ready();
      tg.expand();
      try {
        tg.setHeaderColor('#0b0d17');
        tg.setBackgroundColor('#0b0d17');
      } catch (e) { /* старый клиент */ }
      if (tg.BackButton) { tg.BackButton.hide(); }
      if (tg.disableVerticalSwipes) { tg.disableVerticalSwipes(); }
    }

    document.getElementById('tabs').addEventListener('click', function (event) {
      const button = event.target.closest('button[data-tab]');
      if (!button) return;
      WC.impact('light');
      WC.tab(button.dataset.tab);
    });

    document.getElementById('sheet').addEventListener('click', function (event) {
      if (event.target.id === 'sheet' || event.target.closest('.sheet-close')) {
        WC.closeSheet();
      }
    });

    if (!WC.initData) {
      document.getElementById('view-slots').hidden = false;
      document.getElementById('reels').innerHTML = '';
      document.getElementById('spin-log-tile').innerHTML = WC.tile('Нужен Telegram',
        'Это приложение казино: игрока он узнаёт по подписи Telegram. Открой ' +
        'его кнопкой снизу в боте — или играй прямо в боте.', 'error');
      document.getElementById('spin').disabled = true;
      return;
    }

    let start = 'slots';
    // Экран из адреса: кнопки в боте ведут прямо на нужную вкладку
    // (`...#slots`, `...#case`), и после нажатия искать её не надо. Якорь
    // главнее сохранённой вкладки — игрок только что выбрал, куда идёт.
    const asked = (location.hash || '').replace(/^#/, '');
    if (asked && WC.views[asked]) {
      start = asked;
    } else {
      try {
        const saved = localStorage.getItem('wc.tab');
        if (saved && WC.views[saved]) start = saved;
      } catch (e) { /* приватный режим */ }
    }
    WC.tab(start);

    // Возврат из свёрнутого состояния: состояние могло устареть.
    document.addEventListener('visibilitychange', function () {
      if (document.hidden || !WC.active) return;
      const view = WC.views[WC.active];
      if (view && view.refresh) view.refresh();
    });
  },
};
