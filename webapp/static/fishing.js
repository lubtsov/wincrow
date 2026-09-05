/* Рыбалка: живое колесо, раунд один на всех.

   Клиент здесь ничего не решает. Номер раунда, расписание, угол остановки и
   множители приходят с сервера (games/fishing.py), а колесо крутится по формуле
   от серверного времени — поэтому у всех игроков оно стоит в одном положении, и
   подкрутить исход в DevTools нечем: его тут не вычисляют.

   Что известно когда — тоже решает сервер. Пока идут ставки, на рыбах видны
   только минимальные иксы; икс раунда, множитель и угол остановки приезжают в
   момент закрытия ставок; кто победил — когда колесо встало. */

(function () {
  const el = {};
  let st = null;             // последнее состояние раунда с сервера
  let bet = 10;
  let skew = 0;              // серверное время минус локальное, секунды
  let frame = null;          // requestAnimationFrame
  let poll = null;           // setTimeout следующего запроса
  let live = false;          // экран открыт
  let wired = false;

  let spinBase = 0;          // угол колеса на начале текущего раунда
  let shown = null;          // номер раунда, который уже нарисован
  let drops = {};            // на каких рыбах множитель уже показали
  let caught = false;        // сцену вытягивания уже проиграли
  let scene = 0;             // токен сцены: раунд сменился — старая молчит
  let sending = 0;           // ставок в полёте

  // Градусов в секунду на свободном ходу. Отсюда же выводится торможение:
  // докрутка длится spin_seconds и начинается ровно с этой скорости.
  const SPEED = 200;

  function grab() {
    el.sub = document.getElementById('fi-sub');
    el.wheel = document.getElementById('fi-wheel');
    el.rim = document.getElementById('fi-rim');
    el.labels = document.getElementById('fi-labels');
    el.box = el.wheel.parentNode;
    el.clock = document.getElementById('fi-clock');
    el.state = document.getElementById('fi-state');
    el.catch = document.getElementById('fi-catch');
    el.line = document.getElementById('fi-line');
    el.hooked = document.getElementById('fi-hooked');
    el.cap = document.getElementById('fi-catch-cap');
    el.wonBox = document.getElementById('fi-catch-win');
    el.ribbon = document.getElementById('fi-ribbon');
    el.picks = document.getElementById('fi-picks');
    el.chips = document.getElementById('fi-chips');
    el.betLabel = document.getElementById('fi-bet-label');
    el.log = document.getElementById('fi-log');
  }

  function uuid() {
    try { return crypto.randomUUID(); } catch (e) { /* старый webview */ }
    return 'fi-' + Date.now().toString(36) + '-' +
           Math.random().toString(36).slice(2, 10);
  }

  function now() {
    return Date.now() / 1000 + skew;
  }

  function posOf(key) {
    return (st.positions || []).filter(function (p) { return p.key === key; })[0];
  }

  function emojiOf(key) {
    const found = posOf(key);
    return found ? found.emoji : '❔';
  }

  function kindOf(key) {
    const found = posOf(key);
    return found ? found.kind : 'fish';
  }

  /* --- колесо ------------------------------------------------------------ */

  function buildWheel() {
    // Секторы рисуются одним conic-gradient: так их сколько угодно, а вращается
    // всё одним transform на родителе — без перерисовки на каждом кадре.
    const stops = st.sectors.map(function (s) {
      return 'var(--fi-' + s.pick + ') ' + s.from.toFixed(3) + 'deg ' +
             (s.from + s.width).toFixed(3) + 'deg';
    });
    el.rim.style.background = 'conic-gradient(from 0deg, ' + stops.join(', ') + ')';

    el.labels.innerHTML = '';
    st.sectors.forEach(function (s) {
      const mark = document.createElement('i');
      mark.className = 'fi-mark ' + s.pick;
      mark.dataset.sector = s.index;
      // Подпись стоит по центру своего сектора и повёрнута вместе с ним.
      mark.style.transform = 'rotate(' + (s.from + s.width / 2) + 'deg)';
      // На звеньях вместо эмодзи точка: их 21, и кружки съели бы весь обод.
      mark.innerHTML = kindOf(s.pick) === 'shade'
        ? '<span class="fi-dot"></span>'
        : '<span>' + emojiOf(s.pick) + '</span>';
      el.labels.appendChild(mark);
    });
  }

  function sweepTo(close) {
    /* Сколько градусов осталось докрутить от угла закрытия ставок до метки.

       Однозначного ответа нет: до нужного положения можно доехать за пол-круга
       или за три. Берём то число оборотов, при котором путь ближе всего к
       SPEED*spin/2 — тогда колесо тормозит с той же скорости, на которой шло, и
       перехода не видно вовсе. */
    const want = SPEED * st.spin_seconds / 2;
    let sweep = ((st.angle - close) % 360 + 360) % 360;
    while (sweep < want - 180) sweep += 360;
    return sweep;
  }

  function wheelAngle(t) {
    const T = st.times;
    if (t <= T.started_at) return spinBase;
    if (t < T.closes_at) return spinBase + SPEED * (t - T.started_at);

    const close = spinBase + SPEED * st.bet_seconds;
    const held = Math.min(t - T.closes_at, st.spin_seconds);
    // Угол ещё не приехал (ответ в пути) — крутим ровно, без рывков.
    if (typeof st.angle !== 'number') return close + SPEED * held;

    const sweep = sweepTo(close);
    const u = held / st.spin_seconds;
    // Показатель подобран так, что начальная скорость торможения равна SPEED:
    // производная 1-(1-u)^p в нуле равна p, а путь — sweep.
    const p = SPEED * st.spin_seconds / sweep;
    return close + sweep * (1 - Math.pow(1 - u, p));
  }

  function sectorAt(angle) {
    // Обратная задача: колесо повёрнуто на angle — что под меткой сверху.
    const mark = ((-angle) % 360 + 360) % 360;
    let found = st.sectors[st.sectors.length - 1];
    st.sectors.forEach(function (s) {
      if (mark >= s.from && mark < s.from + s.width) found = s;
    });
    return found;
  }

  function norm(angle) {
    return ((angle % 360) + 360) % 360;
  }

  function fmtMult(value) {
    const n = Number(value) || 0;
    return n === Math.round(n) ? String(n) : n.toFixed(2).replace(/0$/, '');
  }

  function rtpText(data) {
    /* Отдача у рыб и у цвета немного разная: ×1.9 на половине круга дают чуть
       меньше, чем доли круга, выведенные из RTP. Пишем размах, а не одно число. */
    const all = Object.keys(data.rtp_by_pick || {}).map(function (key) {
      return Math.round(data.rtp_by_pick[key] * 100);
    });
    if (!all.length) return Math.round(data.rtp * 100) + '%';
    const low = Math.min.apply(null, all);
    const high = Math.max.apply(null, all);
    return low === high ? low + '%' : low + '–' + high + '%';
  }

  /* --- кадр --------------------------------------------------------------- */

  function loop() {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(paintFrame);
  }

  function paintFrame() {
    frame = null;
    if (!live || !st) return;
    const t = now();
    el.wheel.style.transform = 'rotate(' + wheelAngle(t).toFixed(2) + 'deg)';
    paintClock(t);
    fireDrops(t);
    if (t >= st.times.stops_at && !caught) landing();
    frame = requestAnimationFrame(paintFrame);
  }

  function paintClock(t) {
    const T = st.times;
    if (t < T.closes_at) {
      const left = Math.max(1, Math.ceil(T.closes_at - t));
      el.clock.textContent = left;
      el.clock.classList.toggle('hot', left <= 3);
      el.state.textContent = 'ставки';
      el.box.classList.remove('shut');
    } else if (t < T.stops_at) {
      el.clock.textContent = '🔒';
      el.clock.classList.remove('hot');
      el.state.textContent = 'ставки закрыты';
      el.box.classList.add('shut');
    } else {
      el.clock.textContent = emojiOf(sectorAt(wheelAngle(t)).pick);
      el.state.textContent = 'результат';
    }
  }

  /* --- множитель на рыбах -------------------------------------------------- */

  function fireDrops(t) {
    /* Сервер присылает множители вместе с временем показа (show_at), поэтому
       падают они у всех игроков в одну и ту же секунду. */
    (st.drops || []).forEach(function (drop) {
      const key = st.no + ':' + drop.fish;
      if (drops[key] || t < drop.show_at) return;
      drops[key] = 1;

      const card = el.picks.querySelector('[data-pick="' + drop.fish + '"]');
      if (!card) return;
      const slot = card.querySelector('.fi-mult');
      if (slot) slot.textContent = '×' + fmtMult(drop.mult);

      // Зашли в середине раунда — множитель уже давно висит, без анимации.
      if (t > drop.show_at + 2.5) return;

      card.classList.remove('boom');
      void card.offsetWidth;
      card.classList.add('boom');

      const plus = document.createElement('span');
      plus.className = 'fi-plus';
      plus.textContent = '×' + fmtMult(drop.boost);
      card.appendChild(plus);
      setTimeout(function () {
        if (plus.parentNode) plus.parentNode.removeChild(plus);
      }, 1100);

      const marks = el.labels.querySelectorAll('.fi-mark.' + drop.fish);
      Array.prototype.forEach.call(marks, function (mark) {
        mark.classList.remove('boom');
        void mark.offsetWidth;
        mark.classList.add('boom');
      });
      WC.impact('light');
    });
  }

  /* --- вытягивание из проруби --------------------------------------------- */

  async function landing() {
    /* Колесо встало. Что под меткой — считаем из того же угла, которым его
       нарисовали, поэтому подсветка и сцена не разойдутся с картинкой. */
    caught = true;
    const token = ++scene;
    const sector = sectorAt(wheelAngle(now()));
    const pos = posOf(sector.pick) || { emoji: '❔', title: '', mult: 0 };

    const marks = el.labels.querySelectorAll('[data-sector="' + sector.index + '"]');
    Array.prototype.forEach.call(marks, function (m) { m.classList.add('win'); });
    el.box.classList.add('landed');
    WC.impact('heavy');

    el.hooked.textContent = pos.emoji;
    el.hooked.className = 'fi-hooked ' + sector.pick;
    el.cap.textContent = '';
    el.cap.className = 'fi-catch-cap';
    el.wonBox.textContent = '';
    el.wonBox.className = 'fi-catch-win';
    delete el.wonBox.dataset.done;
    el.catch.hidden = false;
    void el.catch.offsetWidth;
    el.catch.classList.add('on');
    el.line.classList.add('tight');

    await WC.wait(360);                       // леска натягивается
    if (token !== scene) return;
    el.hooked.classList.add('fight');         // добыча упирается
    WC.impact('medium');

    await WC.wait(820);
    if (token !== scene) return;
    el.hooked.classList.remove('fight');
    el.hooked.classList.add('out');           // выходит из лунки

    await WC.wait(520);
    if (token !== scene) return;
    el.cap.innerHTML = '<span class="fi-cap-mult">×' + fmtMult(pos.mult) + '</span>' +
                       '<span class="fi-cap-name">' + WC.esc(pos.title) + '</span>';
    el.cap.classList.add('on');
    paintWin();
  }

  function refundOnly() {
    /* Колесо встало на рыбу, а ставил игрок только на цвет: деньги вернулись,
       но это не выигрыш, и подписывать его зелёным «+$1.00» нельзя. */
    return !!st.landed && kindOf(st.landed.pick) === 'fish' && st.bet_cents > 0 &&
      (st.positions || []).every(function (pos) {
        return pos.kind === 'shade' || !pos.mine_cents;
      });
  }

  function paintWin() {
    /* Сумму пишет сервер: расчёт мог приехать и позже начала сцены, поэтому
       строку выигрыша перерисовываем на каждом ответе, пока сцена открыта. */
    if (!caught || !st || el.catch.hidden || !st.landed) return;
    if (refundOnly()) {
      el.wonBox.textContent = 'возврат ' + st.win;
      el.wonBox.className = 'fi-catch-win back';
      if (!el.wonBox.dataset.done) {
        el.wonBox.dataset.done = '1';
        WC.impact('light');
        WC.setBalance(st.balance, true);
      }
    } else if (st.win_cents > 0) {
      el.wonBox.textContent = '+' + st.win;
      el.wonBox.className = 'fi-catch-win good';
      if (!el.wonBox.dataset.done) {
        el.wonBox.dataset.done = '1';
        WC.haptic('success');
        WC.confetti(18);
        WC.setBalance(st.balance, true);
      }
    } else if (st.bet_cents > 0) {
      el.wonBox.textContent = 'мимо';
      el.wonBox.className = 'fi-catch-win bad';
      if (!el.wonBox.dataset.done) {
        el.wonBox.dataset.done = '1';
        WC.haptic('error');
      }
    }
  }

  function closeCatch() {
    el.catch.hidden = true;
    el.catch.classList.remove('on');
    el.line.classList.remove('tight');
    el.cap.textContent = '';
    el.cap.className = 'fi-catch-cap';
    el.wonBox.textContent = '';
    el.wonBox.className = 'fi-catch-win';
    delete el.wonBox.dataset.done;
    el.hooked.className = 'fi-hooked';
    el.box.classList.remove('landed');
    Array.prototype.forEach.call(el.labels.children, function (m) {
      m.classList.remove('win');
      m.classList.remove('boom');
    });
  }

  /* --- состояние ---------------------------------------------------------- */

  function sync(data, sentAt) {
    // Часы сервера. Половина времени запроса — поправка на дорогу туда.
    const rtt = (Date.now() - sentAt) / 1000;
    skew = data.now + rtt / 2 - Date.now() / 1000;
  }

  function apply(data) {
    const fresh = shown !== data.no;
    // Новый раунд продолжает угол прошлого: колесо не прыгает на стыке.
    if (fresh && shown !== null && st) spinBase = norm(wheelAngle(now()));
    st = data;
    if (fresh) {
      shown = data.no;
      drops = {};
      caught = false;
      scene++;
      closeCatch();
    }
    el.sub.textContent = 'Раунд #' + data.no + ' · отдача ' + rtpText(data);
    paintPicks();
    paintRibbon();
    paintLog();
    paintWin();
    if (data.balance) WC.setBalance(data.balance);
  }

  async function refresh() {
    if (!live) return;
    const sentAt = Date.now();
    try {
      const data = await WC.api('/api/fishing/state', {});
      if (!live) return;
      sync(data, sentAt);
      apply(data);
    } catch (err) {
      WC.banner(WC.errorText(err), 'bad');
    }
    schedule();
  }

  function schedule() {
    /* Запросы приурочены к событиям раунда: иксы и угол остановки открываются
       в момент закрытия ставок, победитель — когда колесо встало. Между этими
       точками достаточно редкого опроса — таймер и колесо клиент ведёт сам. */
    if (poll) clearTimeout(poll);
    poll = null;
    if (!live || !st) return;
    const t = now();
    const T = st.times;
    let at;
    if (t < T.closes_at) at = Math.min(T.closes_at + 0.15, t + 2.5);
    else if (t < T.stops_at) at = T.stops_at + 0.1;
    else at = T.ends_at + 0.15;
    poll = setTimeout(refresh, Math.max(250, (at - t) * 1000));
  }

  /* --- позиции, лента, история -------------------------------------------- */

  function shortName(pos) {
    return pos.kind === 'fish' ? pos.title.replace(' рыба', '')
                               : pos.title.replace(' звено', '');
  }

  function buildPicks() {
    // Карточки строятся один раз: их классы держат анимации прибавок, и
    // перерисовка на каждом ответе сервера их бы срывала.
    el.picks.innerHTML = '';
    st.positions.forEach(function (pos) {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'fi-pick ' + pos.key;
      card.dataset.pick = pos.key;
      card.innerHTML =
        '<span class="fi-face">' + pos.emoji + '</span>' +
        '<span class="fi-name">' + WC.esc(shortName(pos)) + '</span>' +
        '<b class="fi-mult">×' + fmtMult(pos.floor) + '</b>' +
        '<span class="fi-odds">' + Math.round(pos.chance * 100) + '%</span>' +
        '<span class="fi-mine"></span>';
      card.addEventListener('click', function () { stake(pos.key); });
      el.picks.appendChild(card);
    });
  }

  function paintPicks() {
    const open = now() < st.times.closes_at;
    st.positions.forEach(function (pos) {
      const card = el.picks.querySelector('[data-pick="' + pos.key + '"]');
      if (!card) return;
      card.disabled = !open;
      card.classList.toggle('has', pos.mine_cents > 0);
      card.querySelector('.fi-mine').textContent =
        pos.mine_cents > 0 ? pos.mine : '';
      // Икс раунда уже приехал, но множитель ещё не упал на экран — держим то,
      // что было до него, иначе цифра сменится раньше своей анимации.
      const shownDrop = drops[st.no + ':' + pos.key];
      const value = (pos.kind === 'fish' && !shownDrop) ? pos.base : pos.mult;
      card.querySelector('.fi-mult').textContent = '×' + fmtMult(value);
    });
  }

  function paintRibbon() {
    const rows = st.recent || [];
    if (!rows.length) {
      el.ribbon.innerHTML = '<i class="fi-none">Раунды только начались</i>';
      return;
    }
    el.ribbon.innerHTML = rows.map(function (row) {
      return '<i class="fi-was ' + row.pick + '" title="Раунд #' + row.no + '">' +
             emojiOf(row.pick) + '<b>×' + fmtMult(row.mult) + '</b></i>';
    }).join('');
  }

  function paintLog() {
    const rows = st.history || [];
    if (!rows.length) {
      el.log.innerHTML = '<p class="empty">Пока ни одной ставки.</p>';
      return;
    }
    el.log.innerHTML = rows.map(function (row) {
      const won = row.payout_cents > 0;
      return '<div class="log-row' + (won ? ' won' : '') + '">' +
             '<span class="log-bet">' + emojiOf(row.pick) + ' ' +
             WC.esc(row.bet) + '</span>' +
             '<span class="log-mult">×' + fmtMult(row.multiplier) + '</span>' +
             '<span class="log-win">' + (won ? '+' + WC.esc(row.win) : '—') +
             '</span></div>';
    }).join('');
  }

  /* --- ставка -------------------------------------------------------------- */

  function setBet(cents, remember) {
    const step = 10;
    bet = Math.max(st.min_bet,
                   Math.min(st.max_bet, Math.round(cents / step) * step));
    el.betLabel.textContent = WC.money(bet);
    Array.prototype.forEach.call(el.chips.children, function (chip) {
      chip.classList.toggle('on', Number(chip.dataset.bet) === bet);
    });
    if (remember !== false) {
      try { localStorage.setItem('wc.bet', String(bet)); } catch (e) { /* ok */ }
    }
  }

  function renderChips() {
    el.chips.innerHTML = (st.bets || []).map(function (cents) {
      return '<button class="chip" type="button" data-bet="' + cents + '">' +
             WC.money(cents) + '</button>';
    }).join('');
  }

  function told(status, pos) {
    const said = {
      closed: '🔒 Ставки на этот раунд уже закрыты',
      no_money: 'Не хватает на такую ставку',
      bad_bet: 'Ставка вне лимитов',
      too_many: 'На один раунд больше ' + st.max_bets + ' ставок нельзя',
    };
    if (status === 'ok') {
      WC.impact('medium');
      WC.banner(pos.emoji + ' ' + WC.money(bet) + ' на «' + shortName(pos) + '»',
                'good');
      return;
    }
    if (status === 'repeat') return;          // тот же bet_id, деньги уже сняты
    WC.haptic('error');
    WC.banner(said[status] || 'Ставка не прошла', 'bad');
  }

  async function stake(key) {
    if (!st || sending > 5) return;
    const pos = posOf(key);
    if (!pos) return;
    // Сервер проверит это заново — здесь только чтобы не гонять запрос зря.
    if (now() >= st.times.closes_at) {
      WC.haptic('error');
      WC.banner('🔒 Ставки на этот раунд уже закрыты', 'bad');
      return;
    }
    const card = el.picks.querySelector('[data-pick="' + key + '"]');
    if (card) card.classList.add('sending');
    sending++;
    const sentAt = Date.now();
    try {
      const data = await WC.api('/api/fishing/bet', {
        no: st.no, pick: key, bet_cents: bet, bet_id: uuid(),
      });
      sync(data, sentAt);
      apply(data);
      told(data.status, pos);
    } catch (err) {
      WC.haptic('error');
      WC.banner(WC.errorText(err), 'bad');
    } finally {
      sending--;
      if (card) card.classList.remove('sending');
    }
  }

  /* --- правила -------------------------------------------------------------- */

  function rules() {
    const rows = st.positions.map(function (pos) {
      const pays = pos.kind === 'fish'
        ? '×' + fmtMult(pos.floor) + '…×' + fmtMult(pos.top)
        : '×' + fmtMult(pos.floor) + ' всегда';
      return '<div class="pay-row"><span class="pay-sym">' + pos.emoji + '</span>' +
             '<span class="pay-name">' + WC.esc(pos.title) + '</span>' +
             '<span class="pay-nums">' + pays + ' · отдача ' +
             Math.round(pos.rtp * 100) + '%</span></div>';
    }).join('');
    const links = st.sectors.filter(function (s) {
      return kindOf(s.pick) === 'shade';
    }).length;
    WC.sheet(
      '<h2>Рыбалка</h2>' +
      '<p class="sub">Раунд один на всех: колесо, таймер и результат у всех ' +
      'игроков одинаковые. Ставки принимаются ' + st.bet_seconds + ' секунд, ' +
      'потом колесо ещё крутится и плавно встаёт на результате.</p>' +
      '<div class="pays">' + rows + '</div>' +
      '<p class="sub">По кругу идёт ' + st.links_per_fish + ' звеньев, рыба, ' +
      st.links_per_fish + ' звеньев, рыба — всего ' + st.sectors.length +
      ' секторов, звеньев из них ' + links + '. Белые и серые делят круг ровно ' +
      'поровну, платит каждое ×' + fmtMult(st.shade_mult) + '. Чем крупнее ' +
      'рыба, тем уже её сектор.</p>' +
      '<p class="sub">Выпала рыба — ставки на цвет <b>возвращаются</b>: рыбий ' +
      'сектор не белый и не серый, значит цвет в этом раунде не играл.</p>' +
      '<p class="sub">Иксы рыб: ' + st.positions.filter(function (pos) {
        return pos.kind === 'fish';
      }).map(function (pos) {
        return WC.esc(shortName(pos).toLowerCase()) + ' — ×' +
               pos.ladder.map(fmtMult).join(', ×');
      }).join('; ') + '. Чем крупнее икс, тем реже он выпадает: вероятность ' +
      'ступени обратна её иксу, поэтому в кассу все ступени приносят одинаково.</p>' +
      '<p class="sub">Икс рыбы в каждом раунде свой, и открывается он, ' +
      '<b>когда ставки уже закрыты</b> — иначе видимый заранее ×100 давал бы ' +
      'отдачу в разы больше 100%. Тогда же на рыб падает множитель ×' +
      fmtMult((st.boosts || [2])[0]) + '…×' +
      fmtMult((st.boosts || [10])[(st.boosts || [10]).length - 1]) +
      ': он умножает икс той рыбы, на которую упал, и достаться может как одной, ' +
      'так и всем трём. Платит тот икс, который стоит на рыбе в момент ' +
      'остановки.</p>' +
      '<p class="sub">Ставок за раунд можно сделать до ' + st.max_bets + ', ' +
      'сразу на несколько позиций. Результат считает сервер по provably fair: ' +
      'хеш сида раунда — <code>' + WC.esc((st.fair.hash || '').slice(0, 16)) +
      '…</code>, сам сид открывается после остановки.</p>');
  }

  /* --- вход ---------------------------------------------------------------- */

  function wire() {
    if (wired) return;
    wired = true;

    el.chips.addEventListener('click', function (event) {
      const chip = event.target.closest('[data-bet]');
      if (!chip || !st) return;
      WC.impact('light');
      setBet(Number(chip.dataset.bet));
    });
    document.getElementById('fi-bet-minus').addEventListener('click', function () {
      if (!st) return;
      WC.impact('light');
      setBet(bet - 10);
    });
    document.getElementById('fi-bet-plus').addEventListener('click', function () {
      if (!st) return;
      WC.impact('light');
      setBet(bet + 10);
    });
    document.getElementById('fi-rules').addEventListener('click', function () {
      if (!st) return;
      WC.impact('light');
      rules();
    });
  }

  async function load() {
    const sentAt = Date.now();
    let data;
    try {
      data = await WC.api('/api/fishing/state', {});
    } catch (err) {
      el.sub.textContent = WC.errorText(err);
      el.log.innerHTML = '<p class="empty">' + WC.esc(WC.errorText(err)) + '</p>';
      return;
    }
    if (!live) return;
    sync(data, sentAt);
    st = data;

    let saved = 0;
    try { saved = Number(localStorage.getItem('wc.bet')) || 0; } catch (e) { /* ok */ }
    renderChips();
    setBet(saved || data.bets[0] || data.min_bet, false);
    buildWheel();
    buildPicks();
    apply(data);
    loop();
    schedule();
  }

  WC.register('fish', {
    open: function () {
      grab();
      live = true;
      wire();
      if (!st) { load(); return; }
      loop();
      refresh();
    },
    refresh: function () {
      if (!live) return;
      if (st) refresh(); else load();
    },
    leave: function () {
      // Экран ушёл — гасим кадры и опрос: раунд идёт на сервере и без нас.
      live = false;
      if (frame) { cancelAnimationFrame(frame); frame = null; }
      if (poll) { clearTimeout(poll); poll = null; }
    },
  });
})();
