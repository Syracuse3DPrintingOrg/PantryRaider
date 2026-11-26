// Settings, Home Assistant, Status icons on kitchen screens.
//
// Draws the list of status items from the #status-icons-data block, lets the
// owner add, edit, reorder and remove them, and saves the whole list with the
// on/off switch and the corner through /setup/save. The server cleans every
// item again (services/status_icons.normalize_items), so this only has to
// collect what the rows say.
(function () {
  'use strict';

  const ICONS = [
    ['fridge', 'Fridge'], ['door', 'Door'], ['thermometer', 'Thermometer'],
    ['oven', 'Oven'], ['pot', 'Sous vide or pot'], ['plug', 'Plug'],
    ['fan', 'Fan'], ['generic', 'Dot'],
  ];
  const MATCHES = [
    ['on', 'is on, open or heating'], ['off', 'is off or closed'],
    ['state', 'is in a state I type'], ['above', 'reads above a number'],
  ];

  let data = {items: [], satellite: false, thermometers: [], thermometer_names: []};
  let items = [];
  let entitiesLoaded = false;

  function readData() {
    const el = document.getElementById('status-icons-data');
    if (!el) return false;
    try { data = Object.assign(data, JSON.parse(el.textContent || '{}')); } catch (e) { }
    items = (data.items || []).map(it => Object.assign({}, it));
    return true;
  }

  function newId() {
    return Math.random().toString(16).slice(2, 10);
  }

  function opt(value, text, current) {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = text;
    if (value === current) o.selected = true;
    return o;
  }

  function field(labelText, control) {
    const wrap = document.createElement('label');
    wrap.className = 'd-flex flex-column small me-2 mb-2';
    const span = document.createElement('span');
    span.className = 'text-secondary mb-1';
    span.textContent = labelText;
    wrap.appendChild(span);
    wrap.appendChild(control);
    return wrap;
  }

  function input(value, placeholder, width, onInput) {
    const el = document.createElement('input');
    el.type = 'text';
    el.className = 'form-control form-control-sm';
    el.value = value == null ? '' : String(value);
    if (placeholder) el.placeholder = placeholder;
    el.style.width = width;
    el.disabled = !!data.satellite;
    el.addEventListener('input', () => onInput(el.value));
    return el;
  }

  function select(choices, current, width, onChange) {
    const el = document.createElement('select');
    el.className = 'form-select form-select-sm';
    el.style.width = width;
    el.disabled = !!data.satellite;
    choices.forEach(([v, t]) => el.appendChild(opt(v, t, current)));
    el.addEventListener('change', () => onChange(el.value));
    return el;
  }

  function iconButton(icon, title, onClick, disabled) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-outline-secondary btn-sm me-1';
    b.title = title;
    b.setAttribute('aria-label', title);
    b.innerHTML = '<i class="bi ' + icon + '"></i>';
    b.disabled = !!disabled || !!data.satellite;
    b.addEventListener('click', onClick);
    return b;
  }

  function thermometerChoices() {
    const out = [['', 'Any thermometer']];
    (data.thermometers || []).forEach((id, i) => {
      const name = (data.thermometer_names || [])[i] || id;
      out.push([String(id).toUpperCase(), name]);
    });
    return out;
  }

  function render() {
    const list = document.getElementById('status-icons-list');
    if (!list) return;
    list.innerHTML = '';
    const empty = document.getElementById('status-icons-empty');
    if (empty) empty.hidden = items.length > 0;
    items.forEach((item, index) => {
      const row = document.createElement('div');
      row.className = 'border rounded p-2 mb-2';
      row.setAttribute('data-status-item', item.id || '');
      const line = document.createElement('div');
      line.className = 'd-flex flex-wrap align-items-end';

      line.appendChild(field('Name', input(item.name, 'Fridge door', '11rem', v => { item.name = v; })));
      line.appendChild(field('Icon', select(ICONS, item.icon || 'generic', '10rem', v => { item.icon = v; })));
      line.appendChild(field('Shows when', select(
        [['ha', 'a Home Assistant device'], ['thermometer', 'a thermometer reports']],
        item.source || 'ha', '14.5rem', v => { item.source = v; render(); })));

      if (item.source === 'thermometer') {
        line.appendChild(field('Which', select(thermometerChoices(), item.device_id || '', '12rem',
          v => { item.device_id = v; })));
      } else {
        const ent = input(item.entity_id, 'binary_sensor.fridge_door', '15rem', v => { item.entity_id = v.trim(); });
        ent.setAttribute('list', 'status-icons-entities');
        ent.addEventListener('focus', loadEntities);
        ent.addEventListener('change', () => pickEntity(item, ent.value.trim()));
        line.appendChild(field('Home Assistant entity', ent));
        line.appendChild(field('Shows while it', select(MATCHES, item.match || 'on', '13rem',
          v => { item.match = v; render(); })));
        if (item.match === 'state') {
          line.appendChild(field('State (or several, with commas)',
            input(item.state, 'preheating, heating', '12rem', v => { item.state = v; })));
        } else if (item.match === 'above') {
          const num = input(item.above == null ? '' : item.above, '10', '7rem', v => { item.above = v; });
          num.type = 'number';
          num.step = 'any';
          line.appendChild(field('Above', num));
        }
      }

      const tools = document.createElement('div');
      tools.className = 'd-flex align-items-end mb-2 ms-auto';
      tools.appendChild(iconButton('bi-arrow-up', 'Move up', () => move(index, -1), index === 0));
      tools.appendChild(iconButton('bi-arrow-down', 'Move down', () => move(index, 1), index === items.length - 1));
      tools.appendChild(iconButton('bi-trash', 'Remove', () => { items.splice(index, 1); render(); }));
      line.appendChild(tools);
      row.appendChild(line);
      list.appendChild(row);
    });
  }

  function move(index, by) {
    const to = index + by;
    if (to < 0 || to >= items.length) return;
    const [it] = items.splice(index, 1);
    items.splice(to, 0, it);
    render();
  }

  let entityRows = [];
  function pickEntity(item, entityId) {
    const hit = entityRows.find(e => e.entity_id === entityId);
    if (!hit) return;
    if (!item.name || item.name === item.entity_id) item.name = hit.name;
    if (hit.match === 'above' && item.match === 'on') {
      item.match = 'above';
      if (item.above == null || item.above === '') item.above = 10;
    }
    item.entity_id = entityId;
    render();
  }

  async function loadEntities() {
    if (entitiesLoaded || data.satellite) return;
    entitiesLoaded = true;
    try {
      const r = await fetch('setup/status-icons/entities');
      const body = await r.json();
      entityRows = (body && body.entities) || [];
      const dl = document.getElementById('status-icons-entities');
      if (!dl) return;
      dl.innerHTML = '';
      entityRows.forEach(e => {
        const o = document.createElement('option');
        o.value = e.entity_id;
        o.label = e.name + (e.state ? ' (' + e.state + (e.unit ? ' ' + e.unit : '') + ')' : '');
        dl.appendChild(o);
      });
    } catch (e) {
      entitiesLoaded = false;
    }
  }

  window.statusIconsAdd = function (kind) {
    if (data.satellite) return;
    if (kind === 'thermometer') {
      items.push({id: newId(), name: 'Thermometer', icon: 'thermometer', source: 'thermometer', device_id: ''});
    } else if (kind === 'fridge') {
      items.push({id: newId(), name: 'Fridge door', icon: 'fridge', source: 'ha', entity_id: '', match: 'on'});
    } else {
      items.push({id: newId(), name: '', icon: 'plug', source: 'ha', entity_id: '', match: 'on'});
    }
    render();
    loadEntities();
  };

  function collect() {
    return items.map(it => {
      const out = {id: it.id, name: (it.name || '').trim(), icon: it.icon, source: it.source};
      if (it.source === 'thermometer') {
        out.device_id = it.device_id || '';
      } else {
        out.entity_id = (it.entity_id || '').trim();
        out.match = it.match || 'on';
        if (out.match === 'state') out.state = it.state || '';
        if (out.match === 'above') out.above = it.above === '' || it.above == null ? null : Number(it.above);
      }
      return out;
    });
  }

  function problems(list) {
    const out = [];
    list.forEach((it, i) => {
      const n = it.name || ('Item ' + (i + 1));
      if (it.source !== 'ha') return;
      if (!/^[a-z0-9_]+\.[a-z0-9_]+$/.test((it.entity_id || '').toLowerCase())) {
        out.push(n + ' needs a Home Assistant entity, like binary_sensor.fridge_door.');
      } else if (it.match === 'state' && !(it.state || '').trim()) {
        out.push(n + ' needs the state to watch for.');
      } else if (it.match === 'above' && (it.above == null || isNaN(it.above))) {
        out.push(n + ' needs the number to watch for.');
      }
    });
    return out;
  }

  window.statusIconsSave = async function (btn) {
    const el = document.getElementById('status-icons-result');
    const list = collect();
    const bad = problems(list);
    if (bad.length) {
      if (el) {
        el.innerHTML = '';
        const span = document.createElement('span');
        span.className = 'text-danger';
        span.textContent = bad.join(' ');
        el.appendChild(span);
      }
      return;
    }
    const orig = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving...'; }
    if (el) el.innerHTML = '';
    try {
      const r = await fetch('setup/save', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          status_icons_enabled: document.getElementById('status_icons_enabled')?.checked || false,
          status_icons_position: document.getElementById('status_icons_position')?.value || 'top_right',
          status_icons: list,
        }),
      });
      if (!r.ok) throw new Error('Could not save (' + r.status + ').');
      if (el) el.innerHTML = '<span class="text-success"><i class="bi bi-check-circle me-1"></i>Saved. Screens already showing status icons update within a few seconds, the others on their next page.</span>';
    } catch (e) {
      if (el) {
        el.innerHTML = '';
        const span = document.createElement('span');
        span.className = 'text-danger';
        span.textContent = String(e && e.message ? e.message : e);
        el.appendChild(span);
      }
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = orig; }
    }
  };

  function init() {
    if (!readData()) return;
    render();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
