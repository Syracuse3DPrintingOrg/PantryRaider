/*
 * Shared DOM helpers, on the one global namespace window.PR.
 *
 * There is no bundler, so every page script is a classic script and reaches
 * these through PR.esc(...) and friends. base.html loads this file plain (not
 * deferred) in the head, before anything else that might call it; the
 * standalone pages (setup, start, login) load it themselves.
 *
 * esc(s)            Text for HTML: element content and quoted attribute values.
 *                   Escapes & < > " ' and the backtick, and copes with null,
 *                   undefined and numbers.
 * escAttrJs(v)      A value written as an argument inside a data-on-* action
 *                   (see below): a JSON literal, then attribute-escaped. The
 *                   browser decodes the attribute before the action is read,
 *                   so HTML escaping alone would let a quote in the value close
 *                   the string. Numbers and booleans stay as they are; null
 *                   and undefined become "".
 * escAttrJsStr(v)   The same, but always hands the handler a string, for the
 *                   setup panes whose handlers compare ids as strings.
 * fetchJSON(url, o) fetch() plus the app's error wording. Resolves to the
 *                   parsed JSON body (null for an empty reply) and throws an
 *                   Error whose message is the server's own explanation when
 *                   the status is not 2xx. A plain object body is sent as
 *                   JSON. Cross-site write protection is the browser's own
 *                   Sec-Fetch-Site header, which the server checks, so there is
 *                   no token to attach; the request stays same-origin with the
 *                   session cookie. Pass a page-relative URL (no leading
 *                   slash) so it works behind Home Assistant Ingress.
 *
 * Actions: data-on-click and friends
 *
 * The Content-Security-Policy refuses inline script, so markup never carries
 * onclick= or any other on*= attribute. It names what to run with
 * data-on-<event>="fn(args)" instead, and one listener per event type on the
 * document runs it. The value is not JavaScript: it is a short call list read
 * by the small parser below, and anything outside that shape is refused.
 *
 *   data-on-click="saveThing(this, 'id', 3)"
 *   data-on-change="setMode(this.value)"
 *   data-on-submit="return PR.confirmSubmit(this, 'Delete this rule?', event)"
 *
 * Allowed: calls separated by ';', 'return' on the last one (returning false
 * cancels the event, as an inline handler did), '!', literals (strings,
 * numbers, true, false, null, arrays, objects), and these values: this (the
 * element carrying the attribute), event, and a short list of their members
 * (this.value, this.checked, this.dataset.x, this.closest(sel),
 * event.preventDefault() and so on). A bare name calls a function the page
 * itself defined (a global such as saveThing, or a dotted path such as
 * PR.copyText); built-in browser functions are refused. A question before a
 * submit goes through PR.confirmSubmit (lib/dialog.js), not confirm().
 * Bubbling events run from the target outwards and stop when an action calls
 * event.stopPropagation(); error and load run on the element itself.
 * data-on-enter is a keydown of the Enter key, with the default (a form
 * submit, say) already cancelled.
 *
 * clickById(id), hideById(id), setValueById(id, v), setTextById(id, v)
 *                   Small element chores for actions to call, so a button
 *                   that opens a file picker or closes a card needs no page
 *                   function of its own. clickById returns false, so
 *                   "return PR.clickById('x')" also cancels a link.
 * on(name, fn)      Registers a page function under a name for actions to
 *                   call, for scripts that keep their functions private.
 */
(function () {
  var root = typeof globalThis !== 'undefined' ? globalThis : window;
  var PR = root.PR = root.PR || {};

  var ENTITIES = {
    '&': '&amp;', '<': '&lt;', '>': '&gt;',
    '"': '&quot;', "'": '&#39;', '`': '&#96;'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"'`]/g, function (c) {
      return ENTITIES[c];
    });
  }

  function escAttrJs(v) {
    return esc(JSON.stringify(v == null ? '' : v));
  }

  function escAttrJsStr(v) {
    return esc(JSON.stringify(v == null ? '' : String(v)));
  }

  async function errText(r) {
    if (typeof root.prErrText === 'function') return root.prErrText(r);
    try {
      var j = await r.json();
      if (j && (j.detail || j.message || j.error)) return j.detail || j.message || j.error;
    } catch (e) { /* not JSON */ }
    return 'The server returned an error (HTTP ' + r.status + ').';
  }

  async function fetchJSON(url, opts) {
    var o = Object.assign({ credentials: 'same-origin' }, opts || {});
    var headers = new Headers(o.headers || {});
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    var b = o.body;
    if (b != null && typeof b === 'object' && !(b instanceof FormData)
        && !(b instanceof URLSearchParams) && !(b instanceof Blob)
        && !(b instanceof ArrayBuffer)) {
      o.body = JSON.stringify(b);
      if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    }
    o.headers = headers;
    var r = await fetch(url, o);
    if (!r.ok) {
      var err = new Error(await errText(r));
      err.status = r.status;
      err.response = r;
      throw err;
    }
    var text = await r.text();
    if (!text) return null;
    try { return JSON.parse(text); } catch (e) {
      throw new Error('The server sent a reply the page could not read.');
    }
  }

  // ---- actions --------------------------------------------------------

  // Members an action may read or call on this (the element) and event.
  var MEMBER_OK = {
    value: 1, checked: 1, dataset: 1, key: 1, closest: 1, select: 1,
    remove: 1, preventDefault: 1, stopPropagation: 1, focus: 1, blur: 1,
    click: 1, form: 1, files: 1, id: 1, name: 1, shiftKey: 1, ctrlKey: 1,
    metaKey: 1, altKey: 1, textContent: 1
  };
  var NAME_BAD = { constructor: 1, __proto__: 1, prototype: 1 };
  var registry = Object.create(null);

  function on(name, fn) { registry[name] = fn; }

  var WS = /^\s+/;
  var NUM = /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/;
  var IDENT = /^[A-Za-z_$][\w$]*/;
  var ESCAPES = { n: '\n', t: '\t', r: '\r', b: '\b', f: '\f', v: '\v', 0: '\0' };

  function tokenize(src) {
    var toks = [], i = 0, n = src.length, m;
    while (i < n) {
      var rest = src.slice(i), c = src[i];
      if ((m = WS.exec(rest))) { i += m[0].length; continue; }
      if (c === '"' || c === "'") {
        var j = i + 1, out = '';
        for (;;) {
          if (j >= n) throw new SyntaxError('unterminated string');
          var d = src[j];
          if (d === c) break;
          if (d === '\\') {
            var e = src[j + 1];
            j += 2;
            if (e === 'x') { out += String.fromCharCode(parseInt(src.substr(j, 2), 16)); j += 2; }
            else if (e === 'u') { out += String.fromCharCode(parseInt(src.substr(j, 4), 16)); j += 4; }
            else if (Object.prototype.hasOwnProperty.call(ESCAPES, e)) out += ESCAPES[e];
            else if (e !== '\n') out += e;
            continue;
          }
          out += d;
          j++;
        }
        toks.push({ t: 'str', v: out });
        i = j + 1;
        continue;
      }
      if ((m = NUM.exec(rest))) {
        toks.push({ t: 'num', v: Number(m[0]) });
        i += m[0].length;
        continue;
      }
      if ((m = IDENT.exec(rest))) {
        toks.push({ t: 'id', v: m[0] });
        i += m[0].length;
        continue;
      }
      if (rest.slice(0, 2) === '?.') { toks.push({ t: 'p', v: '.' }); i += 2; continue; }
      if ('.(),;![]{}:'.indexOf(c) >= 0) { toks.push({ t: 'p', v: c }); i++; continue; }
      throw new SyntaxError('unexpected ' + JSON.stringify(c));
    }
    return toks;
  }

  function rootOf(node) {
    while (node.k === 'get' || node.k === 'call') node = node.k === 'get' ? node.o : node.f;
    return node;
  }

  // Nodes: lit, this, event, glob (a page function name), get (member),
  // call, not, arr, obj.
  function parse(src) {
    var toks = tokenize(src), pos = 0;
    function peek(v) { var t = toks[pos]; return !!t && t.t === 'p' && t.v === v; }
    function eat(v) {
      if (!peek(v)) throw new SyntaxError('expected ' + v);
      pos++;
    }
    function list(close) {
      var items = [];
      while (!peek(close)) {
        items.push(expr());
        if (!peek(close)) eat(',');
      }
      eat(close);
      return items;
    }
    function primary() {
      var t = toks[pos++];
      if (!t) throw new SyntaxError('unexpected end');
      if (t.t === 'str' || t.t === 'num') return { k: 'lit', v: t.v };
      if (t.t === 'p' && t.v === '!') return { k: 'not', e: expr() };
      if (t.t === 'p' && t.v === '(') { var inner = expr(); eat(')'); return inner; }
      if (t.t === 'p' && t.v === '[') return { k: 'arr', a: list(']') };
      if (t.t === 'p' && t.v === '{') {
        var props = [];
        while (!peek('}')) {
          var kt = toks[pos++];
          if (!kt || kt.t === 'p' || NAME_BAD[kt.v]) throw new SyntaxError('bad key');
          eat(':');
          props.push([String(kt.v), expr()]);
          if (!peek('}')) eat(',');
        }
        eat('}');
        return { k: 'obj', a: props };
      }
      if (t.t === 'id') {
        if (t.v === 'true') return { k: 'lit', v: true };
        if (t.v === 'false') return { k: 'lit', v: false };
        if (t.v === 'null') return { k: 'lit', v: null };
        if (t.v === 'undefined') return { k: 'lit', v: undefined };
        if (t.v === 'this') return { k: 'this' };
        if (t.v === 'event') return { k: 'event' };
        if (NAME_BAD[t.v]) throw new SyntaxError('refused name ' + t.v);
        return { k: 'glob', v: t.v };
      }
      throw new SyntaxError('unexpected ' + t.v);
    }
    function postfix() {
      var node = primary();
      for (;;) {
        var r = rootOf(node);
        if (peek('.')) {
          pos++;
          var t = toks[pos++];
          if (!t || t.t !== 'id' || NAME_BAD[t.v]) throw new SyntaxError('bad member');
          if (r.k === 'glob') {
            if (node.k !== 'glob' && node.k !== 'get') throw new SyntaxError('cannot chain on a page function result');
          } else if (r.k === 'this' || r.k === 'event') {
            var inDataset = node.k === 'get' && node.v === 'dataset';
            if (!inDataset && !MEMBER_OK[t.v]) throw new SyntaxError('refused member ' + t.v);
          } else {
            throw new SyntaxError('bad member');
          }
          node = { k: 'get', o: node, v: t.v };
        } else if (peek('(')) {
          pos++;
          if (r.k === 'glob' && node.k !== 'glob' && node.k !== 'get') {
            throw new SyntaxError('cannot chain on a page function result');
          }
          if (r.k !== 'glob' && node.k !== 'get') throw new SyntaxError('bad call');
          node = { k: 'call', f: node, a: list(')') };
        } else {
          return node;
        }
      }
    }
    function expr() {
      var node = postfix();
      var r = rootOf(node);
      if (r.k === 'glob' && node.k !== 'call') throw new SyntaxError('a page function can only be called');
      return node;
    }
    var stmts = [], ret = false;
    while (pos < toks.length) {
      if (peek(';')) { pos++; continue; }
      if (ret) throw new SyntaxError('return must come last');
      var tk = toks[pos];
      if (tk.t === 'id' && tk.v === 'return') { pos++; ret = true; }
      stmts.push(expr());
      if (pos < toks.length) eat(';');
    }
    return { s: stmts, r: ret };
  }

  var cache = new Map();
  function compile(src) {
    var p = cache.get(src);
    if (!p) { p = parse(src); cache.set(src, p); }
    return p;
  }

  function isNative(fn) {
    try { return /\[native code\]/.test(Function.prototype.toString.call(fn)); } catch (e) { return true; }
  }

  function evaluate(node, el, ev) {
    switch (node.k) {
      case 'lit': return node.v;
      case 'this': return el;
      case 'event': return ev;
      case 'not': return !evaluate(node.e, el, ev);
      case 'arr': return node.a.map(function (x) { return evaluate(x, el, ev); });
      case 'obj': {
        var o = {};
        node.a.forEach(function (kv) { o[kv[0]] = evaluate(kv[1], el, ev); });
        return o;
      }
      case 'glob': {
        if (Object.prototype.hasOwnProperty.call(registry, node.v)) return registry[node.v];
        return root[node.v];
      }
      case 'get': {
        var base = evaluate(node.o, el, ev);
        return base == null ? undefined : base[node.v];
      }
      case 'call': {
        var f = node.f, fn, self;
        var fromPage = rootOf(f).k === 'glob';
        if (f.k === 'glob') {
          fn = evaluate(f, el, ev);
        } else {
          self = evaluate(f.o, el, ev);
          if (self == null) return undefined;   // a missing element, like ?.
          fn = self[f.v];
        }
        if (typeof fn !== 'function') throw new TypeError(f.v + ' is not a function');
        if (fromPage && isNative(fn)) {
          throw new TypeError(f.v + ' is not a page function');
        }
        var args = node.a.map(function (x) { return evaluate(x, el, ev); });
        return fn.apply(self, args);
      }
    }
    throw new TypeError('bad action');
  }

  function runAction(src, el, ev) {
    var prog = compile(src), result;
    for (var i = 0; i < prog.s.length; i++) result = evaluate(prog.s[i], el, ev);
    return prog.r ? result : undefined;
  }

  var BUBBLING = ['click', 'dblclick', 'change', 'input', 'keydown', 'keyup', 'submit',
    'dragstart', 'dragend', 'dragenter', 'dragover', 'dragleave', 'drop'];
  var ON_TARGET = ['error', 'load'];

  // The event as the action sees it: the same event, except currentTarget is
  // the element carrying the attribute (as it was for an inline handler), not
  // the document the listener sits on. Methods stay bound to the real event.
  function eventFor(ev, node) {
    if (typeof Proxy !== 'function') return ev;
    return new Proxy(ev, {
      get: function (target, key) {
        if (key === 'currentTarget') return node;
        var v = target[key];
        return typeof v === 'function' ? v.bind(target) : v;
      }
    });
  }

  function fire(node, type, ev) {
    var src = node.getAttribute('data-on-' + type);
    try {
      if (runAction(src, node, eventFor(ev, node)) === false) ev.preventDefault();
    } catch (e) {
      if (root.console) root.console.error('data-on-' + type + '="' + src + '" failed:', e);
    }
  }

  if (typeof document !== 'undefined' && document && document.addEventListener && !root.__prActions) {
    root.__prActions = true;
    BUBBLING.forEach(function (type) {
      var attr = 'data-on-' + type;
      document.addEventListener(type, function (ev) {
        var enter = type === 'keydown' && ev.key === 'Enter';
        for (var node = ev.target; node && node.nodeType !== 9; node = node.parentNode) {
          if (node.nodeType !== 1) continue;
          if (node.hasAttribute(attr)) fire(node, type, ev);
          if (enter && !ev.cancelBubble && node.hasAttribute('data-on-enter')) {
            ev.preventDefault();
            fire(node, 'enter', ev);
          }
          if (ev.cancelBubble) break;
        }
      });
    });
    ON_TARGET.forEach(function (type) {
      document.addEventListener(type, function (ev) {
        var node = ev.target;
        if (node && node.nodeType === 1 && node.hasAttribute('data-on-' + type)) fire(node, type, ev);
      }, true);
    });
  }

  // Small element chores that used to be one-line inline handlers.
  function byId(id) { return document.getElementById(id); }
  function clickById(id) { var el = byId(id); if (el) el.click(); return false; }
  function hideById(id) { var el = byId(id); if (el) el.classList.add('d-none'); }
  function setValueById(id, v) { var el = byId(id); if (el) el.value = v; }
  function setTextById(id, v) { var el = byId(id); if (el) el.textContent = v; }

  // Focus goes back to whatever opened a dialog once it closes. Bootstrap does
  // this only for a data-bs-toggle trigger; the dialogs a page script opens
  // (inventory Edit and Move, the recipe previews) otherwise left focus on the
  // page body, so a keyboard user started over from the top. When the list
  // behind the dialog re-rendered while it was open, the button with the same
  // action in the new list takes focus instead.
  function trackModalOpeners(doc) {
    var openers = new WeakMap();
    function visible(el) {
      return !!(el && el.isConnected && (el.offsetParent !== null || el.getClientRects().length));
    }
    doc.addEventListener('show.bs.modal', function (ev) {
      var modal = ev.target;
      var opener = ev.relatedTarget || doc.activeElement;
      if (!opener || opener === doc.body || modal.contains(opener)) return;
      openers.set(modal, {
        el: opener,
        action: opener.getAttribute ? opener.getAttribute('data-on-click') : null,
        id: opener.id || '',
      });
    });
    doc.addEventListener('hidden.bs.modal', function (ev) {
      var modal = ev.target;
      var rec = openers.get(modal);
      openers.delete(modal);
      if (!rec) return;
      var active = doc.activeElement;
      // Leave focus alone when something else already took it on purpose.
      if (active && active !== doc.body && !modal.contains(active)) return;
      var target = rec.el;
      if (!visible(target) && rec.id) target = doc.getElementById(rec.id);
      if (!visible(target) && rec.action) {
        var list = doc.querySelectorAll('[data-on-click]');
        for (var i = 0; i < list.length; i++) {
          if (list[i].getAttribute('data-on-click') === rec.action) { target = list[i]; break; }
        }
      }
      if (!visible(target)) {
        var dd = rec.el && rec.el.closest ? rec.el.closest('.dropdown') : null;
        target = dd ? dd.querySelector('[data-bs-toggle="dropdown"]') : null;
      }
      if (visible(target) && target.focus) {
        try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
      }
    });
  }
  if (typeof document !== 'undefined' && document.addEventListener && typeof WeakMap !== 'undefined') {
    try { trackModalOpeners(document); } catch (e) { /* focus return is a nicety */ }
  }

  PR.clickById = clickById;
  PR.hideById = hideById;
  PR.setValueById = setValueById;
  PR.setTextById = setTextById;
  PR.esc = esc;
  PR.escAttrJs = escAttrJs;
  PR.escAttrJsStr = escAttrJsStr;
  PR.fetchJSON = fetchJSON;
  PR.on = on;
  PR.parseAction = parse;
  PR.runAction = runAction;

  if (typeof module !== 'undefined' && module.exports) module.exports = PR;
})();
