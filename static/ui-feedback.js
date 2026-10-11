/* Shared, non-blocking feedback. Messages are shown as toasts in the lower‑right corner. */
(function () {
  'use strict';
  // inject minimal toast styles once
  if (!document.getElementById('uiToastStyles')) {
    var style = document.createElement('style');
    style.id = 'uiToastStyles';
    style.textContent = `
      .ui-toast{
        min-width:200px;max-width:360px;padding:0.75rem 1rem;
        border-radius:0.375rem;box-shadow:0 4px 12px rgba(0,0,0,.15);
        background:#fff;color:#111;font-size:0.875rem;line-height:1.4;
        opacity:1;transform:translateX(0);
        transition:opacity .3s ease,transform .3s ease;
      }
      .ui-toast--success{border-left:4px solid #22c55e;}
      .ui-toast--error{border-left:4px solid #ef4444;}
    `;
    document.head.appendChild(style);
  }
  function ensureToastContainer() {
    var container = document.getElementById('toastContainer');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toastContainer';
      container.style.cssText = 'position:fixed;bottom:1rem;right:1rem;display:flex;flex-direction:column-reverse;gap:0.5rem;z-index:9999;pointer-events:none;';
      document.body.appendChild(container);
    }
    return container;
  }

  function show(message, options) {
    options = options || {};
    var target = typeof options.target === 'string' ? document.querySelector(options.target) : options.target;
    if (target) {
      // legacy inline feedback element
      target.className = 'ui-feedback ui-feedback--' + (options.kind === 'success' ? 'success' : 'error');
      target.setAttribute('role', options.kind === 'success' ? 'status' : 'alert');
      target.setAttribute('aria-live', options.kind === 'success' ? 'polite' : 'assertive');
      target.setAttribute('tabindex', '-1');
      target.hidden = false;
      target.textContent = message;
      target.focus({preventScroll: true});
      target.scrollIntoView({block: 'nearest'});
      return;
    }
    // toast style feedback
    var container = ensureToastContainer();
    var toast = document.createElement('div');
    toast.className = 'ui-toast ui-toast--' + (options.kind === 'success' ? 'success' : 'error');
    toast.setAttribute('role', options.kind === 'success' ? 'status' : 'alert');
    toast.setAttribute('aria-live', options.kind === 'success' ? 'polite' : 'assertive');
    toast.textContent = message;
    toast.style.pointerEvents = 'auto';
    container.appendChild(toast);
    // auto dismiss
    setTimeout(function () {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(100%)';
      toast.addEventListener('transitionend', function () { toast.remove(); });
    }, 4000);
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
