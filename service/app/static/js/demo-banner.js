// Demo-mode banner (templates/_demo_banner.html): while it shows, the body
// carries demo-banner-on so the fixed navbar and the page move down to make
// room, and dismissing the banner takes the room back.
(function () {
  document.body.classList.add('demo-banner-on');
  var banner = document.getElementById('demoBanner');
  if (banner) {
    banner.addEventListener('close.bs.alert', function () {
      document.body.classList.remove('demo-banner-on');
    });
  }
})();
