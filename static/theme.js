(function () {
  var STORAGE_KEY = 'voovr-theme';

  function applyTheme(theme) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
  }

  // Runs immediately (this script tag is placed early, before CSS paints)
  // so there is no flash of the wrong theme on page load.
  var saved = 'dark';
  try { saved = localStorage.getItem(STORAGE_KEY) || 'dark'; } catch (error) {}
  applyTheme(saved);

  // Exposed so settings.html's toggle buttons can call this directly.
  window.voovrSetTheme = function (theme) {
    try { localStorage.setItem(STORAGE_KEY, theme); } catch (error) {}
    applyTheme(theme);
  };

  window.voovrGetTheme = function () {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  };
})();
