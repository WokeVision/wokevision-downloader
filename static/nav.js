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
      '<a class="wvNavLink' + (active("/clipping") ? " active" : "") + '" href="/clipping">Clipping</a>' +
      '<a class="wvNavLink' + (active("/schedule") ? " active" : "") + '" href="/schedule">Schedule</a>' +
      '<a class="wvNavLink' + (active("/accounts") ? " active" : "") + '" href="/accounts">Accounts</a>' +
      '<a class="wvNavLink' + (active("/dashboards") ? " active" : "") + '" href="/dashboards">Analytics Hub</a>' +
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

// "Needs attention" strip: failed scheduled posts, broken/expiring logins.
(function () {
  if (location.pathname === "/" || location.pathname.indexOf("/login") === 0) return;
  fetch("/api/attention").then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (!d || !d.items || !d.items.length) return;
    var bar = document.createElement("div");
    bar.className = "wvAttn";
    var first = d.items[0];
    bar.innerHTML = '<a href="' + first.href + '"><b>' + d.items.length + ' thing' + (d.items.length > 1 ? "s" : "") + ' need attention</b> &mdash; ' +
      String(first.text).replace(/</g, "&lt;") + '</a>';
    var nav = document.querySelector(".wvNav");
    if (nav && nav.parentNode) nav.parentNode.insertBefore(bar, nav.nextSibling);
  }).catch(function () {});
})();
