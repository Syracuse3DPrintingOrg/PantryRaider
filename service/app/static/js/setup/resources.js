// Resources pane loader (FoodAssistant-do2u).
//
// Fetches /setup/resources and fills the cards in _pane_resources.html. Each
// card stays hidden until its metric arrives, so the pane shows only what
// this environment can measure. While the pane is open (and the tab visible)
// it refreshes every few seconds; leaving the pane stops the polling.

const _RES_REFRESH_MS = 4000;
let _resTimer = null;
let _resBusy = false;

function _resShow(id, show) {
  document.getElementById(id)?.classList.toggle('d-none', !show);
}

function _resText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

// Fill a usage bar and colour it by pressure: green, amber from 75%, red from 90%.
function _resBarFill(id, pct) {
  const bar = document.getElementById(id);
  if (!bar) return;
  const p = Math.min(100, Math.max(0, pct || 0));
  bar.style.width = p + '%';
  bar.className = p >= 90 ? 'res-bad' : (p >= 75 ? 'res-warn' : '');
}

function _resGb(bytes) {
  const gb = (bytes || 0) / 1073741824;
  return gb >= 100 ? Math.round(gb) + ' GB'
    : gb >= 1 ? gb.toFixed(1) + ' GB'
    : Math.round((bytes || 0) / 1048576) + ' MB';
}

function _resRenderCpu(cpu) {
  _resShow('res-cpu-card', !!cpu);
  if (!cpu) return;
  _resText('res-cpu-big', cpu.percent != null ? Math.round(cpu.percent) + '%' : '--');
  _resBarFill('res-cpu-bar', cpu.percent);
  const cores = cpu.per_core || [];
  const wrap = document.getElementById('res-cores');
  if (wrap) {
    wrap.classList.toggle('d-none', cores.length < 2);
    if (cores.length !== wrap.children.length) {
      wrap.innerHTML = cores.map(() => '<div class="res-core"><div style="height:0%"></div></div>').join('');
    }
    cores.forEach((p, i) => {
      const fill = wrap.children[i] && wrap.children[i].firstElementChild;
      if (fill) fill.style.height = Math.min(100, Math.max(2, p)) + '%';
    });
  }
  const bits = [];
  if (cpu.cores) bits.push(cpu.cores + ' core' + (cpu.cores === 1 ? '' : 's'));
  if (cpu.load) bits.push('load ' + cpu.load.map(v => v.toFixed(2)).join(' / '));
  _resText('res-cpu-sub', bits.join(', '));
}

function _resRenderMemory(mem) {
  _resShow('res-mem-card', !!mem);
  if (!mem) return;
  _resText('res-mem-big', Math.round(mem.percent) + '%');
  _resBarFill('res-mem-bar', mem.percent);
  let sub = _resGb(mem.used) + ' of ' + _resGb(mem.total) + ' in use';
  if (mem.swap) sub += '. Swap: ' + _resGb(mem.swap.used) + ' of ' + _resGb(mem.swap.total) + '.';
  _resText('res-mem-sub', sub);
}

function _resRenderTemperature(temp) {
  _resShow('res-temp-card', !!temp);
  if (!temp) return;
  const c = temp.celsius;
  _resText('res-temp-big', Math.round(c) + '°C');
  const f = Math.round(c * 9 / 5 + 32);
  _resText('res-temp-sub', f + '°F. ' + (c >= 80 ? 'Running hot: improve the airflow around the device.'
    : c >= 70 ? 'Warm but within limits.' : 'Comfortable operating temperature.'));
}

function _resRenderPower(power) {
  _resShow('res-power-card', !!power);
  if (!power) return;
  const big = document.getElementById('res-power-big');
  const live = power.live || [];
  const since = power.since_boot || [];
  if (big) {
    big.textContent = live.length ? 'Attention' : 'Good';
    big.className = 'res-big ' + (live.length ? 'text-danger' : 'text-success');
    big.style.fontSize = '1.4rem';
  }
  let sub = '';
  if (live.length) sub = live.join('. ') + '.';
  else if (since.length) sub = 'Fine now. Earlier: ' + since.join('. ').toLowerCase() + '.';
  else sub = 'Power supply and cooling are keeping up.';
  _resText('res-power-sub', sub);
}

function _resRenderDisks(disks) {
  _resShow('res-disk-card', !!(disks && disks.length));
  const wrap = document.getElementById('res-disks');
  if (!wrap || !disks || !disks.length) return;
  const esc = s => String(s || '').replace(/[<>&"]/g, '');
  wrap.innerHTML = disks.map((d, i) => {
    const p = Math.min(100, Math.max(0, d.percent || 0));
    const kind = p >= 90 ? 'res-bad' : (p >= 75 ? 'res-warn' : '');
    return '<div class="res-disk-row">'
      + '<div class="res-disk-label"><span><strong>' + esc(d.label) + '</strong></span>'
      + '<span>' + _resGb(d.used) + ' used, ' + _resGb(d.free) + ' free of ' + _resGb(d.total) + '</span></div>'
      + '<div class="res-bar"><div class="' + kind + '" style="width:' + p + '%"></div></div>'
      + '</div>';
  }).join('');
}

// Beszel dashboard link (FoodAssistant-4kz2): shown only when the server
// returned one (beszel_enabled + a hub URL are both set); otherwise the link
// stays hidden and the built-in snapshot below is all there is.
function _resRenderBeszel(url) {
  const link = document.getElementById('res-beszel-link');
  if (!link) return;
  if (url) {
    link.href = url;
    link.classList.remove('d-none');
  } else {
    link.classList.add('d-none');
  }
}

async function loadResources() {
  if (_resBusy) return;
  _resBusy = true;
  try {
    const d = await fetch('setup/resources').then(r => r.json());
    _resRenderBeszel(d.beszel_url);
    _resRenderCpu(d.cpu);
    _resRenderMemory(d.memory);
    _resRenderTemperature(d.temperature);
    _resRenderPower(d.power);
    _resRenderDisks(d.disks);
    _resShow('res-uptime-card', !!d.uptime);
    if (d.uptime) _resText('res-uptime-big', d.uptime.text);
    const any = d.cpu || d.memory || d.temperature || d.power
      || (d.disks && d.disks.length) || d.uptime;
    _resShow('res-empty', !any);
    _resText('res-updated', 'Updated ' + new Date().toLocaleTimeString());
  } catch (e) {
    _resText('res-updated', 'Readings are unavailable right now.');
  } finally {
    _resBusy = false;
  }
}

// Poll only while the Resources pane is the visible pane and the browser tab
// is in the foreground; a hidden kiosk page never keeps hitting the server.
function _resTick() {
  const pane = document.getElementById('pane-resources');
  if (!pane || !pane.classList.contains('active') || document.hidden) {
    _resStop();
    return;
  }
  loadResources();
}

function _resStop() {
  if (_resTimer) { clearInterval(_resTimer); _resTimer = null; }
}

function startResourcesRefresh() {
  loadResources();
  _resStop();
  _resTimer = setInterval(_resTick, _RES_REFRESH_MS);
}

// Resuming a backgrounded tab while the pane is open restarts the polling.
document.addEventListener('visibilitychange', function () {
  const pane = document.getElementById('pane-resources');
  if (!document.hidden && pane && pane.classList.contains('active') && !_resTimer) {
    startResourcesRefresh();
  }
});

// Arriving on the pane through a #pane-resources hash (a bookmark, or a save
// elsewhere that reloaded the page) activates the pill without its onclick,
// so start the refresh from the tab-shown event too. Idempotent with onclick.
document.addEventListener('DOMContentLoaded', function () {
  const pill = document.querySelector('.side-menu [data-bs-target="#pane-resources"]');
  if (pill) pill.addEventListener('shown.bs.tab', startResourcesRefresh);
});
