(function () {
  'use strict';

  var body = document.body;
  var menuToggle = document.querySelector('.support-menu-toggle');
  var closeButton = document.querySelector('.support-menu-close');
  var backdrop = document.querySelector('.support-backdrop');

  function setMenu(open) {
    if (!menuToggle || !backdrop) return;
    body.classList.toggle('support-menu-open', open);
    menuToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    menuToggle.setAttribute('aria-label', open ? 'Close support menu' : 'Open support menu');
    backdrop.hidden = !open;
    if (open && closeButton) closeButton.focus();
    if (!open) menuToggle.focus();
  }

  if (menuToggle) menuToggle.addEventListener('click', function () {
    setMenu(menuToggle.getAttribute('aria-expanded') !== 'true');
  });
  if (closeButton) closeButton.addEventListener('click', function () { setMenu(false); });
  if (backdrop) backdrop.addEventListener('click', function () { setMenu(false); });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && body.classList.contains('support-menu-open')) setMenu(false);
  });

  document.querySelectorAll('.support-chevron').forEach(function (button) {
    var panel = document.getElementById(button.getAttribute('aria-controls'));
    if (!panel) return;
    var parentLink = button.parentElement.querySelector('a[href]');
    var current = document.querySelector('.support-sidebar a[aria-current="page"]');
    var active = parentLink && current && (parentLink === current || panel.contains(current));
    var key = 'voohr-support-nav:' + button.getAttribute('aria-controls');
    var expanded = active;
    try {
      var saved = sessionStorage.getItem(key);
      if (saved !== null && !active) expanded = saved === 'true';
    } catch (error) {}
    function applyExpanded(value, persist) {
      button.setAttribute('aria-expanded', value ? 'true' : 'false');
      button.setAttribute('aria-label', (value ? 'Collapse ' : 'Expand ') + (parentLink ? parentLink.textContent.trim() : 'section'));
      panel.hidden = !value;
      if (persist) {
        try { sessionStorage.setItem(key, value ? 'true' : 'false'); } catch (error) {}
      }
    }
    applyExpanded(expanded, false);
    button.addEventListener('click', function () {
      applyExpanded(button.getAttribute('aria-expanded') !== 'true', true);
    });
  });

  var search = document.getElementById('support-search');
  var results = document.getElementById('support-search-results');
  var searchData = document.getElementById('support-search-data');
  if (search && results && searchData) {
    var items = [];
    try { items = JSON.parse(searchData.textContent || '[]'); } catch (error) {}
    search.addEventListener('input', function () {
      var query = search.value.trim().toLocaleLowerCase();
      results.replaceChildren();
      results.hidden = !query;
      if (!query) return;
      var matches = items.filter(function (item) {
        return item.label.toLocaleLowerCase().indexOf(query) !== -1;
      }).slice(0, 12);
      if (!matches.length) {
        var empty = document.createElement('li');
        empty.className = 'support-search-empty';
        empty.textContent = 'No matching topics or questions.';
        results.appendChild(empty);
        return;
      }
      matches.forEach(function (item) {
        var row = document.createElement('li');
        var link = document.createElement('a');
        link.href = item.url;
        link.textContent = item.label;
        row.appendChild(link);
        results.appendChild(row);
      });
    });
    document.addEventListener('click', function (event) {
      if (!event.target.closest('.support-search-wrap')) results.hidden = true;
    });
    search.addEventListener('focus', function () {
      if (search.value.trim()) results.hidden = false;
    });
  }
})();
