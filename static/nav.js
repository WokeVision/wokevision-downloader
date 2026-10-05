// Injects the shared top menu bar. Logo = homepage (public). Video Editor and
// Dashboards sit behind the passkey -- the server redirects to /login (and
// back again afterwards) if there's no session, so the links are always shown.
(function () {
  var path = location.pathname.replace(/\/+$/, "") || "/";
  function active(p) { return p === "/" ? path === "/" : path === p || path.indexOf(p + "/") === 0; }
  var nav = document.createElement("nav");
  nav.className = "wvNav";
  nav.innerHTML =
    '<div class="wvNavInner">' +
      '<a class="wvNavLogo' + (active("/") ? " active" : "") + '" href="/" aria-label="WokeVision home">' +
        '<img src="/static/assets/logo-mark.png" alt="WokeVision" /></a>' +
      '<a class="wvNavLink' + (active("/editor") ? " active" : "") + '" href="/editor">Video Editor</a>' +
      '<a class="wvNavLink' + (active("/dashboards") ? " active" : "") + '" href="/dashboards">Dashboard</a>' +
      '<span class="wvNavSpacer"></span>' +
      '<a class="wvNavAuth" id="wvNavAuth" href="/login?next=' + encodeURIComponent(path) + '" style="visibility:hidden">Sign in</a>' +
    '</div>';
  document.body.insertBefore(nav, document.body.firstChild);
  fetch("/auth/status").then(function (r) { return r.json(); }).then(function (s) {
    var a = document.getElementById("wvNavAuth");
    if (s.authed) {
      a.textContent = "Sign out"; a.removeAttribute("href");
      a.onclick = function () { fetch("/auth/logout", { method: "POST" }).then(function () { location.href = "/"; }); };
    }
    a.style.visibility = "visible";
  }).catch(function () { document.getElementById("wvNavAuth").style.visibility = "visible"; });
})();
