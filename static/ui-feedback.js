/* Shared, non-blocking feedback. Messages are always inserted as text. */
(function () {
  'use strict';
  function show(message, options) {
    options = options || {};
    var target = typeof options.target === 'string' ? document.querySelector(options.target) : options.target;
    if (!target) {
      target = Array.from(document.querySelectorAll('[data-feedback]')).find(function (el) {
        return el.parentElement.getClientRects().length && getComputedStyle(el.parentElement).display !== 'none';
      });
    }
    if (!target) {
      target = document.getElementById('pageFeedback');
      if (!target) {
        target = document.createElement('div');
        target.id = 'pageFeedback';
        var container = document.querySelector('.content-area, main, .main-content') || document.body;
        container.prepend(target);
      }
    }
    target.className = 'ui-feedback ui-feedback--' + (options.kind === 'success' ? 'success' : 'error');
    target.setAttribute('role', options.kind === 'success' ? 'status' : 'alert');
    target.setAttribute('aria-live', options.kind === 'success' ? 'polite' : 'assertive');
    target.setAttribute('tabindex', '-1');
    target.hidden = false;
    target.textContent = message;
    target.focus({preventScroll: true});
    target.scrollIntoView({block: 'nearest'});
  }

  function clear(target) {
    document.querySelectorAll(target || '[data-feedback], #pageFeedback').forEach(function (el) {
      el.hidden = true;
      el.textContent = '';
    });
  }

  var pending = false;
  function ask(message, options) {
    options = options || {};
    if (pending) return Promise.resolve(false);
    pending = true;
    return new Promise(function (resolve) {
      var previous = document.activeElement;
      var dialog = document.createElement('dialog');
      dialog.className = 'ui-confirm';
      dialog.setAttribute('aria-labelledby', 'uiConfirmTitle');
      dialog.setAttribute('aria-describedby', 'uiConfirmMessage');
      dialog.innerHTML = '<h2 id="uiConfirmTitle">Confirm action</h2><p id="uiConfirmMessage"></p>' +
        '<div class="ui-confirm-actions"><button type="button" class="btn btn-outline" data-cancel>Cancel</button>' +
        '<button type="button" class="btn btn-primary" data-accept>Continue</button></div>';
      dialog.querySelector('h2').textContent = options.title || 'Confirm action';
      dialog.querySelector('p').textContent = message;
      dialog.querySelector('[data-accept]').textContent = options.accept || 'Continue';
      function finish(accepted) {
        dialog.close();
        dialog.remove();
        pending = false;
        if (previous && previous.isConnected) previous.focus();
        resolve(accepted);
      }
      dialog.querySelector('[data-cancel]').onclick = function () { finish(false); };
      dialog.querySelector('[data-accept]').onclick = function () { finish(true); };
      dialog.addEventListener('cancel', function (event) { event.preventDefault(); finish(false); });
      document.body.appendChild(dialog);
      dialog.showModal();
      dialog.querySelector('[data-cancel]').focus();
    });
  }
  window.VooVrUI = {show: show, clear: clear, ask: ask};
})();
