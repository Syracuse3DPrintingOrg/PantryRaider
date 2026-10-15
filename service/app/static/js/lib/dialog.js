/*
 * The page's own question box, in place of the browser's alert(), confirm()
 * and prompt().
 *
 * A browser box is titled with the page's address ("127.0.0.1 says" on a
 * kiosk), cannot be styled, and a prompt() cannot be typed into on a
 * touchscreen, because the on-screen keyboard (osk.js) only reaches fields in
 * the page. These three calls draw a Bootstrap dialog instead and answer with
 * a promise:
 *
 * PR.ask(message, opts)         Resolves to the text typed, or null for Cancel.
 *                               opts: default (the starting text), title,
 *                               ok, cancel (button labels), placeholder,
 *                               type, inputmode, maxlength.
 * PR.confirmBox(message, opts)  Resolves to true for OK, false otherwise.
 *                               opts: title, ok, cancel, danger (a red OK).
 * PR.notice(message, opts)      Resolves once it is dismissed. opts: title, ok.
 * PR.confirmSubmit(form, message, event, opts)
 *                               For data-on-submit="return PR.confirmSubmit(
 *                               this, 'Delete this rule?', event)": holds the
 *                               submit, asks, and submits the form again
 *                               (with the same button) after OK.
 *
 * Escape answers Cancel, Enter answers OK. Focus moves into the dialog and
 * goes back to whatever had it before. A new question replaces one nobody
 * answered, which answers that one Cancel. After an answer the dialog stays
 * over the page, invisible, for a moment, so the second tap of a double tap
 * does not land on the button underneath.
 *
 * Inside an open Bootstrap modal the dialog is drawn in that modal's element,
 * because Bootstrap pulls the focus back into its modal from anywhere else,
 * and the text box would lose it.
 */
(function () {
  var root = typeof globalThis !== 'undefined' ? globalThis : window;
  var PR = root.PR = root.PR || {};
  if (typeof document === 'undefined') return;

  // Above Bootstrap's modals (1055) and toasts (1090); below the on-screen
  // keyboard, the screensaver and the boot intro (2147482000 and up).
  var Z_INDEX = 1095;
  var GUARD_MS = 400;
  var seq = 0;

  var current = null;   // { wrap, settle, opener }

  function ensureStyle() {
    if (document.getElementById('pr-dialog-style')) return;
    var style = document.createElement('style');
    style.id = 'pr-dialog-style';
    style.textContent =
      '.pr-dialog-wrap{position:fixed;inset:0;z-index:' + Z_INDEX + ';}' +
      '.pr-dialog-wrap .modal-backdrop{position:absolute;z-index:0;}' +
      '.pr-dialog-wrap .modal{position:absolute;display:block;z-index:1;}' +
      '.pr-dialog-wrap.pr-dialog-done{opacity:0;}' +
      '.pr-dialog-text{white-space:pre-line;max-height:50vh;overflow-y:auto;margin:0;}' +
      '.pr-dialog-wrap .modal-footer .btn{min-width:6rem;min-height:44px;}';
    document.head.appendChild(style);
  }

  // The topmost open Bootstrap modal, if any (see the note at the top).
  function hostFor() {
    var open = document.querySelectorAll('.modal.show');
    for (var i = open.length - 1; i >= 0; i--) {
      if (!open[i].closest('.pr-dialog-wrap')) return open[i];
    }
    return document.body;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function focusables(box) {
    return Array.prototype.filter.call(
      box.querySelectorAll('button, input'),
      function (n) { return !n.disabled && n.getClientRects().length; });
  }

  // kind: 'ask' | 'confirm' | 'notice'. Resolves to the answer.
  function open(kind, message, opts) {
    opts = opts || {};
    // A question still waiting is answered Cancel and replaced.
    if (current) current.settle(false);
    ensureStyle();

    var id = 'pr-dialog-' + (++seq);
    var title = opts.title ? String(opts.title) : '';
    var wrap = el('div', 'pr-dialog-wrap');
    var backdrop = el('div', 'modal-backdrop show');
    var modal = el('div', 'modal show');
    modal.setAttribute('role', kind === 'notice' ? 'alertdialog' : 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.tabIndex = -1;
    var dlg = el('div', 'modal-dialog modal-dialog-centered');
    var content = el('div', 'modal-content');
    var body = el('div', 'modal-body');
    var text = el('p', 'pr-dialog-text', message == null ? '' : String(message));
    text.id = id + '-text';

    if (title) {
      var header = el('div', 'modal-header');
      var h = el('h5', 'modal-title', title);
      h.id = id + '-title';
      header.appendChild(h);
      content.appendChild(header);
      modal.setAttribute('aria-labelledby', h.id);
      if (text.textContent) modal.setAttribute('aria-describedby', text.id);
    } else {
      modal.setAttribute('aria-labelledby', text.id);
    }
    if (text.textContent) body.appendChild(text);

    var input = null;
    if (kind === 'ask') {
      input = el('input', 'form-control form-control-lg' + (text.textContent ? ' mt-3' : ''));
      input.type = opts.type || 'text';
      input.id = id + '-input';
      input.autocomplete = 'off';
      if (opts.inputmode) input.setAttribute('inputmode', opts.inputmode);
      if (opts.maxlength) input.maxLength = opts.maxlength;
      if (opts.placeholder) input.placeholder = opts.placeholder;
      input.value = opts.default == null ? '' : String(opts.default);
      input.setAttribute('aria-labelledby', title ? id + '-title' : text.id);
      if (title && text.textContent) input.setAttribute('aria-describedby', text.id);
      body.appendChild(input);
    }
    content.appendChild(body);

    var footer = el('div', 'modal-footer');
    var cancelBtn = null;
    if (kind !== 'notice') {
      cancelBtn = el('button', 'btn btn-secondary', opts.cancel || 'Cancel');
      cancelBtn.type = 'button';
      footer.appendChild(cancelBtn);
    }
    var okBtn = el('button', 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary'), opts.ok || 'OK');
    okBtn.type = 'button';
    footer.appendChild(okBtn);
    content.appendChild(footer);

    dlg.appendChild(content);
    modal.appendChild(dlg);
    wrap.appendChild(backdrop);
    wrap.appendChild(modal);

    var opener = document.activeElement;
    var resolveFn;
    var promise = new Promise(function (resolve) { resolveFn = resolve; });
    var done = false;

    function settle(ok) {
      if (done) return;
      done = true;
      var answer;
      if (kind === 'ask') answer = ok ? input.value : null;
      else if (kind === 'confirm') answer = !!ok;
      if (current && current.wrap === wrap) current = null;
      wrap.removeEventListener('keydown', onKey, true);
      // Invisible, but still catching taps for a moment.
      wrap.classList.add('pr-dialog-done');
      setTimeout(function () { if (wrap.parentNode) wrap.parentNode.removeChild(wrap); }, GUARD_MS);
      // A replacement dialog takes the focus itself.
      if (!current && opener && opener.isConnected && opener.focus && opener !== document.body) {
        try { opener.focus({ preventScroll: true }); } catch (e) { /* gone */ }
      } else if (!current && document.activeElement && wrap.contains(document.activeElement)) {
        document.activeElement.blur();
      }
      resolveFn(answer);
    }

    function onKey(ev) {
      if (ev.key === 'Escape' || ev.key === 'Esc') {
        ev.preventDefault();
        ev.stopPropagation();   // a Bootstrap modal underneath must stay open
        settle(false);
        return;
      }
      if (ev.key === 'Enter') {
        ev.stopPropagation();
        // Enter on a button presses that button; anywhere else it answers OK.
        if (ev.target && ev.target.tagName === 'BUTTON') return;
        ev.preventDefault();
        settle(true);
        return;
      }
      if (ev.key === 'Tab') {
        var list = focusables(modal);
        if (!list.length) return;
        var first = list[0], last = list[list.length - 1];
        var at = document.activeElement;
        if (ev.shiftKey && (at === first || !modal.contains(at))) {
          ev.preventDefault();
          last.focus();
        } else if (!ev.shiftKey && (at === last || !modal.contains(at))) {
          ev.preventDefault();
          first.focus();
        }
      }
    }

    okBtn.addEventListener('click', function () { settle(true); });
    if (cancelBtn) cancelBtn.addEventListener('click', function () { settle(false); });
    // A tap on the dimmed page is not an answer; keep the focus in the box.
    backdrop.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
    modal.addEventListener('mousedown', function (ev) {
      if (ev.target === modal) ev.preventDefault();
    });
    wrap.addEventListener('keydown', onKey, true);

    hostFor().appendChild(wrap);
    current = { wrap: wrap, settle: settle };

    var first = input || okBtn;
    try { first.focus({ preventScroll: true }); } catch (e) { first.focus(); }
    if (input && input.select) { try { input.select(); } catch (e) { /* email and number */ } }
    return promise;
  }

  function ask(message, opts) { return open('ask', message, opts); }
  function confirmBox(message, opts) { return open('confirm', message, opts); }
  function notice(message, opts) {
    return open('notice', message, opts).then(function () { return undefined; });
  }

  // Forms already answered OK, let through on their second submit.
  var passed = typeof WeakSet !== 'undefined' ? new WeakSet() : null;

  function confirmSubmit(form, message, event, opts) {
    if (!form || !passed) return true;
    if (passed.has(form)) { passed.delete(form); return true; }
    var submitter = event && event.submitter ? event.submitter : null;
    confirmBox(message, opts).then(function (ok) {
      if (!ok) return;
      passed.add(form);
      if (typeof form.requestSubmit === 'function') {
        if (submitter && submitter.form === form) form.requestSubmit(submitter);
        else form.requestSubmit();
      } else {
        passed.delete(form);
        form.submit();
      }
    });
    return false;
  }

  PR.ask = ask;
  PR.confirmBox = confirmBox;
  PR.notice = notice;
  PR.confirmSubmit = confirmSubmit;
})();
