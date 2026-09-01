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
  var saved = localStorage.getItem(STORAGE_KEY) || 'dark';
  applyTheme(saved);

  // Exposed so settings.html's toggle buttons can call this directly.
  window.voovrSetTheme = function (theme) {
    localStorage.setItem(STORAGE_KEY, theme);
    applyTheme(theme);
  };

  window.voovrGetTheme = function () {
    return localStorage.getItem(STORAGE_KEY) || 'dark';
  };
})();
