// Pantry audit page (templates/audit.html).
(function () {
  "use strict";

  var setup = document.getElementById("auditSetup");
  var active = document.getElementById("auditActive");
  var sel = document.getElementById("locationSelect");
  var startBtn = document.getElementById("startBtn");
  var stopBtn = document.getElementById("stopBtn");
  var applyCard = document.getElementById("applyCard");
  var applyBtn = document.getElementById("applyBtn");
  var applyResult = document.getElementById("applyResult");
  var pollTimer = null;
  var discrepant = 0;        // counted items whose amount differs from Grocy
  var applyDone = false;     // keep the result visible after a run


  async function loadLocations() {
    // "All areas" is always the default and a valid choice, so start is ready
    // even if the per-area list cannot load (an offline Grocy just means you
    // count everything). The specific areas are added after it when they load.
    var head = '<option value="__all__">All areas</option>';
    try {
      var r = await fetch("../audit/locations", { headers: { "Accept": "application/json" } });
      var data = await r.json();
      if (!r.ok) {
        sel.innerHTML = head;
        startBtn.disabled = false;
        return;
      }
      var locs = (data && data.locations) || [];
      var html = head;
      for (var i = 0; i < locs.length; i++) {
        html += '<option value="' + PR.esc(locs[i].name) + '">' +
          PR.esc(locs[i].name) + ' (' + locs[i].item_count + ')</option>';
      }
      sel.innerHTML = html;
      startBtn.disabled = false;
    } catch (e) {
      sel.innerHTML = head;
      startBtn.disabled = false;
    }
  }

  sel.addEventListener("change", function () {
    startBtn.disabled = !sel.value;
  });

  startBtn.addEventListener("click", async function () {
    if (!sel.value) { return; }
    startBtn.disabled = true;
    try {
      await fetch("../audit/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location: sel.value })
      });
      enterActive();
    } catch (e) {
      startBtn.disabled = false;
    }
  });

  stopBtn.addEventListener("click", async function () {
    try { await fetch("../audit/stop", { method: "POST" }); } catch (e) {}
    leaveActive();
  });

  function showApplyResult(kind, html) {
    applyResult.innerHTML = '<div class="alert alert-' + kind + ' py-2 mb-0">' + html + "</div>";
    applyResult.classList.remove("d-none");
  }

  applyBtn.addEventListener("click", async function () {
    var n = discrepant;
    var noun = n === 1 ? "item" : "items";
    var ok = await PR.confirmBox(
      "Items you have not scanned are left alone.",
      { title: "Set your inventory to the amounts you counted for " + n + " " + noun + "?", ok: "Apply" });
    if (!ok) { return; }
    applyBtn.disabled = true;
    try {
      var r = await fetch("../audit/apply", {
        method: "POST",
        headers: { "Accept": "application/json" }
      });
      var data = await r.json();
      if (!r.ok) {
        showApplyResult("danger", PR.esc(data.detail || "The corrections could not be applied."));
        return;
      }
      applyDone = true;
      var kind = (data.failed && data.failed.length) ? "warning" : "success";
      var html = '<i class="bi bi-check-circle me-1"></i>' + PR.esc(data.summary || "Done.");
      var fails = data.failed || [];
      for (var i = 0; i < fails.length; i++) {
        html += '<div class="small mt-1"><i class="bi bi-x-circle me-1"></i>' +
          PR.esc(fails[i].name) + ": " + PR.esc(fails[i].error) + "</div>";
      }
      showApplyResult(kind, html);
      poll();
    } catch (e) {
      showApplyResult("danger", "The server could not be reached. If the corrections applied anyway, the list above will show it in a moment; otherwise try again.");
    } finally {
      applyBtn.disabled = false;
    }
  });

  function enterActive() {
    setup.classList.add("d-none");
    active.classList.remove("d-none");
    poll();
    pollTimer = setInterval(poll, 1500);
  }

  function leaveActive() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    active.classList.add("d-none");
    setup.classList.remove("d-none");
    startBtn.disabled = !sel.value;
    applyDone = false;
    discrepant = 0;
    applyCard.classList.add("d-none");
    applyResult.classList.add("d-none");
    applyResult.innerHTML = "";
    loadLocations();
  }

  function render(s) {
    document.getElementById("auditLocation").textContent = s.location || "";
    var counts = s.counts || {};
    document.getElementById("cExpected").textContent = counts.expected || 0;
    document.getElementById("cSeen").textContent = counts.seen || 0;
    document.getElementById("cMissing").textContent = counts.missing || 0;
    document.getElementById("cUnexpected").textContent = counts.unexpected || 0;

    var expHtml = "";
    var expected = s.expected || [];
    for (var i = 0; i < expected.length; i++) {
      var e = expected[i];
      var cls = e.seen ? "list-group-item-success" : "list-group-item-warning";
      var icon = e.seen
        ? '<i class="bi bi-check-circle-fill text-success me-2"></i>'
        : '<i class="bi bi-question-circle text-warning me-2"></i>';
      var badge = e.seen
        ? '<span class="badge bg-success">scanned ' + e.scanned_count + '</span>'
        : '<span class="badge bg-warning text-dark">not seen</span>';
      expHtml += '<li class="list-group-item d-flex justify-content-between align-items-center ' + cls + '">' +
        '<span>' + icon + PR.esc(e.name) + '</span>' + badge + '</li>';
    }
    document.getElementById("expectedList").innerHTML = expHtml ||
      '<li class="list-group-item text-secondary">No stock recorded here.</li>';

    var unexpHtml = "";
    var unexpScans = (s.scanned || []).filter(function (x) { return !x.expected; });
    for (var j = 0; j < unexpScans.length; j++) {
      var u = unexpScans[j];
      unexpHtml += '<li class="list-group-item d-flex justify-content-between align-items-center list-group-item-info">' +
        '<span><i class="bi bi-exclamation-triangle me-2"></i>' + PR.esc(u.name) + '</span>' +
        '<span class="badge bg-info text-dark">x' + u.count + '</span></li>';
    }
    document.getElementById("unexpectedList").innerHTML = unexpHtml;
    document.getElementById("unexpectedEmpty").classList.toggle("d-none", unexpScans.length > 0);

    // Offer Apply corrections only when a counted item's amount differs from
    // Grocy's. Items never scanned do not count as discrepancies here: the
    // audit cannot tell "gone" from "not scanned yet", so they are never
    // touched. After a run the card stays to show the result.
    discrepant = 0;
    for (var k = 0; k < expected.length; k++) {
      var x = expected[k];
      if (x.seen && x.amount != null && Number(x.amount) !== x.scanned_count) {
        discrepant++;
      }
    }
    applyCard.classList.toggle("d-none", discrepant === 0 && !applyDone);
    applyBtn.classList.toggle("d-none", discrepant === 0);
  }

  async function poll() {
    try {
      var r = await fetch("../audit/status", { headers: { "Accept": "application/json" } });
      var s = await r.json();
      if (!s.active) { leaveActive(); return; }
      render(s);
    } catch (e) {
      // Keep the last snapshot on a network hiccup.
    }
  }

  // If an audit is already running (started elsewhere or before a reload), jump
  // straight into the live view.
  (async function init() {
    await loadLocations();
    try {
      var r = await fetch("../audit/status", { headers: { "Accept": "application/json" } });
      var s = await r.json();
      if (s && s.active) { enterActive(); }
    } catch (e) {}
  })();
})();
