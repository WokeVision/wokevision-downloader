// Injects the shared top menu bar. Logo = homepage (public). Video Editor and
// Dashboards sit behind the passkey -- the server redirects to /login (and
// back again afterwards) if there's no session, so the links are always shown.
(function () {
  try {
    var ml = document.createElement("link"); ml.rel = "manifest"; ml.href = "/manifest.webmanifest"; document.head.appendChild(ml);
    var tc = document.createElement("meta"); tc.name = "theme-color"; tc.content = "#0b0d12"; document.head.appendChild(tc);
    var ai = document.createElement("link"); ai.rel = "apple-touch-icon"; ai.href = "/static/assets/icon-192.png"; document.head.appendChild(ai);
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(function () {});
  } catch (e) {}
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
      '<a class="wvNavLink' + (active("/requests") ? " active" : "") + '" href="/requests">Requests</a>' +
      '<a class="wvNavLink' + (active("/bio") ? " active" : "") + '" href="/bio">Bio link</a>' +
      '<a class="wvNavLink' + (active("/sources") ? " active" : "") + '" href="/sources">Sources</a>' +
      '<a class="wvNavLink' + (active("/ideas") ? " active" : "") + '" href="/ideas">Ideas</a>' +
      '<a class="wvNavLink' + (active("/campaigns") ? " active" : "") + '" href="/campaigns">Campaigns</a>' +
      '<a class="wvNavLink' + (active("/schedule") ? " active" : "") + '" href="/schedule">Schedule</a>' +
      '<a class="wvNavLink' + (active("/accounts") ? " active" : "") + '" href="/accounts">Accounts</a>' +
      '<a class="wvNavLink' + (active("/dashboards") ? " active" : "") + '" href="/dashboards">Analytics Hub</a>' +
      '<a class="wvNavLink' + (active("/settings") ? " active" : "") + '" href="/settings">Settings</a>' +
      '<span class="wvNavSpacer"></span>' +
      '<a class="wvNavAuth" id="wvNavAuth" href="/login?next=' + encodeURIComponent(path) + '" style="visibility:hidden">Sign in</a>' +
    '</div>';
  document.body.insertBefore(nav, document.body.firstChild);
  // Numbered bubble on "Requests" for requests waiting for review.
  function badge() {
    fetch("/api/requests/count").then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      var a = document.querySelector('.wvNavLink[href="/requests"]'); if (!a || !d) return;
      var b = a.querySelector(".wvBadge");
      if (!d.pending) { if (b) b.remove(); return; }
      if (!b) { b = document.createElement("span"); b.className = "wvBadge"; a.appendChild(b); }
      b.textContent = d.pending > 99 ? "99+" : d.pending;
    }).catch(function () {});
  }
  if (location.pathname.indexOf("/login") !== 0) { badge(); setInterval(badge, 60000); }
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
    var dis = []; try { dis = JSON.parse(localStorage.getItem("wvDismissed") || "[]"); } catch (e) {}
    var items = d.items.filter(function (i) { return dis.indexOf(i.text) < 0; });
    if (!items.length) return;
    var bar = document.createElement("div");
    bar.className = "wvAttn";
    function esc(t) { return String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;"); }
    bar.innerHTML = '<a href="#" class="wvAttnHead"><b>' + items.length + ' thing' + (items.length > 1 ? "s" : "") + ' need attention</b> &mdash; tap to see all</a>' +
      '<div class="wvAttnList" style="display:none">' + items.map(function (i, n) {
        return '<div class="wvAttnRow"><a href="' + i.href + '">' + esc(i.text) + '</a><button type="button" data-n="' + n + '" title="Dismiss">&times;</button></div>';
      }).join("") + '</div>';
    bar.querySelector(".wvAttnHead").onclick = function (e) { e.preventDefault(); var l = bar.querySelector(".wvAttnList"); l.style.display = l.style.display === "none" ? "block" : "none"; };
    bar.querySelectorAll(".wvAttnRow button").forEach(function (b) {
      b.onclick = function () {
        dis.push(items[+b.dataset.n].text); try { localStorage.setItem("wvDismissed", JSON.stringify(dis)); } catch (e) {}
        b.parentNode.remove(); if (!bar.querySelector(".wvAttnRow")) bar.remove();
      };
    });
    var nav = document.querySelector(".wvNav");
    if (nav && nav.parentNode) nav.parentNode.insertBefore(bar, nav.nextSibling);
  }).catch(function () {});
})();

// Job tray: running edits and clip jobs from any page, with a link back to each.
(function () {
  var p = location.pathname;
  if (p === "/" || p.indexOf("/login") === 0 || p === "/links" || p.indexOf("/links/") === 0 || p.indexOf("/c/") === 0 || p === "/terms" || p === "/privacy") return;
  var box = document.createElement("div");
  box.id = "wvTray"; box.style.display = "none";
  document.body.appendChild(box);
  var open = false, timer = null, seenRunning = {};
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function eta(s) { if (!s || s < 1) return ""; return s < 90 ? " · ~" + Math.round(s) + "s left" : " · ~" + Math.round(s / 60) + " min left"; }
  function draw(items) {
    var run = items.filter(function (i) { return i.status === "running"; });
    run.forEach(function (i) { seenRunning[i.id] = 1; });
    // Only show finished jobs that were running while this page was open (not old leftovers).
    var fin = items.filter(function (i) { return i.status !== "running" && seenRunning[i.id]; });
    var show = run.concat(fin);
    if (!show.length) { box.style.display = "none"; return; }
    box.style.display = "block";
    var head = run.length ? run.length + " running" : (fin.some(function (i) { return i.status === "error"; }) ? "Job problem" : "Done");
    var rows = show.map(function (i) {
      var pct = i.status === "running" && i.progress != null ? Math.round(i.progress * 100) + "%" : "";
      var state = i.status === "running" ? esc(i.label) + (pct ? " · " + pct : "") + eta(i.eta) : (i.status === "error" ? "Failed — " + esc(i.error || "see the page") : "Finished ✓");
      var bar = i.status === "running" && i.progress != null ? '<div class="wvTb"><i style="width:' + Math.round(i.progress * 100) + '%"></i></div>' : "";
      return '<a class="wvTr" href="' + esc(i.url) + '"><b>' + esc((i.kind === "clips" ? "Clips · " : "") + i.title) + '</b><span>' + state + '</span>' + bar + '</a>';
    }).join("");
    box.innerHTML = '<div id="wvTl" style="display:' + (open ? "block" : "none") + '">' + rows + '</div>' +
      '<button id="wvTt">' + (run.length ? '<i class="wvTd"></i>' : (fin.some(function (i) { return i.status === "error"; }) ? "⚠️ " : "✓ ")) + head + '</button>';
    document.getElementById("wvTt").onclick = function () { open = !open; document.getElementById("wvTl").style.display = open ? "block" : "none"; };
  }
  function poll() {
    fetch("/api/jobs/active").then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) { timer = setTimeout(poll, 30000); return; }
      draw(d.items || []);
      timer = setTimeout(poll, (d.items || []).some(function (i) { return i.status === "running"; }) ? 3000 : 15000);
    }).catch(function () { timer = setTimeout(poll, 30000); });
  }
  poll();
})();
