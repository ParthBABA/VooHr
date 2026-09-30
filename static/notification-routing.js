(function() {
  var ACTIVITY_TYPES = ['translation_ready', 'audio_ready', 'session_ready'];
  var MEETING_TYPES = ['meeting_reminder', 'meeting_event', 'memory_overdue', 'delivery_failed'];

  function categoryOf(n) {
    if (n && (n.category === 'activity' || n.category === 'meeting' || n.category === 'risk')) return n.category;
    var type = n && n.type;
    if (ACTIVITY_TYPES.indexOf(type) !== -1) return 'activity';
    if (MEETING_TYPES.indexOf(type) !== -1) return 'meeting';
    return 'risk';
  }

  function targetUrl(n) {
    var category = categoryOf(n);
    if (category === 'activity') {
      var query = [];
      if (n.source_session_id) query.push('session_id=' + encodeURIComponent(n.source_session_id));
      if (n.employee_id) query.push('employee_id=' + encodeURIComponent(n.employee_id));
      return '/workspace' + (query.length ? '?' + query.join('&') : '');
    }
    if (category === 'meeting') return '/meeting-tracker' + (n.employee_id ? '?employee_id=' + encodeURIComponent(n.employee_id) : '');
    return '/risk-drift?notification=' + encodeURIComponent(n.id);
  }

  // Mark a notification read and resolve once the server has (very likely)
  // received it. Callers navigate AFTER this resolves; a bare fire-and-forget
  // fetch followed by location.href is cancelled by the browser, which left
  // the red badge on. Waits for the CSRF token (the PUT is rejected without
  // it), uses keepalive so it survives a navigation, and never blocks the
  // caller for more than ~1.5s.
  function markRead(id) {
    if (!id) return Promise.resolve();
    var ready = window.csrfTokenReady ? window.csrfTokenReady.catch(function () {}) : Promise.resolve();
    var req = ready.then(function () {
      return fetch('/api/notifications/' + encodeURIComponent(id) + '/read', {
        method: 'PUT',
        keepalive: true,
        credentials: 'same-origin'
      });
    }).catch(function () {});
    var cap = new Promise(function (resolve) { setTimeout(resolve, 1500); });
    return Promise.race([req, cap]);
  }

  // Same contract as markRead, but for a batch: one request carrying a JSON
  // list of ids instead of one request per row. Resolves true/false, never
  // rejects, and never blocks for more than ~1.5s — callers use it to decide
  // whether to keep the optimistic UI or roll it back.
  function putJson(url, body) {
    var ready = window.csrfTokenReady ? window.csrfTokenReady.catch(function () {}) : Promise.resolve();
    var req = ready.then(function () {
      return fetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        keepalive: true,
        credentials: 'same-origin'
      });
    }).then(function (r) { return r.ok === true; }).catch(function () { return false; });
    var cap = new Promise(function (resolve) { setTimeout(function () { resolve(false); }, 1500); });
    return Promise.race([req, cap]);
  }

  // Mark many read in one round trip. Blank/duplicate ids are dropped so the
  // server never sees a list it has to re-parse twice.
  function markManyRead(ids) {
    var seen = {};
    var list = [];
    (ids || []).forEach(function (id) {
      var key = String(id == null ? '' : id);
      if (key && !seen[key]) { seen[key] = true; list.push(key); }
    });
    if (!list.length) return Promise.resolve(true);
    return putJson('/api/notifications/read', { ids: list });
  }

  // Mark-all-read. Resolves true/false on the same terms as markManyRead.
  function markAllRead() {
    return putJson('/api/notifications/read-all', {});
  }

  // The header bell badge. Shared so the Notifications hub can keep the badge
  // in step with its own rows — the hub and the bell are separate closures on
  // the same page. A total of 0 hides both the dot and the count.
  function setBadge(unread) {
    var dot = document.getElementById('notifDot');
    var count = document.getElementById('notifCount');
    if (dot) dot.style.display = unread > 0 ? '' : 'none';
    if (count) {
      count.style.display = unread > 0 ? '' : 'none';
      count.textContent = unread > 99 ? '99+' : String(unread || 0);
    }
  }

  // The badge number currently on screen, or null when it can't be read (no
  // element, or text that isn't a number). Read separately from setBadge so a
  // caller can step the count by a delta without owning the write — the bell
  // needs that to clear one row's worth of unread at a time, and it must be
  // able to tell "0 unread" apart from "I have no idea what the count is".
  function getBadgeCount() {
    var el = document.getElementById('notifCount');
    if (!el) return null;
    var n = parseInt(el.textContent, 10);
    return isNaN(n) ? null : n;
  }

  window.VooNotif = window.VooNotif || {};
  window.VooNotif.categoryOf = categoryOf;
  window.VooNotif.targetUrl = targetUrl;
  window.VooNotif.markRead = markRead;
  window.VooNotif.markManyRead = markManyRead;
  window.VooNotif.markAllRead = markAllRead;
  window.VooNotif.setBadge = setBadge;
  window.VooNotif.getBadgeCount = getBadgeCount;
})();

/* ── Panel row rendering ───────────────────────────────────────────────────
   Builds the bell dropdown's rows (`.notif-row`) in one place so every page
   that ships the panel renders an identical list. The markup/CSS live in
   style.css under the `.notif-panel` block; only the DOM construction is
   here. All text goes in via textContent — never innerHTML. */
(function () {
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function relTime(iso) {
    if (!iso) return '';
    var t = new Date(iso);
    if (isNaN(t.getTime())) return '';
    var diff = (Date.now() - t.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
    return t.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function absTime(iso) {
    var t = iso ? new Date(iso) : null;
    return t && !isNaN(t.getTime()) ? t.toLocaleString() : '';
  }

  // "Harshit Rana" → "HR". Falls back to a category initial when a row has no
  // employee name (job-completion notifications are about the session, not a
  // person), and to "•" when even the type is unknown.
  function initials(n) {
    var name = String((n && n.employee_name) || '').trim();
    if (!name) {
      var t = String((n && n.type) || '').trim();
      name = t ? t.charAt(0).toUpperCase() : '•';
      return name;
    }
    var parts = name.split(/\s+/).filter(Boolean);
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0].charAt(0) + parts[parts.length - 1].charAt(0)).toUpperCase();
  }

  // Swap an avatar back to its initials. Keeps the unread dot (it is a sibling
  // of the image inside the avatar, and `textContent =` would wipe it).
  function showInitials(avatar, n) {
    var dot = avatar.querySelector('.notif-row__unread-dot');
    avatar.textContent = initials(n);
    if (dot) avatar.appendChild(dot);
  }

  // The 34px circular slot: the employee's real photo when the API supplied
  // one, initials otherwise. The unread dot is always the last child so it
  // stacks on top of whichever is showing.
  function buildAvatar(n) {
    var avatar = el('span', 'notif-row__avatar');
    var photo = String((n && n.employee_photo) || '').trim();
    if (photo) {
      var img = document.createElement('img');
      img.className = 'notif-row__photo';
      // The name is already spelled out beside the row, so the picture is
      // decorative — keep it out of the accessibility tree.
      img.setAttribute('alt', '');
      img.setAttribute('aria-hidden', 'true');
      // A data-URL that fails to decode would otherwise leave a broken-image
      // glyph in the slot; fall back to initials instead.
      img.addEventListener('error', function () { showInitials(avatar, n); });
      img.src = photo;
      avatar.appendChild(img);
    } else {
      avatar.textContent = initials(n);
    }
    if (!n.read) avatar.appendChild(el('span', 'notif-row__unread-dot'));
    return avatar;
  }

  // The sentence shown next to the name.
  //   activity — the finishing line IS the message; there's no headline to lead
  //              with, and there may be no employee name at all.
  //   meeting  — the headline is the human phrase. The summary is the
  //              machine-ish "commitment pending: … · due <iso>" string the
  //              hub parses into badges, so it is deliberately not shown raw.
  //   risk     — headline plus the supporting summary.
  function messageOf(n) {
    var cat = window.VooNotif.categoryOf(n);
    if (cat === 'activity') return n.summary || n.headline || 'Ready';
    if (cat === 'meeting') return n.headline || 'Meeting update';
    if (n.headline && n.summary && n.summary !== n.headline) return n.headline + ' · ' + n.summary;
    return n.headline || n.summary || 'Notification';
  }

  // "Today" vs "Earlier", bucketed on the viewer's local calendar day so a
  // row that landed at 11:50 PM reads as "Today" right up to midnight. Rows
  // with no/unparseable created_at fall into "Earlier" rather than vanishing.
  function dayGroup(n) {
    var t = n && n.created_at ? new Date(n.created_at) : null;
    if (!t || isNaN(t.getTime())) return 'Earlier';
    var now = new Date();
    var sameDay = t.getFullYear() === now.getFullYear() &&
      t.getMonth() === now.getMonth() &&
      t.getDate() === now.getDate();
    return sameDay ? 'Today' : 'Earlier';
  }

  function buildRow(n, onOpen) {
    var row = el('button', 'notif-row' + (n.read ? '' : ' notif-row--unread'));
    row.type = 'button';
    row.setAttribute('data-id', n.id);

    var avatar = buildAvatar(n);
    row.appendChild(avatar);

    var body = el('span', 'notif-row__body protected-text');
    body.setAttribute('translate', 'no');

    var text = el('span', 'notif-row__text');
    if (n.employee_name) {
      text.appendChild(el('b', 'notif-row__name', n.employee_name));
      text.appendChild(document.createTextNode(' '));
    }
    text.appendChild(el('span', 'notif-row__message', messageOf(n)));
    body.appendChild(text);

    var time = el('span', 'notif-row__time', relTime(n.created_at));
    var abs = absTime(n.created_at);
    if (abs) time.title = abs;
    body.appendChild(time);

    row.appendChild(body);
    // A span, not a nested <button>: the row itself is the button (keyboard
    // reachable, one tab stop per row) so a real button inside it would be
    // invalid markup. The click handler below covers the whole row.
    row.appendChild(el('span', 'notif-row__action', 'View'));

    if (typeof onOpen === 'function') row.addEventListener('click', function () { onOpen(n); });
    return row;
  }

  // Renders `items` into `container`, inserting a "Today" / "Earlier" label
  // ahead of each bucket. Returns nothing; hides/shows the empty state via the
  // optional `empty` element passed in.
  function renderRows(container, items, onOpen, empty) {
    if (!container) return;
    container.innerHTML = '';
    var list = items || [];
    if (!list.length) {
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';

    var groups = { Today: [], Earlier: [] };
    list.forEach(function (n) { groups[dayGroup(n)].push(n); });

    ['Today', 'Earlier'].forEach(function (label) {
      var bucket = groups[label];
      if (!bucket.length) return;
      container.appendChild(el('div', 'notif-group__label', label));
      bucket.forEach(function (n) { container.appendChild(buildRow(n, onOpen)); });
    });
  }

  var V = window.VooNotif;
  V.relTime = V.relTime || relTime;
  V.initials = initials;
  V.buildNotifAvatar = buildAvatar;
  V.messageOf = messageOf;
  V.dayGroup = dayGroup;
  V.buildNotifRow = buildRow;
  V.renderNotifRows = renderRows;
})();
