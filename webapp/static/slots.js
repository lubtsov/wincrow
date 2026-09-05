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
        cell.classList.remove('pop', 'hit', 'fall', 'land');
        dropShards(cell);
        clearShift(cell);
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

  function clearShift(cell) {
    // Каскад двигает клетки инлайновым transform — после него не должно
    // остаться ни сдвига, ни задержки, ни прозрачности, иначе клетка застынет
    // мимо сетки или тронется не вовремя.
    cell.style.transition = '';
    cell.style.transitionDelay = '';
    cell.style.transform = '';
    cell.style.opacity = '';
  }

  function dropShards(cell) {
    const bits = cell.querySelectorAll('.shard');
    Array.prototype.forEach.call(bits, function (bit) {
      if (bit.parentNode) bit.parentNode.removeChild(bit);
    });
    // Цвет взрыва принадлежал прошлому символу — вместе с осколками уходит и он.
    cell.style.removeProperty('--shard');
  }

  function markWinners(clusters) {
    clusters.forEach(function (cluster) {
      cluster.cells.forEach(function (pair) {
        cells[pair[0]][pair[1]].classList.add('hit');
      });
    });
  }

  // Цвет осколков по символу: взрыв должен читаться как «лопнула вишня», а не
  // как безымянная вспышка. Ключи — те же, что в games/storm.py SYMBOLS.
  const SHARD_COLORS = {
    cherry: '#ff5a7a', lemon: '#ffe066', orange: '#ff9f43', kiwi: '#8ed64a',
    grape: '#b06cff', melon: '#ff6b8a', star: '#ffcf5c', gem: '#6be3ff',
  };

  function shards(cell, key) {
    /* Восемь брызг из центра клетки. Направление считается здесь, а не в CSS:
       восемь разных векторов правилом не описать, а без разлёта символ не
       взрывается, а просто исчезает. Цвет уезжает и на саму клетку — им же
       красятся вспышка и ударная волна. */
    const color = SHARD_COLORS[key] || 'rgba(255, 207, 92, .95)';
    const size = cell.offsetWidth || 48;
    cell.style.setProperty('--shard', color);
    for (let i = 0; i < 8; i++) {
      const bit = document.createElement('i');
      const angle = (i / 8) * Math.PI * 2 + Math.random() * 0.6;
      const reach = size * (0.7 + Math.random() * 0.6);
      bit.className = 'shard';
      bit.style.setProperty('--dx', Math.round(Math.cos(angle) * reach) + 'px');
      bit.style.setProperty('--dy', Math.round(Math.sin(angle) * reach) + 'px');
      bit.style.setProperty('--shard', color);
      bit.style.animationDelay = Math.round(Math.random() * 70) + 'ms';
      cell.appendChild(bit);
    }
    setTimeout(function () { dropShards(cell); }, 700);
  }

  function clusterWin(cluster) {
    /* Сумма скопления всплывает над ним: без неё лопнувшие клетки выглядят как
       подмена поля, и непонятно, что это вообще был выигрыш. */
    const wrap = el.reels.parentNode;
    const box = wrap.getBoundingClientRect();
    let x = 0, y = 0;
    cluster.cells.forEach(function (pair) {
      const spot = cells[pair[0]][pair[1]].getBoundingClientRect();
      x += spot.left + spot.width / 2 - box.left;
      y += spot.top + spot.height / 2 - box.top;
    });
    const tag = document.createElement('b');
    tag.className = 'cluster-win';
    tag.textContent = '+' + WC.money(Math.round(cluster.win * bet));
    tag.style.left = Math.round(x / cluster.cells.length) + 'px';
    tag.style.top = Math.round(y / cluster.cells.length) + 'px';
    wrap.appendChild(tag);
    setTimeout(function () {
      if (tag.parentNode) tag.parentNode.removeChild(tag);
    }, 1000);
  }

  function popWinners(clusters) {
    clusters.forEach(function (cluster) {
      cluster.cells.forEach(function (pair) {
        const cell = cells[pair[0]][pair[1]];
        cell.classList.remove('hit');
        cell.classList.add('pop');
        shards(cell, cluster.symbol);
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
        cell.classList.remove('drop', 'fall', 'land', 'pop', 'hit');
        dropShards(cell);
        clearShift(cell);
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
  //
  // Каскад — не новый спин, а продолжение того же. Поэтому поле целиком здесь
  // больше не перерисовывается: лопаются только скопления, вниз едут только те
  // символы, под которыми освободилось место, а сверху досыпаются новые. Клетки
  // в колонках, где ничего не выиграло, вообще не двигаются — раньше все
  // тридцать падали заново, и это читалось как «началась новая катка».

  const FALL_MS = 340;                 // должно совпадать с --fall в app.css
  const COL_STEP = 25;                 // на сколько отстаёт следующая колонка

  function rowPitch() {
    // Шаг сетки в пикселях: высота клетки плюс зазор. Берётся из живого DOM,
    // потому что клетка тянется по ширине экрана.
    const first = cells[0][0];
    const second = cells[0][1];
    const pitch = second ? second.offsetTop - first.offsetTop : 0;
    return pitch || first.offsetHeight || 56;
  }

  function fallPlan(step, next) {
    /* Кто куда переезжает после того, как скопления лопнули.

       Правила падения те же, что в games/storm.py collapse(): выжившие
       сохраняют порядок и оседают вниз, недостача досыпается сверху. Значит
       символ из строки r съезжает вниз на столько клеток, сколько под ним
       лопнуло, а новые занимают верхние строки и падают на всю высоту дырки. */
    const gone = [];
    for (let c = 0; c < conf.cols; c++) gone.push([]);
    step.clusters.forEach(function (cluster) {
      cluster.cells.forEach(function (pair) { gone[pair[0]].push(pair[1]); });
    });

    const moves = [];
    for (let c = 0; c < conf.cols; c++) {
      const dead = gone[c];
      if (!dead.length) continue;                 // колонка стоит на месте
      for (let r = conf.rows - 1; r >= 0; r--) {
        if (dead.indexOf(r) >= 0) continue;
        let below = 0;
        for (let k = 0; k < dead.length; k++) if (dead[k] > r) below++;
        if (below) {
          moves.push({ col: c, row: r + below, key: step.grid[c][r],
                       from: below, fresh: false });
        }
      }
      for (let j = 0; j < dead.length; j++) {
        moves.push({ col: c, row: j, key: next[c][j],
                     from: dead.length, fresh: true });
      }
    }
    return moves;
  }

  async function dropIn(step, next) {
    const moves = fallPlan(step, next);
    if (!moves.length) return;
    const pitch = rowPitch();

    // Первый кадр: символы уже на новых местах, но сдвинуты туда, откуда падают.
    // Прозрачность не трогаем: новые должны выезжать из-за верхнего края поля, а
    // не проявляться на месте — проявление и читалось как подмена поля.
    moves.forEach(function (move) {
      const cell = cells[move.col][move.row];
      cell.classList.remove('pop', 'hit', 'drop', 'fall', 'land');
      dropShards(cell);
      setCell(cell, move.key);
      cell.style.transition = 'none';
      cell.style.transform = 'translateY(' + (-move.from * pitch) + 'px)';
    });
    void el.reels.offsetWidth;                    // фиксируем стартовое положение

    // Второй кадр: отпускаем — колонка едет вниз одним движением. Колонки
    // трогаются друг за другом: одновременный старт всех шести выглядит как
    // подмена поля, а не как осыпание.
    moves.forEach(function (move) {
      const cell = cells[move.col][move.row];
      cell.style.transition = '';
      cell.style.transitionDelay = (move.col * COL_STEP) + 'ms';
      cell.classList.add('fall');
      cell.style.transform = '';
    });

    // Приземление: символ приседает от удара — каждая колонка в свой момент.
    moves.forEach(function (move) {
      const cell = cells[move.col][move.row];
      setTimeout(function () {
        cell.classList.remove('fall');
        cell.style.transitionDelay = '';
        cell.classList.add('land');
        setTimeout(function () { cell.classList.remove('land'); }, 210);
      }, FALL_MS + move.col * COL_STEP + 20);
    });
    await WC.wait(FALL_MS + COL_STEP * (conf.cols - 1) + 110);
  }

  async function playCascades(spin) {
    let won = 0;
    if (!spin.steps.length) {
      paint(spin.grid, false);
      return won;
    }
    for (let i = 0; i < spin.steps.length; i++) {
      const step = spin.steps[i];
      // Сетка шага уже на экране: её посадил landReels (первый шаг) или
      // предыдущий dropIn. Перерисовывать нечего.
      markWinners(step.clusters);
      step.clusters.forEach(clusterWin);
      flash(step.win >= 5 ? 'strong' : '');
      WC.impact('medium');
      won += step.win;
      showBadge(WC.money(Math.round(won * bet)) +
                (spin.steps.length > 1 ? '  ·  каскад ' + (i + 1) : ''), 'live');
      await WC.wait(440);                         // подсветка и сумма читаются

      popWinners(step.clusters);
      WC.impact('heavy');
      await WC.wait(360);                         // взрыв доигрывает до конца

      const next = spin.steps[i + 1] ? spin.steps[i + 1].grid : spin.grid;
      await dropIn(step, next);
    }
    // Страховка: на экране должно стоять ровно то поле, которое посчитал
    // сервер, — по нему же ищутся штормы. Без анимации, это не кадр.
    paint(spin.grid, false);
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
