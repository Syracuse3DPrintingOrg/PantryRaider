// Settings > Bandit Remotes: fills in the Bandit Cub install tag.

(function () {
  var el = document.getElementById("cub-install-tag");
  if (!el) return;
  fetch("cub/install-tag", {credentials: "same-origin"})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      if (d && d.install_tag) { el.textContent = d.install_tag; return; }
      el.textContent = (d && d.source === "server")
        ? "not known yet (it arrives with the next sync from your main server)"
        : "not available";
    })
    .catch(function () { el.textContent = "not available"; });
})();
