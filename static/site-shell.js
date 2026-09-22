/* Shared navigation accessibility and essential-cookie notice. */
document.addEventListener('DOMContentLoaded', function () {
  var sidebar = document.querySelector('.sidebar');
  if (sidebar) {
    sidebar.setAttribute('aria-label', 'Main navigation');
    sidebar.querySelectorAll('.nav-item').forEach(function (link) {
      var label = link.textContent.trim() || link.getAttribute('title');
      if (label) { link.setAttribute('aria-label', label); link.title = label; }
    });
    // An always-available icon rail replaces the old, unwired mobile button.
    var obsoleteToggle = document.getElementById('mobileToggle');
    if (obsoleteToggle) obsoleteToggle.remove();
  }
  document.querySelectorAll('.table-wrap, .table-container').forEach(function (table) {
    table.tabIndex = 0;
    table.setAttribute('role', 'region');
    table.setAttribute('aria-label', 'Scrollable table');
  });
  if (!document.querySelector('.auth-page, .legal-page')) return;
  try { if (localStorage.getItem('cookieConsent') === 'true') return; } catch (error) {}
  var notice = document.createElement('aside');
  notice.className = 'cookie-notice';
  notice.setAttribute('aria-label', 'Cookie information');
  notice.innerHTML = '<p>VooVr uses essential cookies to keep you signed in and protect your account. <a href="/privacy#cookies">Learn about cookies</a>.</p><button type="button" class="auth-secondary">Got it</button>';
  notice.querySelector('button').addEventListener('click', function () {
    try { localStorage.setItem('cookieConsent', 'true'); } catch (error) {}
    notice.remove();
  });
  document.body.appendChild(notice);
});

/* ── Protected-text copy/selection deterrent ─────────────────────────
   Delegated document-level handling so dynamically rendered sensitive
   content stays protected with no per-element listeners. UI-level
   deterrent only (not a security boundary). Copy/cut and right-click are
   blocked ONLY when the selection/target is inside `.protected-text`;
   everything else — inputs, textareas, selects, contenteditable, normal
   nav/controls elsewhere — behaves normally. */
(function () {
  var INTERACTIVE = 'input, textarea, select, [contenteditable="true"]';

  // If the currently focused element is an editable control, always allow
  // copy/cut — the user legitimately owns text they are entering/editing.
  function editableIsFocused() {
    var el = document.activeElement;
    return !!(el && el.closest && el.closest(INTERACTIVE));
  }

  // True when any edge of the current selection lies inside protected text.
  function selectionTouchesProtected() {
    try {
      var sel = window.getSelection();
      if (!sel || sel.isCollapsed || sel.rangeCount === 0) return false;
      var start = sel.anchorNode;
      var end = sel.focusNode;
      var startEl = start && start.nodeType === 1 ? start : start && start.parentElement;
      var endEl = end && end.nodeType === 1 ? end : end && end.parentElement;
      return !!((startEl && startEl.closest('.protected-text')) ||
                (endEl && endEl.closest('.protected-text')));
    } catch (err) { return false; }
  }

  // True for right-clicks on protected content, except right-clicks inside
  // an editable control (which keeps its native menu for editing actions).
  function rightClickOnProtected(target) {
    if (!target || !target.closest) return false;
    if (target.closest(INTERACTIVE)) return false;
    return !!target.closest('.protected-text');
  }

  document.addEventListener('copy', function (e) {
    if (editableIsFocused()) return;
    if (selectionTouchesProtected()) e.preventDefault();
  });

  document.addEventListener('cut', function (e) {
    if (editableIsFocused()) return;
    if (selectionTouchesProtected()) e.preventDefault();
  });

  document.addEventListener('contextmenu', function (e) {
    if (rightClickOnProtected(e.target)) e.preventDefault();
  });
})();
