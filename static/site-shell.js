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
