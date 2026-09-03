/* Экран ежедневного кейса.

   Клиент не знает, где приз: до открытия сервер присылает только число
   карточек, а `reveal` приезжает уже вместе с результатом. Поэтому подсмотреть
   выигрышную карточку в трафике нельзя — её там нет. */

(function () {
  const el = {};
  let state = null;
  let ticker = null;
  let busy = false;

  function grab() {
    el.prize = document.getElementById('prize');
    el.sub = document.getElementById('sub');
    el.cards = document.getElementById('cards');
    el.panel = document.getElementById('panel');
    el.streak = document.getElementById('streak');
  }

  /* --- серия ------------------------------------------------------------- */

  let shownStreak = null;         // чтобы «+1» вспыхивало только на росте

  function dayWord(n) {
    const tail = n % 100 > 10 && n % 100 < 20 ? 0 : n % 10;
    return tail === 1 ? 'день' : tail >= 2 && tail <= 4 ? 'дня' : 'дней';
  }

  function renderStreak(data) {
    /* Плашка серии: огонёк, счёт и следующая сумма — без правил словами.
       Механику объясняют сами числа, а абзац текста на этом экране читать
       никто не станет: игрок пришёл нажать карточку. */
    const waiting = data.status === 'cooldown';
    const cold = waiting && !data.streak;      // не угадал — огонёк потух
    const count = waiting ? data.streak : data.streak_day;

    el.streak.hidden = false;
    el.streak.className = 'streak' + (cold ? ' cold' : '');
    el.streak.innerHTML =
      '<span class="fire">' + (cold ? '🖤' : '🔥') + '</span>' +
      '<span class="streak-body">' +
        '<b class="streak-num">' +
          (cold ? 'Серия сгорела' : count + ' ' + dayWord(count)) + '</b>' +
        '<span class="streak-cap">' +
          (waiting ? 'завтра ' : 'в кейсе ') + WC.esc(data.prize) + '</span>' +
      '</span>' +
      (waiting ? '' : '<span class="streak-next">дальше<b>' +
        WC.esc(data.next_prize) + '</b></span>');

    if (shownStreak !== null && data.streak > shownStreak) {
      el.streak.classList.remove('grew');
      void el.streak.offsetWidth;
      el.streak.classList.add('grew');
    }
    shownStreak = data.streak;
  }

  function stopTimer() {
    if (ticker) { clearInterval(ticker); ticker = null; }
  }

  function timerTile() {
    return '<div class="tile"><div class="timer">' +
           '<span class="clock" id="clock">--:--</span>' +
           '<span class="cap">до следующего кейса</span></div>' +
           '<div class="progress"><i id="bar" style="width:0"></i></div></div>';
  }

  function startTimer(seconds) {
    stopTimer();
    const clockEl = document.getElementById('clock');
    const bar = document.getElementById('bar');
    if (!clockEl) return;
    const total = (state && state.cooldown) || 86400;
    let left = seconds;
    const tick = function () {
      clockEl.textContent = WC.clock(left);
      if (bar) {
        const done = Math.min(100, Math.max(0, (1 - left / total) * 100));
        bar.style.width = done.toFixed(1) + '%';
      }
      if (left <= 0) { stopTimer(); refresh(); return; }
      left -= 1;
    };
    tick();
    ticker = setInterval(tick, 1000);
  }

  /* --- экраны ----------------------------------------------------------- */

  function cardMarkup(i) {
    return '<span class="face">🎁<span class="num">' + (i + 1) + '</span></span>' +
           '<span class="back"><span class="sum"></span><span class="tag"></span></span>';
  }

  function renderCards(data) {
    el.sub.textContent = 'Одна из ' + data.cards + ' карточек прячет ' +
                         data.prize + '. Выбирай.';
    el.cards.className = 'cards';
    el.cards.innerHTML = '';
    for (let i = 0; i < data.cards; i++) {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'card';
      card.innerHTML = cardMarkup(i);
      card.addEventListener('click', function () { pick(i, card); });
      el.cards.appendChild(card);
    }
    el.panel.innerHTML = WC.tile('Как это работает',
      'Выигрышную карточку сервер выбрал заранее — до того, как ты нажал. ' +
      'Приз уходит на баланс сразу, повторно этот кейс уже не откроется.');
  }

  function renderReveal(data, animate) {
    const r = data.reveal;
    el.cards.className = 'cards locked';
    if (el.cards.children.length !== r.cards) {
      el.cards.innerHTML = '';
      for (let i = 0; i < r.cards; i++) {
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = cardMarkup(i);
        el.cards.appendChild(card);
      }
    }
    Array.prototype.forEach.call(el.cards.children, function (card, i) {
      const isWin = i === r.win;
      card.classList.add('revealed', isWin ? 'win' : 'empty');
      card.classList.toggle('picked', i === r.picked);
      card.classList.toggle('dim', i !== r.picked);
      card.querySelector('.back .sum').textContent = isWin ? r.prize : '$0.00';
      card.querySelector('.back .tag').textContent = isWin ? 'приз' : 'пусто';
    });

    const won = r.payout_cents > 0;
    el.sub.textContent = won ? 'Карточка ' + (r.picked + 1) + ' — есть!'
                             : 'Карточка ' + (r.picked + 1) + ' оказалась пустой.';
    el.panel.innerHTML =
      (won ? WC.tile('Выигрыш ' + r.payout, 'Уже на балансе.', 'win-tile')
           : WC.tile('Пусто', 'Приз лежал в карточке ' + (r.win + 1) +
                     '. Следующий кейс — новая раздача.')) + timerTile();
    startTimer(data.seconds_left);
    if (animate) {
      WC.setBalance(data.balance, won);
      if (won) { WC.confetti(); WC.haptic('success'); } else { WC.haptic('warning'); }
    }
  }

  function renderCooldown(data) {
    if (data.reveal) { renderReveal(data, false); return; }
    el.sub.textContent = 'Кейс на сегодня уже открыт.';
    el.cards.className = 'cards locked';
    el.cards.innerHTML = '';
    el.panel.innerHTML = WC.tile('Кейс получен',
      'Возвращайся, когда таймер добежит до нуля.') + timerTile();
    startTimer(data.seconds_left);
  }

  function renderSubscribe(data) {
    el.sub.textContent = 'Кейс открывается за подписку.';
    el.cards.className = 'cards locked';
    el.cards.innerHTML = '';

    const links = data.channels.map(function (ch) {
      const name = WC.esc(ch.title || 'Канал');
      return ch.url
        ? '<a class="channel" href="' + WC.esc(ch.url) + '" target="_blank" ' +
          'rel="noopener"><span class="ava">📢</span><span class="name">' + name +
          '</span><span class="go">›</span></a>'
        : '<div class="channel"><span class="ava">📢</span><span class="name">' +
          name + '</span></div>';
    }).join('');

    el.panel.innerHTML =
      '<div class="tile"><h2>Подпишись, чтобы открыть</h2>' +
      '<p>Нужны все каналы из списка.</p>' +
      '<div class="channels">' + links + '</div></div>' +
      '<button class="btn" id="check" type="button">✅ Проверить подписку</button>';

    document.getElementById('check').addEventListener('click', async function (e) {
      const button = e.currentTarget;
      button.classList.add('busy');
      try {
        const fresh = await WC.api('/api/subscription/check');
        if (fresh.status === 'subscribe') {
          WC.impact('rigid');
          button.classList.remove('busy');
          button.textContent = 'Ещё не всё — проверь список';
          setTimeout(function () {
            button.textContent = '✅ Проверить подписку';
          }, 1800);
          render(fresh);
          return;
        }
        WC.haptic('success');
        await open();
      } catch (err) {
        button.classList.remove('busy');
        fail(err);
      }
    });
  }

  /* --- поток ------------------------------------------------------------ */

  function render(data, bump) {
    state = data;
    stopTimer();
    document.getElementById('casino').textContent = data.casino;
    el.prize.textContent = data.prize;
    WC.setBalance(data.balance, bump);
    renderStreak(data);

    if (data.status === 'subscribe') renderSubscribe(data);
    else if (data.status === 'cooldown') renderCooldown(data);
    else renderCards(data);
  }

  function fail(err) {
    stopTimer();
    el.panel.innerHTML = WC.tile('Не вышло', WC.errorText(err), 'error');
    WC.banner(WC.errorText(err));
  }

  async function open() {
    try { render(await WC.api('/api/case/open')); } catch (err) { fail(err); }
  }

  async function refresh() {
    try { render(await WC.api('/api/state')); } catch (err) { fail(err); }
  }

  async function pick(index, node) {
    if (busy || !state || state.case_id === null) return;
    busy = true;
    el.cards.classList.add('locked');
    node.classList.add('opening');
    WC.impact('medium');
    try {
      const data = await WC.api('/api/case/pick',
                                { case_id: state.case_id, index: index });
      await WC.wait(420);                    // дать анимации карточки доиграть
      state = data;
      el.prize.textContent = data.prize;
      renderStreak(data);                    // серия либо выросла, либо сгорела
      if (data.pick === 'subscribe') { render(data); return; }
      if (data.reveal) { renderReveal(data, true); return; }
      render(data);
    } catch (err) {
      fail(err);
    } finally {
      busy = false;
    }
  }

  WC.register('case', {
    open: function () { grab(); open(); },
    refresh: function () { if (!busy) refresh(); },
  });
})();
