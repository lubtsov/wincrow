/* Слоты «Сочный шторм»: 6×5, скопления от восьми символов, каскады.

   Клиент здесь — проигрыватель. Спин целиком считает сервер (games/storm.py) и
   присылает раскадровку: сетку до каждого удаления, скопления и итоговый
   множитель. Поэтому анимация не может «случайно» показать другой результат, а
   подкрутить исход в JS нечем — его тут просто не вычисляют.

   У каждого спина есть свой id (uuid). Если запрос оборвался, он уходит второй
   раз с тем же id, и сервер отдаёт прежний результат вместо новой ставки. */

(function () {
  const el = {};
  let conf = null;
  let bet = 10;
  let busy = false;
  let cells = [];                 // cells[col][row] — узлы поля
  let reelTimers = [];

  const BIG_WIN = 15;             // с этого множителя показываем крупный выигрыш
  const FALLBACK = ['🍒', '🍋', '🍊', '🥝', '🍇', '🍉', '⭐', '💎'];

  function grab() {
    el.title = document.getElementById('slot-title');
    el.sub = document.getElementById('slot-sub');
    el.reels = document.getElementById('reels');
    el.flash = document.getElementById('win-flash');
    el.badge = document.getElementById('win-badge');
    el.spin = document.getElementById('spin');
    el.betLabel = document.getElementById('bet-label');
    el.chips = document.getElementById('bet-chips');
    el.log = document.getElementById('spin-log');
  }

  function uuid() {
    try { return crypto.randomUUID(); } catch (e) { /* старый webview */ }
    return 'sp-' + Date.now().toString(36) + '-' +
           Math.random().toString(36).slice(2, 10);
  }

  function emojiOf(key) {
    if (!key) return '';
    if (key.charAt(0) === 'x') return '🌪';
    const found = (conf && conf.paytable || []).filter(function (s) {
      return s.key === key;
    })[0];
    return found ? found.emoji : '❔';
  }

  /* --- поле ------------------------------------------------------------- */

  function buildGrid() {
    const cols = conf.cols, rows = conf.rows;
    el.reels.style.setProperty('--cols', cols);
    el.reels.innerHTML = '';
    cells = [];
    for (let c = 0; c < cols; c++) cells.push([]);
    // Порядок в DOM — построчный: так CSS-сетка раскладывает сама.
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const cell = document.createElement('div');
        cell.className = 'cell';
        cell.innerHTML = '<span class="sym"></span><span class="mult"></span>';
        el.reels.appendChild(cell);
        cells[c][r] = cell;
      }
    }
  }

  function setCell(cell, key) {
    const sym = cell.querySelector('.sym');
    const mult = cell.querySelector('.mult');
    sym.textContent = emojiOf(key);
    const storm = key && key.charAt(0) === 'x';
    cell.classList.toggle('storm', !!storm);
    mult.textContent = storm ? '×' + key.slice(1) : '';
  }

  function paint(grid, drop) {
    for (let c = 0; c < conf.cols; c++) {
      for (let r = 0; r < conf.rows; r++) {
        const cell = cells[c][r];
        cell.classList.remove('pop', 'hit');
        setCell(cell, grid[c][r]);
        if (drop) {
          cell.classList.remove('drop');
          void cell.offsetWidth;
          cell.style.setProperty('--delay', (r * 40 + c * 18) + 'ms');
          cell.classList.add('drop');
        }
      }
    }
  }

  function markWinners(clusters) {
    clusters.forEach(function (cluster) {
      cluster.cells.forEach(function (pair) {
        cells[pair[0]][pair[1]].classList.add('hit');
      });
    });
  }

  function popWinners(clusters) {
    clusters.forEach(function (cluster) {
      cluster.cells.forEach(function (pair) {
        const cell = cells[pair[0]][pair[1]];
        cell.classList.remove('hit');
        cell.classList.add('pop');
      });
    });
  }

  /* --- крутка ----------------------------------------------------------- */

  function randomKey() {
    const table = conf && conf.paytable;
    if (!table || !table.length) {
      return FALLBACK[Math.floor(Math.random() * FALLBACK.length)];
    }
    return table[Math.floor(Math.random() * table.length)].key;
  }

  function startReels() {
    stopReels();
    el.reels.classList.add('spinning');
    for (let c = 0; c < conf.cols; c++) {
      (function (col) {
        reelTimers.push(setInterval(function () {
          for (let r = 0; r < conf.rows; r++) setCell(cells[col][r], randomKey());
        }, 70 + col * 6));
      })(c);
    }
  }

  function stopReels() {
    reelTimers.forEach(clearInterval);
    reelTimers = [];
    el.reels.classList.remove('spinning');
  }

  async function landReels(grid) {
    // Колонки замирают слева направо — так крутка читается как настоящая.
    for (let c = 0; c < conf.cols; c++) {
      clearInterval(reelTimers[c]);
      for (let r = 0; r < conf.rows; r++) {
        const cell = cells[c][r];
        setCell(cell, grid[c][r]);
        cell.classList.remove('drop');
        void cell.offsetWidth;
        cell.style.setProperty('--delay', r * 30 + 'ms');
        cell.classList.add('drop');
      }
      WC.impact('light');
      await WC.wait(90);
    }
    stopReels();
  }

  /* --- счётчик выигрыша ------------------------------------------------- */

  function showBadge(text, cls) {
    el.badge.hidden = false;
    el.badge.className = 'win-badge ' + (cls || '');
    el.badge.textContent = text;
  }

  function hideBadge() {
    el.badge.hidden = true;
    el.badge.textContent = '';
  }

  function flash(strength) {
    el.flash.className = 'win-flash on' + (strength ? ' ' + strength : '');
    setTimeout(function () { el.flash.className = 'win-flash'; }, 460);
  }

  /* --- каскады ---------------------------------------------------------- */

  async function playCascades(spin) {
    let won = 0;
    for (let i = 0; i < spin.steps.length; i++) {
      const step = spin.steps[i];
      paint(step.grid, i > 0);
      await WC.wait(i ? 260 : 120);

      markWinners(step.clusters);
      flash(step.win >= 5 ? 'strong' : '');
      WC.impact('medium');
      won += step.win;
      showBadge(WC.money(Math.round(won * bet)) +
                (spin.steps.length > 1 ? '  ·  каскад ' + (i + 1) : ''), 'live');
      await WC.wait(520);

      popWinners(step.clusters);
      await WC.wait(260);

      const next = spin.steps[i + 1] ? spin.steps[i + 1].grid : spin.grid;
      paint(next, true);
      await WC.wait(300);
    }
    if (!spin.steps.length) paint(spin.grid, false);
    return won;
  }

  async function applyStorms(spin, base) {
    if (!spin.storm_total || base <= 0) return;
    spin.storms.forEach(function (storm) {
      cells[storm.cell[0]][storm.cell[1]].classList.add('boom');
    });
    showBadge('шторм ×' + spin.storm_total, 'storm');
    WC.impact('heavy');
    await WC.wait(900);
    spin.storms.forEach(function (storm) {
      cells[storm.cell[0]][storm.cell[1]].classList.remove('boom');
    });
  }

  async function bigWin(spin) {
    const box = document.getElementById('bigwin');
    const cap = spin.multiplier >= 100 ? 'ШТОРМ!'
              : spin.multiplier >= 40 ? 'МЕГА ВЫИГРЫШ' : 'БОЛЬШОЙ ВЫИГРЫШ';
    document.getElementById('bigwin-cap').textContent = cap;
    document.getElementById('bigwin-mult').textContent = '×' + spin.multiplier;
    const sum = document.getElementById('bigwin-sum');
    box.hidden = false;
    requestAnimationFrame(function () { box.classList.add('on'); });
    WC.haptic('success');
    WC.confetti(26);

    const target = spin.payout_cents;
    const started = performance.now();
    await new Promise(function (done) {
      const tick = function (now) {
        const k = Math.min(1, (now - started) / 900);
        sum.textContent = WC.money(Math.round(target * (1 - Math.pow(1 - k, 3))));
        if (k < 1) requestAnimationFrame(tick); else done();
      };
      requestAnimationFrame(tick);
    });
    await WC.wait(1100);
    box.classList.remove('on');
    setTimeout(function () { box.hidden = true; }, 260);
  }

  /* --- спин ------------------------------------------------------------- */

  function postSpin(spinId) {
    const payload = { bet_cents: bet, spin_id: spinId };
    return WC.api('/api/slots/spin', payload).catch(function (err) {
      // Сервер не ответил вовсе — пробуем ещё раз С ТЕМ ЖЕ id. Если первый
      // запрос всё-таки дошёл, вернётся прежний результат, а не новая ставка.
      if (err.status) throw err;
      return WC.wait(700).then(function () {
        return WC.api('/api/slots/spin', payload);
      });
    });
  }

  async function spin() {
    if (busy || !conf) return;
    busy = true;
    el.spin.classList.add('busy');
    el.spin.disabled = true;
    hideBadge();
    WC.impact('medium');
    startReels();

    const started = performance.now();
    let data;
    try {
      data = await postSpin(uuid());
    } catch (err) {
      stopReels();
      showBadge(WC.errorText(err), 'bad');
      release();
      return;
    }

    if (data.status !== 'ok' && data.status !== 'repeat') {
      stopReels();
      const message = data.status === 'no_money' ? 'Не хватает на ставку'
                    : data.status === 'bad_bet' ? 'Ставка вне лимитов'
                    : 'Спин ещё считается, повтори';
      showBadge(message, 'bad');
      WC.haptic('error');
      WC.setBalance(data.balance);
      release();
      return;
    }

    // Крутка должна быть видна, даже если сервер ответил моментально.
    await WC.wait(Math.max(0, 620 - (performance.now() - started)));
    WC.setBalance(data.balance);
    await finishSpin(data);
    release();
  }

  function release() {
    busy = false;
    el.spin.classList.remove('busy');
    el.spin.disabled = false;
  }

  async function finishSpin(data) {
    const spin = data.spin;
    renderLog(data.history);
    if (!spin || !spin.grid || !spin.grid.length) {   // сид сменили, кадров нет
      stopReels();
      showBadge(spin && spin.payout_cents ? 'Выигрыш ' + spin.win : 'Без выигрыша',
                spin && spin.payout_cents ? 'good' : '');
      return;
    }

    const first = spin.steps.length ? spin.steps[0].grid : spin.grid;
    await landReels(first);

    const base = await playCascades(spin);
    await applyStorms(spin, base);

    if (spin.payout_cents > 0) {
      showBadge('+' + spin.win + '   ×' + spin.multiplier, 'good');
      flash(spin.multiplier >= BIG_WIN ? 'strong' : '');
      WC.setBalance(data.balance, true);
      if (spin.multiplier >= BIG_WIN) await bigWin(spin);
      else { WC.haptic('success'); WC.confetti(10); }
    } else {
      showBadge('Без выигрыша', '');
      WC.setBalance(data.balance);
    }
  }

  /* --- ставка, история, выплаты ----------------------------------------- */

  function clampBet(cents) {
    return Math.max(conf.min_bet, Math.min(conf.max_bet, cents));
  }

  function setBet(cents, remember) {
    bet = clampBet(Math.round(cents / 10) * 10);
    el.betLabel.textContent = WC.money(bet);
    Array.prototype.forEach.call(el.chips.children, function (chip) {
      chip.classList.toggle('on', Number(chip.dataset.bet) === bet);
    });
    if (remember !== false) {
      try { localStorage.setItem('wc.bet', String(bet)); } catch (e) { /* ok */ }
    }
  }

  function renderChips() {
    el.chips.innerHTML = '';
    conf.bets.forEach(function (cents) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.dataset.bet = cents;
      chip.textContent = WC.money(cents);
      chip.addEventListener('click', function () {
        WC.impact('light');
        setBet(cents);
      });
      el.chips.appendChild(chip);
    });
  }

  function renderLog(history) {
    if (!history || !history.length) {
      el.log.innerHTML = '<p class="empty">Пока ни одного спина.</p>';
      return;
    }
    el.log.innerHTML = history.map(function (row) {
      const won = row.payout_cents > 0;
      return '<div class="log-row' + (won ? ' won' : '') + '">' +
             '<span class="log-bet">' + WC.esc(row.bet) + '</span>' +
             '<span class="log-mult">×' + row.multiplier.toFixed(2) + '</span>' +
             '<span class="log-win">' + (won ? '+' + WC.esc(row.win) : '—') +
             '</span></div>';
    }).join('');
  }

  function paytableSheet() {
    const rows = conf.paytable.slice().reverse().map(function (s) {
      return '<div class="pay-row"><span class="pay-sym">' + s.emoji + '</span>' +
             '<span class="pay-name">' + WC.esc(s.title) + '</span>' +
             '<span class="pay-nums">' +
             s.pays.map(function (v) { return '×' + v; }).join(' / ') +
             '</span></div>';
    }).join('');
    WC.sheet(
      '<h2>' + WC.esc(conf.title) + '</h2>' +
      '<p class="sub">Платят <b>скопления</b>: ' + conf.cluster + '+ одинаковых ' +
      'символов в любых местах поля, линий нет. Выигрышные исчезают, верхние ' +
      'падают вниз — и каскад может продолжиться.</p>' +
      '<div class="pay-head"><span></span><span></span>' +
      '<span class="pay-nums">' + conf.cluster + '–9 / 10–11 / 12+</span></div>' +
      '<div class="pays">' + rows + '</div>' +
      '<div class="pay-row storm-row"><span class="pay-sym">🌪</span>' +
      '<span class="pay-name">Шторм</span>' +
      '<span class="pay-nums">×2…×25 к выигрышу</span></div>' +
      '<p class="sub">Штормы не входят в скопления и остаются на поле до конца ' +
      'спина. Если спин что-то заплатил, их сумма умножает весь выигрыш.</p>' +
      '<p class="sub">Отдача — ' + Math.round(conf.rtp * 100) + '%, максимум ×' +
      conf.max_multiplier + ' за спин. Результат считает сервер по ' +
      'provably fair: сид спина виден в профиле.</p>');
  }

  /* --- вход ------------------------------------------------------------- */

  let wired = false;

  function wire() {
    if (wired) return;
    wired = true;
    el.spin.addEventListener('click', spin);
    document.getElementById('bet-minus').addEventListener('click', function () {
      WC.impact('light');
      setBet(bet - 10);
    });
    document.getElementById('bet-plus').addEventListener('click', function () {
      WC.impact('light');
      setBet(bet + 10);
    });
    document.getElementById('rules-open').addEventListener('click', paytableSheet);
  }

  async function load() {
    try {
      conf = await WC.api('/api/slots/state');
    } catch (err) {
      el.log.innerHTML = '<p class="empty">' + WC.esc(WC.errorText(err)) + '</p>';
      return;
    }
    el.title.textContent = conf.title;
    el.sub.textContent = conf.cols + '×' + conf.rows + ' · скопления от ' +
                         conf.cluster + ' · каскады · отдача ' +
                         Math.round(conf.rtp * 100) + '%';
    WC.setBalance(conf.balance);

    let saved = 0;
    try { saved = Number(localStorage.getItem('wc.bet')) || 0; } catch (e) { /* ok */ }
    renderChips();
    setBet(saved || conf.bets[0] || conf.min_bet, false);
    buildGrid();
    paint(emptyGrid(), true);
    renderLog(conf.history);
    wire();
  }

  function emptyGrid() {
    const grid = [];
    for (let c = 0; c < conf.cols; c++) {
      grid.push([]);
      for (let r = 0; r < conf.rows; r++) grid[c].push(randomKey());
    }
    return grid;
  }

  WC.register('slots', {
    open: function () {
      grab();
      if (!conf) { load(); return; }
      WC.setBalance(conf.balance);
    },
    refresh: async function () {
      if (busy || !conf) return;
      try {
        const fresh = await WC.api('/api/slots/state');
        conf.balance = fresh.balance;
        WC.setBalance(fresh.balance);
        renderLog(fresh.history);
      } catch (e) { /* обновление баланса не критично */ }
    },
  });
})();
