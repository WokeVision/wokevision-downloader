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
  // Five top-level sections; pages inside a section show as a second row of tabs.
  var GROUPS = [
    { id: "create", label: "Create", pages: [["/editor", "Editor"], ["/clipping", "Clipper"], ["/ideas", "Ideas"]] },
    { id: "schedule", label: "Schedule", pages: [["/schedule", "Schedule"]] },
    { id: "inbox", label: "Inbox", pages: [["/requests", "Requests"], ["/comments", "Comments"]] },
    { id: "analytics", label: "Analytics", pages: [["/dashboards", "Analytics"], ["/campaigns", "Campaigns"]] },
    { id: "manage", label: "Manage", pages: [["/settings", "Settings"], ["/accounts", "Accounts"], ["/bio", "Bio link"], ["/sources", "Sources"]] }
  ];
  var curGroup = null;
  GROUPS.forEach(function (g) { g.pages.forEach(function (p) { if (active(p[0])) curGroup = g; }); });
  var groupsHtml = GROUPS.map(function (g) {
    return '<a class="wvNavLink' + (g === curGroup ? " active" : "") + '" data-g="' + g.id + '" href="' + g.pages[0][0] + '">' + g.label + '</a>';
  }).join("");
  var nav = document.createElement("nav");
  nav.className = "wvNav";
  nav.setAttribute("role", "navigation"); nav.setAttribute("aria-label", "Main");
  nav.innerHTML =
    '<div class="wvNavInner">' +
      '<a class="wvNavLogo' + (active("/") ? " active" : "") + '" href="/" aria-label="WokeVision home">' +
        '<img src="/static/assets/logo-mark.png" alt="WokeVision" /></a>' +
      groupsHtml +
      '<span class="wvNavSpacer"></span>' +
      '<a class="wvNavAuth" id="wvNavAuth" href="/login?next=' + encodeURIComponent(path) + '" style="visibility:hidden">Sign in</a>' +
    '</div>';
  document.body.insertBefore(nav, document.body.firstChild);
  if (curGroup && curGroup.pages.length > 1) {
    var sub = document.createElement("div");
    sub.className = "wvSub"; sub.setAttribute("role", "navigation"); sub.setAttribute("aria-label", curGroup.label);
    sub.innerHTML = '<div class="wvSubInner">' + curGroup.pages.map(function (p) {
      return '<a class="wvSubLink' + (active(p[0]) ? " active" : "") + '" href="' + p[0] + '">' + p[1] + '</a>';
    }).join("") + '</div>';
    nav.parentNode.insertBefore(sub, nav.nextSibling);
  }
  document.querySelectorAll(".wvNav a.active, .wvSub a.active").forEach(function (a) { a.setAttribute("aria-current", "page"); });
  // Numbered bubble on "Requests" for requests waiting for review.
  function badge() {
    fetch("/api/requests/count").then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      document.querySelectorAll('.wvNavLink[data-g="inbox"], .wvSubLink[href="/requests"]').forEach(function (a) {
        var b = a.querySelector(".wvBadge");
        if (!d.pending) { if (b) b.remove(); return; }
        if (!b) { b = document.createElement("span"); b.className = "wvBadge"; a.appendChild(b); }
        b.textContent = d.pending > 99 ? "99+" : d.pending;
      });
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
  box.id = "wvTray"; box.style.display = "none"; box.setAttribute("aria-live", "polite");
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
    document.getElementById("wvTt").setAttribute("aria-expanded", open ? "true" : "false");
    document.getElementById("wvTt").onclick = function () { open = !open; this.setAttribute("aria-expanded", open ? "true" : "false"); document.getElementById("wvTl").style.display = open ? "block" : "none"; };
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

// Keyboard shortcuts: press ? for the list; "g" then a letter jumps to a page.
(function () {
  if (location.pathname.indexOf("/login") === 0 || location.pathname === "/" || location.pathname.indexOf("/links") === 0) return;
  var MAP = { e: ["/editor", "Editor"], c: ["/clipping", "Clipper"], r: ["/requests", "Requests"], b: ["/bio", "Bio link"], s: ["/schedule", "Schedule"], a: ["/dashboards", "Analytics"], i: ["/ideas", "Ideas"], p: ["/campaigns", "Campaigns"], o: ["/sources", "Sources"], m: ["/comments", "Comments"], t: ["/settings", "Settings"] };
  var armed = 0, ov = null;
  function typing(e) { var t = e.target, n = t && t.tagName; return n === "INPUT" || n === "TEXTAREA" || n === "SELECT" || (t && t.isContentEditable); }
  function help() {
    if (ov) { ov.remove(); ov = null; return; }
    ov = document.createElement("div");
    ov.setAttribute("role", "dialog"); ov.setAttribute("aria-label", "Keyboard shortcuts");
    ov.style.cssText = "position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:18px";
    ov.innerHTML = '<div style="background:#16161a;border:1px solid rgba(255,255,255,.14);border-radius:18px;padding:20px 24px;max-width:420px;width:100%;color:#F5F5F7;font:14px/1.9 -apple-system,BlinkMacSystemFont,sans-serif"><b style="font-size:16px">Keyboard shortcuts</b><div style="opacity:.6;font-size:12.5px;margin-bottom:8px">Press <kbd>g</kbd> then a letter. <kbd>?</kbd> toggles this, <kbd>Esc</kbd> closes it.</div>' +
      Object.keys(MAP).map(function (k) { return '<div><kbd style="background:rgba(255,255,255,.12);border-radius:6px;padding:1px 8px;margin-right:8px">g ' + k + '</kbd>' + MAP[k][1] + '</div>'; }).join("") + '</div>';
    ov.addEventListener("click", function (e) { if (e.target === ov) help(); });
    document.body.appendChild(ov);
  }
  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape" && ov) { help(); return; }
    if (typing(e)) return;
    if (e.key === "?") { e.preventDefault(); help(); return; }
    if (armed && Date.now() - armed < 1500 && MAP[e.key.toLowerCase()]) { e.preventDefault(); location.href = MAP[e.key.toLowerCase()][0]; return; }
    armed = e.key === "g" ? Date.now() : 0;
  });
})();
