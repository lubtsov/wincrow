/* Профиль: те же цифры, что в боте, плюс сид provably fair.

   Экран только читает: ни одной операции с деньгами здесь нет, поэтому и
   запросов всего один — /api/profile. */

(function () {
  function stat(label, value, cls) {
    return '<div class="stat ' + (cls || '') + '">' +
           '<span class="stat-cap">' + label + '</span>' +
           '<b>' + WC.esc(value) + '</b></div>';
  }

  function render(p) {
    const nick = p.username ? '@' + p.username : 'ID ' + p.id;
    // «Итог» сервер присылает только админу — у игрока поля просто нет.
    const net = p.net === null || p.net === undefined ? '' :
      stat('Итог', p.net, String(p.net).charAt(0) === '-' ? 'bad' : 'good');
    return (
      '<div class="tile profile-head">' +
        '<div class="ava-big">' + WC.esc((p.name || '?').slice(0, 1).toUpperCase()) + '</div>' +
        '<div class="who"><b>' + WC.esc(p.name) + '</b>' +
        '<span class="sub">' + WC.esc(nick) + '</span></div>' +
        '<div class="who-balance"><span class="cap">Баланс</span><b>' +
        WC.esc(p.balance) + '</b></div>' +
      '</div>' +

      '<div class="stats">' +
        stat('Сыграно', p.played) +
        stat('Оборот', p.wagered) +
        stat('Получено', p.won) +
        net +
      '</div>' +

      '<div class="tile"><h2>🎁 Кейсы</h2><p>Открыто: <b>' + p.cases.opened +
        '</b> · получено <b>' + WC.esc(p.cases.paid) + '</b></p>' +
        '<p class="sub">' + (p.cases.streak
          ? '🔥 Серия ' + p.cases.streak + ' · следующий кейс ' +
            WC.esc(p.cases.next_prize)
          : '🖤 Серии нет · следующий кейс ' + WC.esc(p.cases.next_prize)) +
        '</p></div>' +

      '<div class="tile"><h2>👥 Друзья</h2><p>Приглашено: <b>' + p.referrals +
        '</b> · уровень <b>' + p.level + '</b> (' + p.percent + '% с их ' +
        'пополнений)<br>Заработано: <b>' + WC.esc(p.referral_earned) +
        '</b> · с чатов <b>' + WC.esc(p.chat_earned) + '</b></p>' +
        '<p class="sub">Ссылка и условия — в боте, раздел «Пригласить».</p></div>' +

      '<div class="tile"><h2>💳 Касса</h2><p>Пополнено: <b>' +
        WC.esc(p.deposited) + '</b></p>' +
        '<p class="sub">Пополнение и вывод — в боте: там подтверждение платежа ' +
        'и заявки.</p></div>' +

      fair(p) +

      '<div class="tile"><h2>🔒 Честная игра</h2>' +
        '<p>Отдача всех игр — <b>' + Math.round(p.rtp * 100) + '%</b>. Результат ' +
        'каждого раунда считается из сида до броска, и хеш сида публикуется ' +
        'заранее.</p>' +
        '<p class="sub">Сменить свой client_seed и раскрыть прошлый серверный — ' +
        'в боте, «Игры» → «Честная игра».</p></div>'
    );
  }

  function fair(p) {
    if (!p.fair) {
      return WC.tile('Сид', 'Появится после первого раунда.');
    }
    return '<div class="tile"><h2>Сид текущей серии</h2>' +
           '<p class="mono">' + WC.esc(p.fair.hash) + '</p>' +
           '<p class="sub">client_seed <span class="mono">' +
           WC.esc(p.fair.client_seed) + '</span> · раундов на сиде ' +
           p.fair.nonce + '</p></div>';
  }

  async function load() {
    const box = document.getElementById('profile');
    if (!box.innerHTML) {
      box.innerHTML = '<div class="tile"><p class="sub">Загружаем…</p></div>';
    }
    try {
      const data = await WC.api('/api/profile');
      WC.setBalance(data.balance);
      box.innerHTML = render(data);
    } catch (err) {
      box.innerHTML = WC.tile('Не вышло', WC.errorText(err), 'error');
    }
  }

  WC.register('profile', { open: load, refresh: load });
})();
