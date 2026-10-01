(function () {
  var KEY = 'voovr_back_to';
  var INFO = ['/about','/careers','/cookies','/privacy','/terms','/subprocessors','/status',
              '/about.html','/careers.html','/cookie-policy.html','/privacy-policy.html',
              '/terms-of-service.html','/subprocessors.html','/status.html'];

  function isInfo(path) { return INFO.indexOf(path) !== -1; }
  function isSignIn(path) {
    return path === '/login' || path === '/login.html' || path === '/signin' || path === '/signin.html';
  }

  try {
    if (document.referrer) {
      var ref = new URL(document.referrer);
      if (ref.origin === location.origin && !isInfo(ref.pathname) && !isSignIn(ref.pathname) &&
          ref.pathname !== location.pathname) {
        sessionStorage.setItem(KEY, ref.pathname + ref.search + ref.hash);
      }
    }
  } catch (e) {}

  document.addEventListener('DOMContentLoaded', function () {
    var links = document.querySelectorAll('.legal-back, [data-back-link]');
    var target = '/';
    try { target = sessionStorage.getItem(KEY) || '/'; } catch (e) {}
    try {
      var destination = new URL(target, location.origin);
      if (target.charAt(0) !== '/' || target.indexOf('//') === 0 ||
          destination.origin !== location.origin || isSignIn(destination.pathname)) target = '/';
    } catch (e) { target = '/'; }
    links.forEach(function (a) { a.setAttribute('href', target); });
  });
})();