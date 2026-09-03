/* Игры Mini App: краш, мины, башня, блэкджек, рулетка, монетка.

   Клиент здесь — та же витрина, что и в слотах. Ставку снимает сервер, исход
   считает сервер, множитель краша считает сервер по своим часам. Экран рисует
   присланное и отправляет ровно три вещи: какую игру, на сколько и что нажали.

   Секретов у клиента нет: раскладка мин, плохие двери, точка краша и закрытая
   карта дилера приезжают только вместе с развязкой раунда. */

(function () {
  const el = {};
  let conf = null;                 // ответ /api/games/state
  let game = null;                 // ключ открытой игры
  let round = null;                // текущий раунд с сервера
  let bet = 10;
  let busy = false;
  let minesPick = 3;               // сколько мин просить в следующем поле
  let crashPaint = null;           // таймер рисования множителя
  let crashPeek = null;            // таймер вопроса «не сорвалось ли»

  function grab() {
    el.title = document.getElementById('games-title');
    el.sub = document.getElementById('games-sub');
    el.back = document.getElementById('games-back');
    el.list = document.getElementById('game-list');
    el.table = document.getElementById('game-table');
    el.board = document.getElementById('board');
    el.actions = document.getElementById('game-actions');
    el.betBar = document.getElementById('game-bet-bar');
    el.betLabel = document.getElementById('game-bet-label');
    el.chips = document.getElementById('game-chips');
    el.log = document.getElementById('game-log');
  }

  function spec(key) {
    return (conf.games || []).filter(function (g) { return g.key === key; })[0]
           || { key: key, title: key, emoji: '🎲', note: '' };
  }

  function uuid() {
    try { return crypto.randomUUID(); } catch (e) { /* старый webview */ }
    return 'g-' + Date.now().toString(36) + '-' +
           Math.random().toString(36).slice(2, 10);
  }

  /* --- запросы ----------------------------------------------------------- */

  async function load() {
    try {
      conf = await WC.api('/api/games/state');
    } catch (err) {
      el.log.innerHTML = '<p class="empty">' + WC.esc(WC.errorText(err)) + '</p>';
      WC.banner(WC.errorText(err));
      return;
    }
    WC.setBalance(conf.balance);
    let saved = 0;
    try { saved = Number(localStorage.getItem('wc.bet')) || 0; } catch (e) { /* ok */ }
    renderChips();
    setBet(saved || conf.bets[0] || conf.min_bet, false);
    renderList();
    renderLog(conf.history);
  }

  function apply(data) {
    /* Единственная точка, где меняется состояние экрана после запроса. */
    if (data.balance) WC.setBalance(data.balance, data.round
                                    && data.round.payout_cents > 0);
    if (data.history) { conf.history = data.history; renderLog(data.history); }
    round = data.round && data.round.status === 'active' ? data.round : null;
    return data.round || null;
  }

  function failText(status) {
    return {
      no_money: 'Не хватает на ставку',
      bad_bet: 'Ставка вне лимитов',
      busy: 'Прошлый раунд ещё открыт',
      gone: 'Раунд закрыт — начни заново',
      bad_action: 'Так нельзя',
      bad_pick: 'Выбери, на что ставишь',
      bad_game: 'Такой игры нет',
    }[status] || null;
  }

  async function send(path, payload) {
    if (busy) return null;
    busy = true;
    try {
      const data = await WC.api(path, payload);
      const view = apply(data);
      const problem = failText(data.status);
      if (problem) { WC.banner(problem); WC.haptic('error'); }
      return view;
    } catch (err) {
      WC.banner(WC.errorText(err));
      return null;
    } finally {
      busy = false;
    }
  }

  async function play(pick) {
    const view = await send('/api/games/play', {
      game: game, bet_cents: bet, client_id: uuid(),
      pick: pick === undefined ? null : String(pick),
    });
    if (view) { WC.impact('medium'); afterRound(view); }
  }

  async function step(action) {
    if (!round) return;
    const view = await send('/api/games/step', {
      game: game, round_id: round.round_id, action: String(action),
    });
    if (view) { WC.impact('light'); afterRound(view); }
  }

  function afterRound(view) {
    /* Раунд либо продолжается (мины, башня, краш, блэкджек), либо закрыт. */
    stopCrash();
    if (view.status === 'active') {
      round = view;
      if (view.game === 'crash') startCrash();
    } else {
      round = null;
      if (view.payout_cents > 0) { WC.haptic('success'); WC.confetti(12); }
      else if (view.game !== 'blackjack' || !view.push) WC.haptic('warning');
    }
    renderBoard(view);
    renderActions(view);
  }

  /* --- каталог ------------------------------------------------------------ */

  function renderList() {
    // Первая карточка — слот: он живёт на своей вкладке, но искать его в
    // каталоге игр естественнее, чем помнить про нижнюю навигацию.
    const slots = '<button class="game-card" type="button" data-tab="slots">' +
      '<span class="game-emoji">🎰</span>' +
      '<span class="game-name">Сочный шторм</span>' +
      '<span class="game-note">Слот 6×5 с каскадами</span></button>';
    el.list.innerHTML = slots + (conf.games || []).map(function (g) {
      const live = conf.active && conf.active[g.key];
      return '<button class="game-card" type="button" data-game="' + g.key + '">' +
        '<span class="game-emoji">' + g.emoji + '</span>' +
        '<span class="game-name">' + WC.esc(g.title) + '</span>' +
        '<span class="game-note">' + WC.esc(g.note) + '</span>' +
        (live ? '<span class="game-live">раунд открыт</span>' : '') +
        '</button>';
    }).join('');
  }

  function openGame(key) {
    game = key;
    round = (conf.active && conf.active[key]) || null;
    const g = spec(key);
    el.title.textContent = g.emoji + ' ' + g.title;
    el.sub.textContent = g.note;
    el.back.hidden = false;
    el.list.hidden = true;
    el.table.hidden = false;
    el.betBar.hidden = !!round;
    el.chips.hidden = !!round;
    if (round && key === 'crash') startCrash();
    renderBoard(round || null);
    renderActions(round || null);
  }

  function backToList() {
    stopCrash();
    game = null;
    el.back.hidden = true;
    el.table.hidden = true;
    el.list.hidden = false;
    el.title.textContent = 'Игры';
    el.sub.textContent = 'Ставка снимается сервером, как в боте';
    refresh();
  }

  /* --- ставка ------------------------------------------------------------- */

  function setBet(cents, remember) {
    const step10 = 10;
    bet = Math.max(conf.min_bet,
                   Math.min(conf.max_bet, Math.round(cents / step10) * step10));
    el.betLabel.textContent = WC.money(bet);
    Array.prototype.forEach.call(el.chips.children, function (chip) {
      chip.classList.toggle('on', Number(chip.dataset.bet) === bet);
    });
    if (remember !== false) {
      try { localStorage.setItem('wc.bet', String(bet)); } catch (e) { /* ok */ }
    }
  }

  function renderChips() {
    el.chips.innerHTML = (conf.bets || []).map(function (cents) {
      return '<button class="chip" type="button" data-bet="' + cents + '">' +
             WC.money(cents) + '</button>';
    }).join('');
  }

  /* --- история ------------------------------------------------------------ */

  function renderLog(history) {
    if (!history || !history.length) {
      el.log.innerHTML = '<p class="empty">Пока пусто.</p>';
      return;
    }
    el.log.innerHTML = history.map(function (row) {
      const won = row.payout_cents > 0;
      const push = row.status === 'void';
      return '<div class="log-row' + (won ? ' won' : '') + '">' +
        '<span class="log-game">' + row.emoji + ' ' + WC.esc(row.title) + '</span>' +
        '<span class="log-bet">' + WC.esc(row.bet) + '</span>' +
        '<span class="log-mult">×' + row.multiplier.toFixed(2) + '</span>' +
        '<span class="log-win">' + (won ? '+' + WC.esc(row.win)
                                        : push ? 'возврат' : '—') + '</span>' +
        '</div>';
    }).join('');
  }

  /* --- поля игр ----------------------------------------------------------- */

  function result(view, extra) {
    /* Общая шапка исхода: сумма, множитель и одна строка объяснения. */
    const won = view.payout_cents > 0;
    const cls = won ? 'good' : view.push ? '' : 'bad';
    return '<div class="outcome ' + cls + '">' +
      '<b>' + (won ? '+' + WC.esc(view.win) : view.push ? 'Возврат'
                                                        : '−' + WC.esc(view.bet)) + '</b>' +
      '<span>×' + Number(view.multiplier).toFixed(2) + '</span>' +
      (extra ? '<i>' + extra + '</i>' : '') + '</div>';
  }

  function coinBoard(view) {
    const sides = conf.rules.coin.sides;
    if (!view || view.status === 'active') {
      return '<div class="coin-pick">' + sides.map(function (s) {
        return '<button class="pick-card" type="button" data-pick="' + s.key + '">' +
          '<span class="pick-emoji">' + s.emoji + '</span>' +
          '<span class="pick-name">' + WC.esc(s.name) + '</span>' +
          '<span class="pick-mult">×' + conf.rules.coin.multiplier.toFixed(2) +
          '</span></button>';
      }).join('') + '</div>';
    }
    const mine = sides.filter(function (s) { return s.key === view.pick; })[0];
    return '<div class="coin-flip"><span class="coin-face">' + view.emoji +
      '</span><span class="coin-name">' + WC.esc(view.name) + '</span>' +
      '<span class="sub">ставил на ' + WC.esc(mine ? mine.name : view.pick) +
      '</span></div>' + result(view);
  }

  function rouletteBoard(view) {
    if (!view || view.status === 'active') {
      const outside = conf.rules.roulette.bets.map(function (b) {
        return '<button class="pick-chip" type="button" data-pick="' + b.key + '">' +
          WC.esc(b.label) + '<i>×' + b.multiplier.toFixed(0) + '</i></button>';
      }).join('');
      const red = conf.rules.roulette.red;
      let grid = '';
      for (let n = 0; n <= 36; n++) {
        const cls = n === 0 ? 'zero' : red.indexOf(n) >= 0 ? 'red' : 'black';
        grid += '<button class="wheel-cell ' + cls + '" type="button" data-pick="' +
                n + '">' + n + '</button>';
      }
      return '<div class="rl-outside">' + outside + '</div>' +
             '<div class="rl-grid">' + grid + '</div>' +
             '<p class="sub">Число платит ×' +
             conf.rules.roulette.straight.toFixed(0) + '. Зеро есть, и вся отдача ' +
             'держится именно на нём.</p>';
    }
    return '<div class="rl-result"><span class="rl-number ' +
      (view.number === 0 ? 'zero' : conf.rules.roulette.red.indexOf(view.number) >= 0
        ? 'red' : 'black') + '">' + view.number + '</span>' +
      '<span class="sub">' + WC.esc(view.describe) + ' · ставка: ' +
      WC.esc(view.bet_label) + '</span></div>' + result(view);
  }

  function minesBoard(view) {
    if (!view) {
      return '<div class="mines-pick"><p class="sub">Сколько мин спрятать на ' +
        'поле 5×5? Отдача одинаковая в любом варианте — меняется только шаг ' +
        'множителя.</p><div class="chips">' +
        conf.rules.mines.choices.map(function (c) {
          return '<button class="chip' + (c.n === minesPick ? ' on' : '') +
            '" type="button" data-mines="' + c.n + '">' + c.n + ' 💣 ×' +
            c.first.toFixed(2) + '</button>';
        }).join('') + '</div></div>';
    }

    const opened = view.opened || [];
    const bombs = view.mines || null;          // приезжают только в развязке
    let cells = '';
    for (let i = 0; i < view.cells; i++) {
      const isOpen = opened.indexOf(i) >= 0;
      const isBomb = bombs && bombs.indexOf(i) >= 0;
      let face = '▫️', cls = '';
      if (i === view.hit) { face = '💥'; cls = ' boom'; }
      else if (isBomb) { face = '💣'; cls = ' mine'; }
      else if (isOpen) { face = '💎'; cls = ' gem'; }
      cells += '<button class="mine-cell' + cls + '" type="button" data-cell="' +
               i + '"' + (isOpen || bombs ? ' disabled' : '') + '>' + face +
               '</button>';
    }

    const head = view.status === 'active'
      ? '<div class="board-head"><span>Открыто <b>' + opened.length + '</b> · ' +
        view.n + ' 💣</span><span>×' + view.multiplier.toFixed(2) +
        ' → ×' + view.next_multiplier.toFixed(2) + '</span></div>'
      : '';
    return head + '<div class="mine-grid">' + cells + '</div>' +
           (view.status === 'active' ? '' : result(view, view.cleared
             ? 'поле вычищено' : view.hit !== null ? 'мина' : 'забрал'));
  }

  function towerBoard(view) {
    const ladder = (view && view.ladder) || conf.rules.tower.ladder;
    const level = view ? view.level : 0;
    const bad = (view && view.bad) || null;
    let floors = '';
    for (let f = ladder.length; f >= 1; f--) {
      const done = f <= level;
      const next = f === level + 1;
      floors += '<div class="floor' + (done ? ' done' : '') +
        (next && view && view.status === 'active' ? ' next' : '') + '">' +
        '<span class="floor-no">' + f + '</span>' +
        '<span class="floor-mult">×' + ladder[f - 1].toFixed(2) + '</span>' +
        (next && bad ? '<span class="floor-bad">дверь ' + (bad[f - 1] + 1) +
          ' была плохой</span>' : '') + '</div>';
    }
    const doors = view && view.status === 'active'
      ? '<div class="doors">' + [0, 1, 2].map(function (d) {
          return '<button class="door" type="button" data-door="' + d + '">🚪 ' +
                 (d + 1) + '</button>';
        }).join('') + '</div>'
      : '';
    return '<div class="tower">' + floors + '</div>' + doors +
           (view && view.status === 'done'
             ? result(view, view.fell !== null && view.fell !== undefined
                 ? 'обрыв на ' + (view.floor || level + 1) + ' этаже'
                 : 'забрал с ' + level + ' этажа') : '');
  }

  function crashBoard(view) {
    if (!view) {
      return '<div class="crash-box"><span class="crash-mult">×1.00</span>' +
        '<span class="sub">Множитель растёт на ' +
        Math.round((conf.rules.crash.growth - 1) * 100) + '% в секунду, максимум ×' +
        conf.rules.crash.max_multiplier.toFixed(0) + '. Точка срыва посчитана до ' +
        'старта — из твоего сида.</span></div>';
    }
    if (view.status === 'active') {
      return '<div class="crash-box live"><span class="crash-mult" ' +
        'id="crash-mult">×1.00</span><span class="sub">Забирай, пока растёт.</span>' +
        '</div>';
    }
    return '<div class="crash-box ' + (view.payout_cents > 0 ? 'took' : 'burst') +
      '"><span class="crash-mult">×' + Number(view.point).toFixed(2) + '</span>' +
      '<span class="sub">' + (view.crashed ? 'сорвалось здесь'
        : 'забрал на ×' + Number(view.taken || view.multiplier).toFixed(2)) +
      '</span></div>' + result(view);
  }

  function blackjackBoard(view) {
    const rules = conf.rules.blackjack;
    if (!view) {
      return '<div class="bj-empty"><p class="sub">' + rules.decks +
        ' колод, дилер добирает на мягкой ' + rules.stand + '. Победа ×' +
        rules.win.toFixed(2) + ', блэкджек ×' + rules.blackjack.toFixed(2) +
        '.</p></div>';
    }
    const hidden = view.hidden ? '<span class="card back">🂠</span>'.repeat(view.hidden) : '';
    const cards = function (list) {
      return list.map(function (c) {
        const red = c.indexOf('♥') >= 0 || c.indexOf('♦') >= 0;
        return '<span class="card' + (red ? ' red' : '') + '">' + WC.esc(c) +
               '</span>';
      }).join('');
    };
    const head = view.status === 'done'
      ? result(view, {
          bust: 'перебор', push_bj: 'блэкджек у обоих', player_bj: 'блэкджек!',
          dealer_bj: 'блэкджек у дилера', dealer_bust: 'перебор у дилера',
          win: 'твоя рука старше', lose: 'рука дилера старше', push: 'ничья',
        }[view.outcome] || '')
      : '';
    return '<div class="bj-table">' +
      '<div class="bj-side"><span class="cap">Дилер' +
        (view.status === 'done' ? ' · ' + view.dealer_total : '') + '</span>' +
        '<div class="hand">' + cards(view.dealer) + hidden + '</div></div>' +
      '<div class="bj-side"><span class="cap">Ты · ' + view.player_total +
        (view.player_soft ? ' (мягкие)' : '') +
        (view.doubled ? ' · удвоено' : '') + '</span>' +
        '<div class="hand">' + cards(view.player) + '</div></div></div>' + head;
  }

  function renderBoard(view) {
    if (game === 'coin') el.board.innerHTML = coinBoard(view);
    else if (game === 'roulette') el.board.innerHTML = rouletteBoard(view);
    else if (game === 'mines') el.board.innerHTML = minesBoard(view);
    else if (game === 'tower') el.board.innerHTML = towerBoard(view);
    else if (game === 'crash') el.board.innerHTML = crashBoard(view);
    else if (game === 'blackjack') el.board.innerHTML = blackjackBoard(view);
    el.betBar.hidden = !!(view && view.status === 'active');
    el.chips.hidden = el.betBar.hidden;
  }

  /* --- кнопки под полем --------------------------------------------------- */

  function button(label, act, cls) {
    return '<button class="btn ' + (cls || '') + '" type="button" data-act="' +
           act + '">' + label + '</button>';
  }

  function renderActions(view) {
    const live = view && view.status === 'active';
    let html = '';

    if (game === 'crash') {
      html = live
        ? button('💸 Забрать <b id="crash-take">×1.00</b>', 'c', 'take')
        : button('🚀 Старт', 'start', 'spin');
    } else if (game === 'blackjack') {
      html = live
        ? button('🃏 Ещё', 'h') + button('✋ Хватит', 's') +
          (view.can_double ? button('✖️ Удвоить', 'd', 'ghost') : '')
        : button('🃏 Раздать', 'start', 'spin');
    } else if (game === 'mines') {
      html = live
        ? button('💸 Забрать ' + WC.money(view.cash_cents), 'c', 'take' +
                 (view.opened.length ? '' : ' off'))
        : button('💣 Поставить поле', 'start', 'spin');
    } else if (game === 'tower') {
      html = live
        ? (view.level ? button('💸 Забрать ' + WC.money(view.cash_cents), 'c', 'take')
                      : '<p class="sub">Выбирай дверь на первый этаж.</p>')
        : button('🚪 Начать подъём', 'start', 'spin');
    } else if (view && view.status === 'done') {
      html = button('↻ Ещё раз', 'again', 'spin');
    }
    el.actions.innerHTML = html;
    if (live && game === 'crash') paintCrash();
  }

  /* --- краш: живая цифра --------------------------------------------------- */

  function crashNow() {
    /* Та же формула, что на сервере, но с задержкой: цифра на экране всегда
       чуть отстаёт, поэтому выплата не может оказаться меньше показанной.

       Часы клиента и сервера не совпадают, поэтому считается не по абсолютному
       времени, а от момента получения ответа: сколько прошло у сервера плюс
       сколько прошло здесь. */
    const r = round;
    if (!r || !r.started_at) return 1;
    const here = (Date.now() / 1000) - (r._localAt || Date.now() / 1000);
    const seconds = Math.max(0, (r.now - r.started_at) + here - r.lag);
    return Math.min(Math.pow(r.growth, seconds / r.tick), r.max_multiplier);
  }

  function paintCrash() {
    const value = crashNow();
    const box = document.getElementById('crash-mult');
    const take = document.getElementById('crash-take');
    if (box) box.textContent = '×' + value.toFixed(2);
    if (take) take.textContent = '×' + value.toFixed(2);
  }

  function startCrash() {
    stopCrash();
    if (!round) return;
    round._localAt = Date.now() / 1000;
    paintCrash();
    crashPaint = setInterval(paintCrash, 60);
    // Про срыв знает только сервер: точку краша клиенту не отдают. Поэтому
    // раз в секунду спрашиваем, жив ли раунд, — не «забрать», а именно спросить.
    crashPeek = setInterval(function () {
      if (!busy && round && round.game === 'crash') step('p');
    }, 950);
  }

  function stopCrash() {
    if (crashPaint) { clearInterval(crashPaint); crashPaint = null; }
    if (crashPeek) { clearInterval(crashPeek); crashPeek = null; }
  }

  /* --- нажатия ------------------------------------------------------------- */

  let wired = false;

  function wire() {
    if (wired) return;
    wired = true;

    el.list.addEventListener('click', function (event) {
      const tab = event.target.closest('[data-tab]');
      if (tab) { WC.impact('light'); WC.tab(tab.dataset.tab); return; }
      const card = event.target.closest('[data-game]');
      if (!card) return;
      WC.impact('light');
      openGame(card.dataset.game);
    });

    el.back.addEventListener('click', backToList);

    el.chips.addEventListener('click', function (event) {
      const chip = event.target.closest('[data-bet]');
      if (!chip) return;
      WC.impact('light');
      setBet(Number(chip.dataset.bet));
    });

    document.getElementById('game-bet-minus').addEventListener('click', function () {
      WC.impact('light');
      setBet(bet - 10);
    });
    document.getElementById('game-bet-plus').addEventListener('click', function () {
      WC.impact('light');
      setBet(bet + 10);
    });

    el.board.addEventListener('click', onBoardClick);
    el.actions.addEventListener('click', onActionClick);
  }

  function onBoardClick(event) {
    const pick = event.target.closest('[data-pick]');
    if (pick) { play(pick.dataset.pick); return; }

    const mines = event.target.closest('[data-mines]');
    if (mines) {
      minesPick = Number(mines.dataset.mines);
      WC.impact('light');
      renderBoard(null);
      return;
    }

    const cell = event.target.closest('[data-cell]');
    if (cell && !cell.disabled) { step(cell.dataset.cell); return; }

    const door = event.target.closest('[data-door]');
    if (door) { step(door.dataset.door); }
  }

  function onActionClick(event) {
    const action = event.target.closest('[data-act]');
    if (!action) return;
    const act = action.dataset.act;

    if (act === 'start' || act === 'again') {
      if (game === 'mines') { play(minesPick); return; }
      if (game === 'coin' || game === 'roulette') {
        // Здесь ставку выбирают в поле — просто вернём выбор.
        round = null;
        renderBoard(null);
        renderActions(null);
        return;
      }
      play();
      return;
    }
    step(act);
  }

  /* --- вход --------------------------------------------------------------- */

  async function refresh() {
    if (busy) return;
    try {
      const fresh = await WC.api('/api/games/state');
      conf = fresh;
      WC.setBalance(fresh.balance);
      renderLog(fresh.history);
      if (!game) renderList();
    } catch (e) { /* обновление баланса не критично */ }
  }

  WC.register('games', {
    open: async function () {
      grab();
      wire();
      if (!conf) { await load(); return; }
      await refresh();
      if (game) { renderBoard(round); renderActions(round); }
    },
    refresh: refresh,
    leave: stopCrash,
  });
})();
