// Demo theme picker (templates/_demo_theme_picker.html). The tab opens a
// short list of themes; picking one stores it in the pr_demo_theme cookie and
// reloads, and the server renders this browser's pages in that theme. Nothing
// is saved on the demo itself.
(function () {
  var root = document.getElementById('prDemoTheme');
  if (!root) return;
  var tab = document.getElementById('prDemoThemeTab');
  var panel = document.getElementById('prDemoThemePanel');
  var select = document.getElementById('prDemoThemeSelect');

  function setOpen(open) {
    panel.hidden = !open;
    tab.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) select.focus();
  }

  tab.addEventListener('click', function () { setOpen(panel.hidden); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !panel.hidden) { setOpen(false); tab.focus(); }
  });
  document.addEventListener('click', function (e) {
    if (!panel.hidden && !root.contains(e.target)) setOpen(false);
  });

  select.addEventListener('change', function () {
    var secure = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = 'pr_demo_theme=' + encodeURIComponent(select.value) +
      '; Path=/; Max-Age=2592000; SameSite=Lax' + secure;
    location.reload();
  });
})();
