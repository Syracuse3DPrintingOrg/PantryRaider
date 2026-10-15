// Weather page (templates/weather.html).
  // The server fetches wttr.in's JSON API (the same reliable source the Stream
  // Deck weather widget uses) and we render it here, so the page no longer
  // depends on the flaky panel PNG or the kiosk's own internet (afqd).
  const loadingEl = document.getElementById('weatherLoading');
  const bodyEl = document.getElementById('weatherBody');
  const errEl = document.getElementById('weatherError');


  let wxDaysData = [];   // kept so a click can look up the day's detail
  let wxOpenIdx = -1;    // index of the expanded day, or -1 when collapsed

  function statBlock(label, value) {
    if (value == null || value === '') return '';
    return `<div class="col"><div class="wx-stat text-center"><div class="small text-secondary">${PR.esc(label)}</div><div class="fw-semibold">${value}</div></div></div>`;
  }

  function renderHourStrip(hours, unit) {
    if (!hours || !hours.length) return '';
    const cells = hours.map(h => `
      <div class="wx-hour">
        <div class="small text-secondary">${PR.esc(h.time)}</div>
        <i class="bi bi-${PR.esc(h.icon || 'cloud')} text-info"></i>
        <div class="small fw-semibold">${PR.esc(h.temp)}&deg;</div>
        ${h.precip != null ? `<div class="small text-info">${PR.esc(h.precip)}%</div>` : ''}
      </div>`).join('');
    return `<div class="wx-hours mt-2">${cells}</div>`;
  }

  function renderDetail(idx) {
    const detailEl = document.getElementById('wxDetail');
    const d = wxDaysData[idx];
    if (!d) { detailEl.classList.add('d-none'); detailEl.innerHTML = ''; return; }
    const stats = [
      statBlock('High', `${PR.esc(d.hi)}&deg;${PR.esc(d.unit)}`),
      statBlock('Low', `${PR.esc(d.lo)}&deg;${PR.esc(d.unit)}`),
      d.precip != null ? statBlock('Rain', `${PR.esc(d.precip)}%`) : '',
      d.wind != null ? statBlock('Wind', `${PR.esc(d.wind)} ${PR.esc(d.wind_unit || '')}`) : '',
      d.sunrise ? statBlock('Sunrise', PR.esc(d.sunrise)) : '',
      d.sunset ? statBlock('Sunset', PR.esc(d.sunset)) : '',
    ].join('');
    detailEl.innerHTML = `
      <div class="card">
        <div class="card-body p-2">
          <div class="d-flex align-items-center justify-content-between mb-2">
            <div class="fw-semibold"><i class="bi bi-${PR.esc(d.icon || 'cloud')} text-info me-1"></i>${PR.esc(d.label)} <span class="text-secondary small">${PR.esc(d.desc)}</span></div>
            <button type="button" class="btn-close btn-close-white" aria-label="Close" data-on-click="closeDetail()"></button>
          </div>
          <div class="row row-cols-3 row-cols-md-6 g-2">${stats}</div>
          ${renderHourStrip(d.hourly, d.unit)}
        </div>
      </div>`;
    detailEl.classList.remove('d-none');
  }

  function closeDetail() {
    wxOpenIdx = -1;
    renderDetail(-1);
    document.querySelectorAll('#wxDays .card').forEach(c => c.classList.remove('wx-open'));
  }

  function toggleDay(idx) {
    if (wxOpenIdx === idx) { closeDetail(); return; }
    wxOpenIdx = idx;
    renderDetail(idx);
    document.querySelectorAll('#wxDays .card').forEach((c, i) => c.classList.toggle('wx-open', i === idx));
  }

  // Enter or Space on a focused day card opens it, like a button.
  function dayKey(ev, idx) {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      toggleDay(idx);
    }
  }

  function render(fc) {
    const c = fc.current || {};
    document.getElementById('wxIcon').className = 'bi wx-hero-icon bi-' + (c.icon || 'cloud');
    document.getElementById('wxTemp').innerHTML = `${PR.esc(c.temp)}&deg;${PR.esc(c.unit)}`;
    document.getElementById('wxDesc').textContent = c.desc || '';
    const noteEl = document.getElementById('wxNote');
    noteEl.textContent = fc.note || '';
    noteEl.classList.toggle('d-none', !fc.note);
    document.getElementById('wxFeels').innerHTML = `${PR.esc(c.feels)}&deg;`;
    document.getElementById('wxHumidity').textContent = `${c.humidity}%`;
    document.getElementById('wxWind').textContent = `${c.wind} ${c.wind_unit}`;
    const days = fc.days || [];
    wxDaysData = days;
    closeDetail();
    document.getElementById('wxDays').innerHTML = days.map((d, i) => `
      <div class="wx-day">
        <div class="card h-100 text-center" role="button" tabindex="0" data-on-click="toggleDay(${i})" data-on-keydown="dayKey(event, ${i})">
          <div class="card-body p-2">
            <div class="fw-semibold small mb-1">${PR.esc(d.label)}</div>
            <i class="bi bi-${PR.esc(d.icon || 'cloud')} wx-day-icon text-info d-block mb-1"></i>
            <div class="small text-secondary text-truncate mb-1">${PR.esc(d.desc)}</div>
            <div class="fs-6 fw-semibold">${PR.esc(d.hi)}&deg;<span class="text-secondary"> / ${PR.esc(d.lo)}&deg;${PR.esc(d.unit)}</span></div>
            ${d.precip != null ? `<div class="small text-info"><i class="bi bi-droplet"></i> ${PR.esc(d.precip)}%</div>` : ''}
          </div>
        </div>
      </div>`).join('');
  }

  async function reloadWeather() {
    loadingEl.classList.remove('d-none');
    bodyEl.classList.add('d-none');
    errEl.classList.add('d-none');
    try {
      const r = await fetch('ui/weather/data', { cache: 'no-store' });
      const d = await r.json();
      if (!d.ok || !d.forecast) throw new Error(d.error || 'unavailable');
      render(d.forecast);
      loadingEl.classList.add('d-none');
      bodyEl.classList.remove('d-none');
    } catch (e) {
      loadingEl.classList.add('d-none');
      const detail = document.getElementById('weatherErrorDetail');
      if (detail) detail.textContent = e.message && e.message !== 'unavailable' ? 'Reason: ' + e.message : '';
      errEl.classList.remove('d-none');
    }
  }

  async function saveWeatherSettings(btn) {
    const out = document.getElementById('wxSaveResult');
    const location = document.getElementById('wxLocation')?.value || '';
    const units = document.getElementById('wxUnits')?.value || 'f';
    const server = document.getElementById('wxServer')?.value.trim() || '';
    const orig = btn.innerHTML; btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
    if (out) out.textContent = '';
    try {
      const r = await fetch('setup/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ streamdeck_weather_location: location,
                               streamdeck_weather_units: units, weather_api_base: server }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.detail || 'Save failed');
      if (out) out.innerHTML = '<span class="text-success"><i class="bi bi-check-circle"></i></span>';
      reloadWeather();
    } catch (e) {
      if (out) out.innerHTML = '<span class="text-danger">' + e.message + '</span>';
    } finally { btn.disabled = false; btn.innerHTML = orig; }
  }

  reloadWeather();
  setInterval(reloadWeather, 10 * 60 * 1000);
