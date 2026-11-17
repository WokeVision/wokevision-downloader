/* Demo mode: the real app pages run unchanged, but every request is answered
   here with made-up data. Nothing in this file ever talks to the real server. */
(function () {
  if (window.__DEMO) return;
  var cfg = {};
  try { cfg = JSON.parse(sessionStorage.getItem("wvDemoCfg") || "{}") || {}; } catch (e) {}
  var Q = new URLSearchParams(location.search);
  if (Q.get("name")) cfg.name = Q.get("name").replace(/[^\w .&'-]/g, "").slice(0, 28);
  if (/^#?[0-9a-f]{6}$/i.test(Q.get("color") || "")) cfg.color = "#" + Q.get("color").replace("#", "");
  cfg.name = cfg.name || "Your Page"; cfg.color = cfg.color || "#e1262d";
  try { sessionStorage.setItem("wvDemoCfg", JSON.stringify(cfg)); } catch (e) {}
  window.__DEMO = cfg;

  /* ---------- helpers ---------- */
  var NOW = Date.now(), H = 3600 * 1000, D = 24 * H;
  var seed = 7; function rnd() { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }
  function ri(a, b) { return Math.floor(a + rnd() * (b - a + 1)); }
  function pick(a) { return a[ri(0, a.length - 1)]; }
  function iso(ms) { return new Date(ms).toISOString(); }
  function uid() { return Math.random().toString(16).slice(2, 14); }
  var HANDLE = "@" + cfg.name.toLowerCase().replace(/[^a-z0-9]/g, "");
  var CLIP = "/static/assets/demo/clip.mp4";
  var EMOJI = ["😂", "💀", "😳", "🤯", "👀", "🔥", "😬", "🍺", "⚽", "🚌", "🥶", "🤦"];
  var GRAD = [["#ff512f", "#dd2476"], ["#1e3c72", "#2a5298"], ["#134e5e", "#71b280"], ["#614385", "#516395"], ["#f7971e", "#ffd200"], ["#232526", "#414345"], ["#c31432", "#240b36"], ["#0f2027", "#2c5364"]];
  function thumb(i, label) {
    var g = GRAD[i % GRAD.length], e = EMOJI[i % EMOJI.length];
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="' + g[0] + '"/><stop offset="1" stop-color="' + g[1] + '"/></linearGradient></defs><rect width="360" height="640" fill="url(#g)"/><text x="180" y="360" font-size="120" text-anchor="middle">' + e + '</text></svg>';
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  }
  var TITLES = ["Bus driver absolutely not having it", "Pub quiz argument gets out of hand", "Lads' holiday sunburn disaster", "Self-checkout meltdown at the Co-op", "Five-a-side keeper does the unthinkable", "Granny vs the garden gnome thief", "Traffic warden meets his match", "Chippy queue etiquette lesson", "Rain delay at the cricket, chaos", "Taxi driver's hot take on the bypass", "Mate's first time on a paddleboard", "Supermarket trolley rage, part 2", "Man cooks full roast in a kettle", "Referee hilariously loses his flags", "Roundabout etiquette goes wrong", "Dog steals the Sunday roast", "Student discovers the washing machine", "Train announcer has had enough", "Fry-up challenge, no survivors", "Karaoke night goes viral for the wrong reasons", "Postie meets the world's angriest goose", "Charity shop find of the century", "Dad tries the self-assembly wardrobe", "Seagull steals the whole chip tray", "Parking space dispute, round 3", "Local hero saves the pub quiz", "Wedding speech goes off script", "Lift music hits too hard", "Barber gives the wrong haircut, confidently", "Weather forecaster gets drenched live"];
  var CAPS = ["Wait for the ending 😳😳", "\"He really thought that would work\" 💀", "Nobody was ready for this 🤯", "The way he said it 😭", "Not the seagull again 👀", "Mate has zero shame 😬", "This is why we love the UK 🇬🇧", "Imagine being there for this 😳", "How is this even legal? 🤨", "She had NO idea 💀💀"];
  var PLATS = [["instagram", "Instagram", "#E1306C"], ["threads", "Threads", "#A78BFA"], ["youtube", "YouTube", "#FF3B30"], ["tiktok", "TikTok", "#25F4EE"], ["x", "X", "#F5F5F7"], ["facebook", "Facebook", "#4C8BF5"]];
  var LAB = {}; PLATS.forEach(function (p) { LAB[p[0]] = p[1]; });

  /* ---------- state (kept in sessionStorage so it survives page changes) ---------- */
  var S;
  try { S = JSON.parse(sessionStorage.getItem("wvDemoState") || "null"); } catch (e) {}
  function save() { try { sessionStorage.setItem("wvDemoState", JSON.stringify(S)); } catch (e) {} }
  if (!S) {
    S = { history: [], schedule: [], ideas: [], campaigns: [], watermarks: [], jobs: {}, settings: {}, goals: { views_month: 2000000, posts_week: 14 }, requests: [], comments: {}, replies: {}, explore: [], bio: null, accounts: {}, sources: [] };
    // history
    for (var i = 0; i < 24; i++) {
      var id = uid() + uid().slice(0, 4), t = NOW - (i * 0.8 + rnd()) * D, title = TITLES[i % TITLES.length];
      var pr = {};
      PLATS.forEach(function (p) { if (rnd() < 0.9) pr[p[0]] = { at: iso(t + 600000), ok: rnd() < 0.96, status: "done" }; });
      S.history.push({ id: id, created_at: iso(t), title: title, on_screen_caption: CAPS[i % CAPS.length], posting_caption: CAPS[i % CAPS.length] + "\n\n#funny #uk #banter #viral #memes", video_available: i < 18, publish_results: pr, hue: i });
    }
    // schedule: upcoming + posted
    S.history.slice(0, 5).forEach(function (h, k) {
      var base = NOW + (k + 1) * 9 * H + 2 * H;
      PLATS.forEach(function (p, j) { S.schedule.push({ id: uid(), history_id: h.id, platform: p[0], label: p[1], run_at: iso(base + j * 20 * 60000), status: "scheduled", error: "", post_url: "", title: h.title, on_screen_caption: h.on_screen_caption, video_url: CLIP }); });
    });
    S.history.slice(5, 9).forEach(function (h, k) {
      PLATS.slice(0, 4).forEach(function (p) { S.schedule.push({ id: uid(), history_id: h.id, platform: p[0], label: p[1], run_at: iso(NOW - (k + 1) * 14 * H), status: "done", error: "", post_url: "#", title: h.title, on_screen_caption: h.on_screen_caption, video_url: CLIP }); });
    });
    S.schedule.push({ id: uid(), history_id: S.history[2].id, platform: "tiktok", label: "TikTok", run_at: iso(NOW + 30 * H), status: "error", error: "TikTok needs you to re-connect the account (demo example of a failed post)", post_url: "", title: S.history[2].title, on_screen_caption: S.history[2].on_screen_caption, video_url: CLIP });
    S.campaigns = [{ id: "c1" + uid().slice(0, 8), name: "Summer brand deal (sample)", sponsor: "Example Sponsor Ltd", brief: "Mention the product naturally, show it in the first 3 seconds, include the discount code in the caption.", hashtags: "#ad #sponsored", wm_token: "", wm_pos: "right", posts: 6, share_token: "demo" }];
    S.ideas = [{ id: uid(), url: "https://example.com/video/parking-fails", note: "Compilation: worst parking jobs of the year", created_at: iso(NOW - 3 * D) }, { id: uid(), url: "https://example.com/video/bus-chat", note: "Series: 'Overheard on the bus'", created_at: iso(NOW - 2 * D) }, { id: uid(), url: "https://example.com/video/pub-quiz", note: "Reaction to the viral pub quiz clip", created_at: iso(NOW - D) }];
    S.clipHist = [{ id: "clipdemo1", title: "Full match-day podcast, episode 41", status: "done", clip_count: 6, created_at: iso(NOW - 2 * D) }, { id: "clipdemo2", title: "Two-hour pub quiz livestream", status: "done", clip_count: 6, created_at: iso(NOW - 5 * D) }];
    S.clipJobs = {}; S.watch = [{ id: "w1", url: "https://example.com/channel/banter-daily", title: "Latest episode (sample)" }];
    S.settings = { vocab: "mate, lads, innit, chippy", banned: "", voice_notes: "Dry British humour. Short punchy lines. No swearing in the first line.", mask_profanity: false, mask_words: "", caption_style: "classic", slots: "08:00,12:00,18:00,20:00", smart_crop: true, yt_visibility: "public", ig_collaborators: "" };
    S.requests = [{ id: uid(), kind: "consult", status: "pending", created_at: iso(NOW - 5 * H), name: "Sample Creator", email: "creator@example.com", data: { when: "Thu 14:00", topic: "Setting up a branded editor for my page", invited: false, minutes: 20, when_iso: iso(NOW + 2 * D), meeting_link: "" }, run_at: iso(NOW + 2 * D), history_id: null, owner_note: "", file_urls: [] }, { id: uid(), kind: "reel", status: "pending", created_at: iso(NOW - 26 * H), name: "Example Brand", email: "hello@example.com", data: { note: "Looking for a 30s edit of our product launch" }, run_at: null, history_id: null, owner_note: "", file_urls: [] }];
    S.sources = S.history.slice(0, 14).map(function (h, k) {
      return { id: h.id, created_at: h.created_at, title: h.title, source_url: "https://example.com/clip/" + (k + 1), creator: "@sample_creator" + (k + 1), method: k % 3 ? "yt-dlp" : "upload", credited: k % 4 ? "@sample_creator" + (k + 1) : "", posted_to: [{ platform: "instagram", url: "#" }, { platform: "youtube", url: "#" }], rights: { status: ["unreviewed", "licensed", "permission", "unreviewed"][k % 4], note: "", evidence: "", updated_at: null, explicit: false } };
    });
    var kinds = ["news", "reddit", "youtube", "odysee", "dailymotion"];
    S.explore = TITLES.slice(0, 22).map(function (t, k) { return { id: uid(), source: kinds[k % 5], url: "https://example.com/v/" + k, title: t, thumb: thumb(k), preview_url: null, embed: k % 2 ? "youtube" : "", duration: ri(12, 90), stats: { hot: pick(["", "🔥 hot", "rising"]), story: "", views: ri(20000, 4000000), channel: pick(["Banter Daily", "UK Clips", "Pub Talk TV", "The Daily Roll"]), outlets: ri(1, 9) }, score: ri(55, 98), topic: pick(["sport", "funny", "news", "viral", "local"]), status: "new", job_id: null, created_at: iso(NOW - ri(1, 30) * H) }; }).sort(function (a, b) { return b.score - a.score; });
    save();
  }
  window.__demoState = S;

  /* ---------- fake analytics ---------- */
  function series(days, start, growth, vol) {
    var out = [], f = start;
    for (var d = days; d >= 0; d--) { f += growth + (rnd() - 0.5) * vol; out.push({ day: new Date(NOW - d * D).toISOString().slice(0, 10), followers: Math.round(f), views: ri(30000, 160000) }); }
    return out;
  }
  var FOL = { instagram: 148200, threads: 21400, youtube: 9650, tiktok: 61200, x: 12850, facebook: 33400 };
  function insights(pid) {
    var n = pid === "instagram" ? 80 : 40, posts = [];
    for (var i = 0; i < n; i++) {
      var v = Math.round(Math.exp(ri(80, 140) / 10) * (0.4 + rnd()));
      posts.push({ id: uid(), title: TITLES[i % TITLES.length], caption: TITLES[i % TITLES.length], url: "#", thumb: thumb(i), ts: iso(NOW - (i * 0.45 + rnd() * 0.3) * D), views: v, likes: Math.round(v * (0.03 + rnd() * 0.06)), comments: Math.round(v * (0.002 + rnd() * 0.004)), shares: Math.round(v * (0.004 + rnd() * 0.01)), type: "VIDEO" });
    }
    var tot = posts.reduce(function (a, p) { a.views += p.views; a.likes += p.likes; a.comments += p.comments; return a; }, { views: 0, likes: 0, comments: 0 });
    return { platform: pid, label: LAB[pid], state: "ok", note: null, account: HANDLE, totals: { followers: FOL[pid], posts: n, views: tot.views, likes: tot.likes, comments: tot.comments, shares: pid === "instagram" ? null : Math.round(tot.views * 0.006) }, posts: posts };
  }
  var INS = {}; PLATS.forEach(function (p) { INS[p[0]] = insights(p[0]); });
  function summary(days) {
    var heat = []; for (var d = 0; d < 7; d++) for (var h = 6; h < 24; h++) if (rnd() < 0.65) heat.push({ d: d, h: h, avg: ri(8000, 90000) * (h >= 17 && h <= 21 ? 2.2 : 1), n: ri(1, 6) });
    var cur = { posts: Math.round(days * 1.4), views: days * 52000, likes: days * 3100, comments: days * 190, avg_views: 37000 };
    return { posts: 220, tagged: 220, heatmap: heat, best_hours: [{ hour: 18, avg: 91000, n: 14 }, { hour: 20, avg: 84000, n: 11 }, { hour: 12, avg: 61000, n: 9 }, { hour: 8, avg: 44000, n: 6 }],
      topics: [["sport", 92000, 31], ["funny", 71000, 58], ["local", 48000, 24], ["news", 40000, 19], ["viral", 66000, 40], ["food", 52000, 17]].map(function (x) { return { name: x[0], avg: x[1], n: x[2] }; }),
      hooks: [["question", 79000, 28], ["statement", 58000, 64], ["quote", 83000, 22], ["wait for it", 96000, 18], ["POV", 67000, 12]].map(function (x) { return { name: x[0], avg: x[1], n: x[2] }; }),
      current: cur, previous: { posts: Math.round(days * 1.2), views: days * 43000, likes: days * 2700, comments: days * 160, avg_views: 33000 }, days: days, updated_at: iso(NOW - 25 * 60000),
      top: INS.instagram.posts.slice().sort(function (a, b) { return b.views - a.views; }).slice(0, 5).map(function (p) { return { title: p.title, url: "#", platform: "Instagram", views: p.views }; }) };
  }

  /* ---------- editor job simulation ---------- */
  var STAGES = [["downloading", "Fetching your video", 0.08], ["transcribing", "Listening to the audio", 0.35], ["captioning", "Writing captions", 0.6], ["rendering", "Framing & burning in captions", 0.9]];
  function newJob(label) {
    var id = uid() + uid().slice(0, 4), pick1 = CAPS[ri(0, CAPS.length - 1)];
    var title = TITLES[ri(0, TITLES.length - 1)];
    var cap = pick1.replace(/\s*\S+$/, "").trim() + " 😳😳";
    var core = cap.replace(/[^\w\s"'?!.,]/gu, "").trim() + " 👇";
    S.jobs[id] = { started: Date.now(), title: title, on_screen: cap, core: core, posts: mkPosts(core, title), cues: mkCues(), angle: "" };
    S.history.unshift({ id: id, created_at: iso(Date.now()), title: title, on_screen_caption: cap, posting_caption: core, video_available: true, publish_results: {}, hue: ri(0, 7) });
    save();
    return id;
  }
  function mkCues() {
    var w = "so this is how the whole thing started and honestly nobody saw it coming at all".split(" "), out = [];
    for (var i = 0; i < w.length; i += 3) out.push({ start: i * 0.33, end: i * 0.33 + 0.95, text: w.slice(i, i + 3).join(" ") });
    return out;
  }
  function mkPosts(core, title) {
    var tags = "#funny #uk #banter #viral #memes";
    var yt = core + "\n\nA proper moment of British chaos: here's exactly what happens, who is involved and why everyone is sharing it. Perfect for fans of UK banter, funny videos, caught-on-camera clips and viral moments. Tell us your favourite bit in the comments and subscribe for a new clip every day.\n\nCredit: @sample_creator\n\n#Shorts #funny #uk #viral";
    return { instagram: { caption: core + "\n\nCredit: @sample_creator\n\n" + tags, story: true, collab: "" }, threads: { text: core + " #funny" }, tiktok: { caption: core + " #fyp #funny #uk", privacy_level: "PUBLIC_TO_EVERYONE", story: false }, facebook: { description: core + "\n\n" + tags, story: true }, x: { text: core.slice(0, 220) + " #viral" }, youtube: { title: title.slice(0, 95), caption: core, description: yt, tags: ["funny", "uk banter", "viral", "caught on camera", "shorts"] } };
  }
  function jobResult(id) {
    var j = S.jobs[id]; if (!j) return null;
    return { platform_posts: j.posts, video_url: CLIP, staged_url: CLIP, caption_adj: j.adj || {}, on_screen_caption: j.on_screen, caption: j.core, download_method: "demo", angle: j.angle || "", video_read: "A short, funny British street moment with a surprise at the end.", cues: j.cues, captions_on: true, paid_promo: false, captions_style: "classic", credit: "@sample_creator", campaign_id: j.campaign || "", post: {}, cover_ms: 800, finished_edit: false, warnings: [], watermark: null };
  }
  function jobStatus(id) {
    var j = S.jobs[id];
    if (!j) { var h = S.history.filter(function (x) { return x.id === id; })[0]; if (!h) return [404, { detail: "Unknown job id" }]; j = S.jobs[id] = { started: 0, title: h.title, on_screen: h.on_screen_caption, core: h.posting_caption, posts: mkPosts(h.posting_caption, h.title), cues: mkCues() }; }
    var el = (Date.now() - j.started) / 1000;
    if (j.busy && Date.now() < j.busy) return [200, { status: "running", stage: "captioning", stage_label: "Updating", progress: 0.7, eta_seconds: 2 }];
    if (el < 7) { var p = Math.min(0.95, el / 7); var st = STAGES[Math.min(3, Math.floor(p * 4))]; return [200, { status: "running", stage: st[0], stage_label: st[1], progress: p, eta_seconds: Math.max(1, Math.round(7 - el)) }]; }
    return [200, { status: "done", stage: "done", stage_label: "Done", progress: 1, eta_seconds: 0, result: jobResult(id) }];
  }

  /* ---------- the router ---------- */
  function J(x, st) { return [st || 200, x]; }
  function route(method, url, body) {
    var u = new URL(url, location.origin), p = u.pathname, q = u.searchParams, m;
    if (method === "GET") {
      if (p === "/auth/status") return J({ authed: true });
      if (p === "/api/requests/count") return J({ pending: S.requests.filter(function (r) { return r.status === "pending"; }).length });
      if (p === "/api/attention") return J({ items: [] });
      if (p === "/api/jobs/active") return J({ items: [] });
      if (p === "/api/home/popular") return J({ account: HANDLE, posts: INS.instagram.posts.slice(0, 10).map(function (x) { return { thumb: x.thumb, url: "#", title: x.title, likes: x.likes, comments: x.comments }; }) });
      if (p === "/connections") return J({ connections: PLATS.map(function (x) { return { platform: x[0], label: x[1], available: true, connected: true, ok: true, status_label: "Connected as " + HANDLE }; }) });
      if (p === "/history") return J({ history: S.history.map(function (h) { var sched = {}; S.schedule.forEach(function (s) { if (s.history_id === h.id && s.status === "scheduled") sched[s.platform] = s.run_at; }); return { id: h.id, created_at: h.created_at, title: h.title, on_screen_caption: h.on_screen_caption, video_available: h.video_available, publish_results: h.publish_results, scheduled: sched }; }) });
      if ((m = p.match(/^\/history\/([^/]+)$/))) {
        var h = S.history.filter(function (x) { return x.id === m[1]; })[0]; if (!h) return J({ detail: "Unknown history entry" }, 404);
        var j = S.jobs[h.id] || (S.jobs[h.id] = { started: 0, title: h.title, on_screen: h.on_screen_caption, core: h.posting_caption, posts: mkPosts(h.posting_caption, h.title), cues: mkCues() });
        var r = jobResult(h.id);
        return J({ id: h.id, created_at: h.created_at, title: h.title, on_screen_caption: j.on_screen, posting_caption: j.core, platform_posts: j.posts, video_available: true, source_available: true, video_url: CLIP, publish_results: h.publish_results, meta: { angle: "" }, cues: r.cues, captions_on: true, watermark: null, paid_promo: false, captions_style: "classic", credit: "@sample_creator", campaign_id: "", post: {}, cover_ms: 800, video_read: r.video_read, finished_edit: false, warnings: [], caption_adj: j.adj || {}, staged_url: CLIP });
      }
      if ((m = p.match(/^\/jobs\/([^/]+)$/))) { var js = jobStatus(m[1]); return J(js[1], js[0]); }
      if ((m = p.match(/^\/publish\/([^/]+)$/))) { var pj = S.jobs["pub_" + m[1]]; var el = (Date.now() - ((pj && pj.started) || 0)) / 1000; var res = {}; ((pj && pj.platforms) || []).forEach(function (pl, i) { res[pl] = el > 1.2 + i * 0.7 ? { ok: true, status: "done", story: "posted" } : { status: "pending" }; }); return J({ status: el > 1.2 + ((pj && pj.platforms.length) || 1) * 0.7 ? "done" : "running", results: res }); }
      if (p === "/api/watermarks") return J({ items: S.watermarks });
      if (p === "/api/campaigns") return J({ items: S.campaigns });
      if (p === "/api/settings") return J(S.settings);
      if (p === "/api/ideas") return J({ items: S.ideas });
      if (p === "/api/schedule-health") return J({ timer_configured: true, last_tick_seconds_ago: 24 });
      if (p === "/api/schedule") {
        var items = S.schedule.slice(); if (q.get("history_id")) items = items.filter(function (s) { return s.history_id === q.get("history_id"); });
        if (q.get("past") === "false") items = items.filter(function (s) { return s.status !== "done"; });
        return J({ items: items });
      }
      if (p === "/api/explore/config") { var c = { keywords: ["funny", "caught on camera", "fail", "viral", "uk", "banter", "prank", "reaction"], channels: ["Banter Daily", "UK Clips", "Pub Talk TV"], subreddits: ["PublicFreakout", "funny", "unitedkingdom"], telegram: [], hashtags: [], handles: [], outlets: [], push_min_score: 80 }; return J({ config: c, defaults: c }); }
      if (p === "/api/explore") { var st2 = q.get("status") || "new"; return J({ items: S.explore.filter(function (x) { return x.status === st2; }), last_scan: { at: iso(NOW - 40 * 60000), sources: { radar: { ok: true, found: 38, detail: "sample" }, news: { ok: true, found: 24, seconds: 3 }, dailymotion: { ok: true, found: 12, seconds: 2 }, odysee: { ok: true, found: 9, seconds: 2 }, channels: { ok: true, found: 17, seconds: 4 } } } }); }
      if (p === "/api/requests") return J({ items: S.requests, email_configured: true });
      if (p === "/api/messages") return J({ conversations: ["Sample Fan", "Brand Rep", "Creator Mate", "Page Admin", "Another Fan"].map(function (n, i) { return { id: "c" + i, platform: i % 2 ? "threads" : "instagram", with_id: "u" + i, with_name: n, last_text: pick(["love this one 😂", "can we collab on a video?", "what camera do you use?", "sent you a clip", "are you posting today?"]), updated: iso(NOW - i * 3 * H) }; }), platforms: PLATS.map(function (x, i) { return { platform: x[0], label: x[1], status: i < 2 ? "live" : i < 5 ? "needs_access" : "unsupported", note: i < 2 ? "Messaging connected" : "Switch on messaging access in the platform settings" }; }) });
      if ((m = p.match(/^\/api\/messages\/([^/]+)\/([^/]+)$/))) return J({ messages: [{ mine: false, text: "Hey! Big fan of the page 😂" }, { mine: true, text: "Cheers mate, appreciate it!" }, { mine: false, text: "Any chance of a shoutout for our channel?" }].concat(S.replies[m[2]] || []) });
      if (p === "/api/goals") { var v = 1430000; return J({ goals: S.goals, views_month: v, posts_week: 9, projected_views: Math.round(v * 1.35), views_prev_month: 1710000 }); }
      if (p === "/api/analytics/summary") return J(summary(+q.get("days") || 30));
      if (p === "/api/insights-history") { var hh = {}; PLATS.forEach(function (x) { hh[x[0]] = series(30, FOL[x[0]] - 3000, 90, 120); }); return J({ history: hh }); }
      if (p === "/api/content-groups") return J({ groups: S.history.slice(0, 14).map(function (h, i) { var ids = {}; PLATS.forEach(function (x) { var po = INS[x[0]].posts[i]; if (po) ids[x[0]] = po.id; }); return { id: h.id, title: h.title, created_at: h.created_at, ids: ids }; }) });
      if ((m = p.match(/^\/api\/insights\/([a-z]+)\/views$/))) return J({ views: 3650000, clamped: false });
      if ((m = p.match(/^\/api\/insights\/([a-z]+)$/)) && INS[m[1]]) return J(INS[m[1]]);
      if (p === "/api/accounts") return J({ accounts: PLATS.map(function (x) { var a = S.accounts[x[0]] || {}; return { platform: x[0], label: x[1], live: { connected: true, name: cfg.name, handle: HANDLE, bio: "Daily UK banter and the best clips on the internet 🇬🇧", link: "https://example.com/" + HANDLE.slice(1), url: "#", error: null }, draft: { bio: a.bio || "Daily UK banter and the best clips on the internet 🇬🇧", link: a.link || "https://example.com", name: cfg.name, notes: a.notes || "", updated_at: iso(NOW - 2 * D) }, limit: x[0] === "x" ? 160 : 150, edit_url: "#", can_push: x[0] === "facebook" }; }) });
      if (p === "/api/sources") return J({ items: S.sources });
      if (p === "/api/backups") return J({ items: [0, 1, 2, 3, 4, 5, 6].map(function (i) { return { name: "backup-" + new Date(NOW - i * D).toISOString().slice(0, 10) + ".json.gz", size: ri(300000, 900000), modified: iso(NOW - i * D) }; }), keep: 14 });
      if (p === "/api/audit") return J({ items: [["Signed in with passkey", "Chrome on laptop"], ["Connected Instagram", HANDLE], ["Published post", "5 platforms"], ["Scheduled 6 posts", "tomorrow 18:00"], ["Settings changed", "posting slots"], ["Backup created", "daily"]].map(function (x, i) { return { ts: iso(NOW - i * 5 * H), event: x[0], detail: x[1] }; }) });
      if (p === "/api/ai-usage") return J({ month_usd: 4.82, week_usd: 1.07, videos_month: 71, per_video_usd: 0.07, kinds: [{ kind: "caption", cost: 1.9, calls: 210 }, { kind: "transcribe", cost: 1.6, calls: 71 }, { kind: "vision", cost: 0.9, calls: 71 }, { kind: "reply", cost: 0.4, calls: 38 }] });
      if (p === "/api/ai-status") return J({ ok: true, kind: "", message: "" });
      if (p === "/api/comments/config") return J({ saved_replies: ["Thanks for watching! 🙌", "Cheers mate 😂"], hide_words: ["spam"] });
      if (p === "/api/comments/platforms") return J({ platforms: PLATS.map(function (x) { return { id: x[0], connected: true, available: x[0] !== "tiktok" }; }) });
      if (p === "/api/comments") return J(commentsFor(q));
      if (p === "/api/bio") return J(bioData());
      if (p === "/api/clips") return J({ history: S.clipHist });
      if ((m = p.match(/^\/api\/clip\/([^/]+)$/))) {
        var cj = S.clipJobs[m[1]], el3 = cj ? (Date.now() - cj.started) / 1000 : 99;
        if (el3 < 6) return J({ status: "running", stage: "analysing", stage_label: ["Fetching the video", "Listening to the audio", "Finding the best moments"][Math.min(2, Math.floor(el3 / 2))], clips: [] });
        return J(clipResult(m[1]));
      }
      if ((m = p.match(/^\/api\/clip\/([^/]+)\/words$/))) return J({ words: [], text: "so this is how the whole thing started and honestly nobody saw it coming at all" });
      if (p === "/api/clip-watch") return J({ items: S.watch || [] });
      if (p === "/api/uploads/status") return J({ ok: true });
      if (p.indexOf("/api/") === 0 || p.indexOf("/jobs") === 0) { console.warn("[demo] unhandled GET", p); return J({ items: [] }); }
      return null;
    }
    // ---- writes ----
    var b = body || {};
    if (p === "/api/uploads/start") return J({ direct: false });
    if (/^\/(process|process-file|process-upload|process-finished|process-finished-file)$/.test(p)) return J({ job_id: newJob() });
    if (p === "/api/batch") { var ids = (b.urls || [1]).map(function () { return newJob(); }); return J({ job_ids: ids }); }
    if ((m = p.match(/^\/jobs\/([^/]+)\/(.+)$/))) {
      var jj = S.jobs[m[1]]; if (!jj) return J({ ok: true });
      var act = m[2];
      if (act === "regenerate-caption") { jj.on_screen = pick(CAPS); jj.busy = Date.now() + 1800; }
      else if (act === "regenerate-video") { jj.busy = Date.now() + 2500; }
      else if (act === "set-on-screen-caption") { jj.on_screen = b.on_screen_caption; if (b.dx != null) jj.adj = { dx: b.dx, dy: b.dy, scale: b.scale }; jj.busy = Date.now() + 1800; }
      else if (act === "posting-caption") { jj.core = b.caption; if (b.sync) { var np = mkPosts(b.caption, jj.title); np.youtube.title = jj.posts.youtube.title; jj.posts = np; } save(); return J({ ok: true, platform_posts: b.sync ? jj.posts : null }); }
      else if (act === "platform-posts") { if (b.platform_posts) jj.posts = b.platform_posts; save(); return J({ ok: true }); }
      else if (act === "platform-posts/generate") { jj.posts = mkPosts(jj.core, jj.title); save(); return J({ platform_posts: jj.posts }); }
      else if (act === "reroll-hashtags") { return J({ platform_posts: jj.posts }); }
      else if (act === "closed-captions") { if (b.cues) jj.cues = b.cues; jj.busy = Date.now() + 2000; }
      else if (act === "risk-check") { return J({ level: "low", notes: ["No obvious copyright flags in this demo clip."] }); }
      else if (act === "angle") { jj.angle = b.angle || ""; }
      else if (act === "hooks") { return J({ hooks: ["Wait for the ending 😳😳", "\"He really thought that would work\" 💀", "Nobody was ready for this 🤯", "The way he said it 😭", "She had NO idea 💀💀"] }); }
      save(); return J({ ok: true });
    }
    if (p === "/publish") { var pid = uid(); S.jobs["pub_" + pid] = { started: Date.now(), platforms: b.platforms || [] }; var hh2 = S.history.filter(function (x) { return x.id === b.job_id; })[0]; if (hh2) (b.platforms || []).forEach(function (pl) { hh2.publish_results[pl] = { at: iso(Date.now()), ok: true, status: "done" }; }); save(); return J({ publish_job_id: pid }); }
    if (p === "/api/schedule" && method === "POST") {
      var out = (b.items || []).map(function (it) { var row = { id: uid(), history_id: b.history_id, platform: it.platform, label: LAB[it.platform] || it.platform, run_at: it.run_at, status: "scheduled", error: "", post_url: "", title: (S.jobs[b.history_id] || {}).title || "Post", on_screen_caption: (S.jobs[b.history_id] || {}).on_screen || "", video_url: CLIP }; S.schedule.push(row); return row; }); save(); return J({ items: out });
    }
    if ((m = p.match(/^\/api\/schedule\/([^/]+)(?:\/(post-now|repost))?$/))) {
      var row2 = S.schedule.filter(function (x) { return x.id === m[1]; })[0];
      if (method === "DELETE") S.schedule = S.schedule.filter(function (x) { return x.id !== m[1]; });
      else if (row2 && m[2] === "post-now") { row2.status = "done"; row2.post_url = "#"; row2.error = ""; row2.run_at = iso(Date.now()); }
      else if (row2 && m[2] === "repost") { S.schedule.push(Object.assign({}, row2, { id: uid(), run_at: b.run_at, status: "scheduled" })); }
      else if (row2 && b.run_at) { row2.run_at = b.run_at; if (row2.status !== "scheduled") { row2.status = "scheduled"; row2.error = ""; } }
      save(); return J({ ok: true });
    }
    if ((m = p.match(/^\/history\/([^/]+)\/(reopen|duplicate)$/))) { if (m[2] === "duplicate") { var src = S.history.filter(function (x) { return x.id === m[1]; })[0]; var nid = uid() + uid().slice(0, 4); if (src) { S.history.unshift(Object.assign({}, src, { id: nid, created_at: iso(Date.now()), publish_results: {} })); save(); } return J({ id: nid }); } return J({ ok: true }); }
    if ((m = p.match(/^\/history\/([^/]+)$/)) && method === "DELETE") { S.history = S.history.filter(function (x) { return x.id !== m[1]; }); save(); return J({ ok: true }); }
    if (p === "/api/campaigns" && method === "POST") { var c = Object.assign({ id: uid().slice(0, 12), posts: 0, share_token: uid().slice(0, 8) }, b); S.campaigns.push(c); save(); return J(c); }
    if ((m = p.match(/^\/api\/campaigns\/([^/]+)$/))) { if (method === "DELETE") S.campaigns = S.campaigns.filter(function (x) { return x.id !== m[1]; }); else S.campaigns = S.campaigns.map(function (x) { return x.id === m[1] ? Object.assign(x, b) : x; }); save(); return J({ ok: true }); }
    if (p === "/api/watermark") return J({ token: uid(), url: thumb(2) });
    if (p === "/api/watermarks" && method === "POST") { S.watermarks.push({ token: uid(), name: b.name || "Watermark", pos: b.pos || "right", url: thumb(3) }); save(); return J({ ok: true }); }
    if (p === "/api/settings" && method === "PUT") { S.settings = Object.assign(S.settings, b); save(); return J(S.settings); }
    if (p === "/api/goals" && method === "PUT") { S.goals = Object.assign(S.goals, b); save(); return J({ ok: true }); }
    if (p === "/api/ideas" && method === "POST") { var idea = { id: uid(), text: b.text || "", created_at: iso(Date.now()) }; S.ideas.unshift(idea); save(); return J(idea); }
    if ((m = p.match(/^\/api\/ideas\/([^/]+)$/)) && method === "DELETE") { S.ideas = S.ideas.filter(function (x) { return x.id !== m[1]; }); save(); return J({ ok: true }); }
    if (p === "/api/explore/scan") return J({ ok: true, found: 0 });
    if (p === "/api/explore/clear") { S.explore = []; save(); return J({ ok: true }); }
    if ((m = p.match(/^\/api\/explore\/([^/]+)\/([a-z]+)$/))) { S.explore.forEach(function (x) { if (x.id === m[1]) x.status = m[2] === "dismiss" ? "dismissed" : m[2] === "save" ? "saved" : x.status; }); save(); return J({ ok: true, job_id: m[2] === "use" ? newJob() : null }); }
    if (p === "/api/explore/config" && method === "PUT") return J({ ok: true });
    if (p.indexOf("/api/messages/") === 0 && /reply$/.test(p)) { (S.replies[b.recipient_id] = S.replies[b.recipient_id] || []).push({ mine: true, text: b.text }); save(); return J({ ok: true }); }
    if ((m = p.match(/^\/api\/requests\/([^/]+)(?:\/(.+))?$/))) { var rq = S.requests.filter(function (x) { return x.id === m[1]; })[0]; if (rq) { var a2 = m[2] || ""; if (/confirm|accept|approve/.test(a2)) rq.status = "confirmed"; else if (/decline|reject/.test(a2)) rq.status = "declined"; else if (/done|complete/.test(a2)) rq.status = "done"; if (b && b.owner_note != null) rq.owner_note = b.owner_note; } save(); return J({ ok: true }); }
    if (p === "/api/comments/suggest") return J({ replies: ["Cheers, glad you liked it! 😂", "Appreciate you mate 🙌", "More coming tomorrow, stay tuned 👀"] });
    if (p === "/api/comments/config") return J({ ok: true });
    if (/^\/api\/comments\//.test(p)) return J({ ok: true });
    if (p === "/api/bio/page" || p === "/api/bio/link" || /^\/api\/bio\//.test(p)) return J({ ok: true });
    if ((m = p.match(/^\/api\/accounts\/([^/]+)$/))) { S.accounts[m[1]] = Object.assign(S.accounts[m[1]] || {}, b); save(); return J({ ok: true }); }
    if ((m = p.match(/^\/api\/sources\/([^/]+)$/))) { S.sources.forEach(function (x) { if (x.id === m[1]) { x.rights = Object.assign(x.rights, b.rights || b); x.credited = b.credited != null ? b.credited : x.credited; } }); save(); return J({ ok: true }); }
    if (p === "/api/notify-test") return J({ ok: true });
    if (p === "/api/backup/run") return J({ ok: true });
    if (p === "/api/translate") return J({ text: b.text || "" });
    if (p === "/api/clip") { var cid = "clip" + uid().slice(0, 6); S.clipJobs[cid] = { started: Date.now(), jobs: {} }; S.clipHist.unshift({ id: cid, title: "Your clip session (sample)", status: "done", clip_count: 6, created_at: iso(Date.now()) }); save(); return J({ clip_id: cid }); }
    if ((m = p.match(/^\/api\/clip\/([^/]+)\/edit$/))) { var nj = newJob(); return J({ job_id: nj }); }
    if (/^\/api\/clip-watch/.test(p)) { if (method === "DELETE") S.watch = []; else S.watch = (S.watch || []).concat([{ id: uid(), url: b.url || "", title: "Latest episode (sample)" }]); save(); return J({ ok: true }); }
    if (/^\/api\/clip/.test(p)) return J({ ok: true });
    console.warn("[demo] unhandled", method, p);
    return J({ ok: true });
  }
  function commentsFor(q) {
    var items = [], names = ["sample_fan", "banter_lad", "chippy_queen", "matchday_mike", "pubquiz_pete", "gran_gran"], txt = ["Absolutely brilliant 😂", "Mate, I can't breathe", "Where was this filmed??", "Needs a part 2", "This is why I follow you", "Sent this to the whole group chat", "Not gonna lie, that ending 💀", "First! 🥇"];
    for (var i = 0; i < 12; i++) { var po = INS.instagram.posts[i % 5]; items.push({ id: "cm" + i, post_id: po.id, media_caption: po.title, post_url: "#", permalink: "#", post_thumb: po.thumb, post_ts: po.ts, username: names[i % names.length], text: txt[i % txt.length], ts: iso(NOW - i * 2 * H), likes: ri(0, 40), hidden: false, replied: i % 4 === 0, replies: i % 4 === 0 ? [{ username: "you", mine: true, text: "Cheers mate 😂" }] : [] }); }
    return { items: items, config: { saved_replies: ["Thanks for watching! 🙌", "Cheers mate 😂"], hide_words: ["spam"] } };
  }
  function clipResult(id) {
    var ids = [0, 1, 2, 3, 4, 5], cl = ids.map(function (i) { return { id: i, title: TITLES[(i + 3) % TITLES.length], score: 96 - i * 4, energy: 90 - i * 5, start: 40 + i * 95, end: 40 + i * 95 + ri(28, 55), reason: pick(["Strong hook and a clear payoff.", "Big reaction in the first three seconds.", "Quotable line, easy to caption.", "Funny escalation that ends on a laugh."]), text: "so this is how the whole thing started and honestly nobody saw it coming at all", flag: "", video_url: CLIP, job_id: (S.clipJobs[id] && S.clipJobs[id].jobs && S.clipJobs[id].jobs[i]) || null }; });
    return { status: "done", stage_label: "Done", video_url: CLIP, clips: cl };
  }
  function bioData() {
    if (!S.bio) S.bio = { page: { title: cfg.name, handle: HANDLE.slice(1), text: "Daily UK banter. Link in bio for the good stuff.", contact_email: "hello@example.com", reel_price: 150, still_price: 40, currency: "£", deal_note: "Open to brand deals and collabs", socials: PLATS.slice(0, 4).map(function (x) { return { label: x[1], url: "#" }; }), pay_link_reel: "", pay_link_still: "", meeting_link: "", consult_blurb: "Book a 20 minute call about getting your page set up.", show_services: true, show_tool: true }, links: [{ id: "l1", label: "Latest YouTube Short", url: "#", clicks: 412 }, { id: "l2", label: "Merch store", url: "#", clicks: 188 }, { id: "l3", label: "Join the group chat", url: "#", clicks: 97 }], campaigns: S.campaigns.map(function (c) { return { id: c.id, name: c.name }; }) };
    return S.bio;
  }
  function clips() {
    return TITLES.slice(0, 6).map(function (t, i) { return { id: "cl" + i, title: t, source_url: "https://example.com/long/" + i, status: "ready", created_at: iso(NOW - i * 6 * H), duration: ri(40, 900), thumb: thumb(i), clips: [] }; });
  }

  /* ---------- fetch / XHR overrides ---------- */
  var realFetch = window.fetch;
  function mkResp(r) { return new Response(JSON.stringify(r[1]), { status: r[0], headers: { "Content-Type": "application/json" } }); }
  window.fetch = function (input, init) {
    var url = typeof input === "string" ? input : (input && input.url) || String(input);
    var method = ((init && init.method) || (input && input.method) || "GET").toUpperCase();
    var abs = new URL(url, location.origin);
    if (abs.origin !== location.origin) return Promise.reject(new TypeError("Network disabled in demo"));
    if (/^\/(static|files|sw\.js|manifest)/.test(abs.pathname)) return realFetch.apply(this, arguments);
    var body = {}; try { if (init && typeof init.body === "string") body = JSON.parse(init.body); } catch (e) {}
    var r = route(method, abs.pathname + abs.search, body);
    if (!r) return realFetch.apply(this, arguments);
    return new Promise(function (res) { setTimeout(function () { res(mkResp(r)); }, 60 + Math.random() * 120); });
  };
  var RealXHR = window.XMLHttpRequest;
  window.XMLHttpRequest = function () {
    var x = new RealXHR(), open = x.open, send = x.send;
    x.open = function (m, u) { x.__u = u; x.__m = m; return open.apply(x, arguments); };
    x.send = function () {
      var a = arguments;
      try {
        var uu = new URL(x.__u, location.origin);
        if (uu.origin !== location.origin || /^\/(api|process|publish|jobs)/.test(uu.pathname)) {
          var r = uu.origin === location.origin ? route((x.__m || "GET").toUpperCase(), uu.pathname + uu.search, {}) : [200, { ok: true }];
          r = r || [200, { ok: true }];
          Object.defineProperty(x, "status", { value: r[0] });
          Object.defineProperty(x, "responseText", { value: JSON.stringify(r[1]) });
          Object.defineProperty(x, "response", { value: JSON.stringify(r[1]) });
          Object.defineProperty(x, "readyState", { value: 4 });
          setTimeout(function () { try { x.upload && x.upload.onprogress && x.upload.onprogress({ lengthComputable: true, loaded: 1, total: 1 }); } catch (e) {} x.onload && x.onload(); x.onreadystatechange && x.onreadystatechange(); }, 400);
          return;
        }
      } catch (e) {}
      return send.apply(x, a);
    };
    return x;
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  try { if (navigator.serviceWorker) navigator.serviceWorker.register = function () { return Promise.reject(new Error("demo")); }; } catch (e) {}

  /* ---------- theme, branding, links ---------- */
  function rgb(h) { h = h.replace("#", ""); return parseInt(h.slice(0, 2), 16) + "," + parseInt(h.slice(2, 4), 16) + "," + parseInt(h.slice(4, 6), 16); }
  var st = document.createElement("style");
  st.textContent = ":root{--accent:" + cfg.color + " !important;--accent-dim:rgba(" + rgb(cfg.color) + ",.16) !important;}" +
    ".wvNavLogo img{display:none !important}.wvNavLogo .demoMark{font-weight:900;letter-spacing:.5px;font-size:15px;text-transform:uppercase;color:#fff;white-space:nowrap}.wvNavLogo .demoMark b{color:" + cfg.color + "}" +
    "#demoPill{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(10px + env(safe-area-inset-bottom,0px));z-index:99999;background:rgba(20,20,24,.92);border:1px solid rgba(255,255,255,.18);color:#fff;font:600 12px -apple-system,Segoe UI,Roboto,sans-serif;padding:7px 12px;border-radius:99px;display:flex;gap:8px;align-items:center;backdrop-filter:blur(10px)}#demoPill a{color:" + cfg.color + ";text-decoration:none}" +
    ".wvNavAuth{display:none !important}";
  (document.head || document.documentElement).appendChild(st);
  document.addEventListener("DOMContentLoaded", function () {
    var logo = document.querySelector(".wvNavLogo");
    if (logo && !logo.querySelector(".demoMark")) { var s = document.createElement("span"); s.className = "demoMark"; s.innerHTML = ""; var a = document.createElement("span"); a.textContent = cfg.name; var b2 = document.createElement("b"); b2.textContent = " Studio"; s.append(a, b2); logo.appendChild(s); }
    var pill = document.createElement("div"); pill.id = "demoPill"; pill.innerHTML = "Demo · sample data &nbsp;<a href=\"https://ig.me/m/wokevision_\" target=\"_blank\" rel=\"noopener\">Talk to us ↗</a>"; document.body.appendChild(pill);
    try { document.title = document.title.replace(/WokeVision/g, cfg.name); } catch (e) {}
    try { document.querySelectorAll("textarea[placeholder]").forEach(function (t) { if (/Clancy|OnlyFans/i.test(t.placeholder)) t.placeholder = "e.g. Dry British humour about the pub quiz argument, don't take it seriously. Or: mock the referee's flag-waving."; }); } catch (e) {}
  });
  // keep every in-app link inside the demo
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]"); if (!a) return;
    var h = a.getAttribute("href"); if (!h || h[0] !== "/" || /^\/(demo|static|files|api)/.test(h)) return;
    e.preventDefault(); location.href = "/demo" + (h === "/" ? "/editor" : h);
  }, true);
})();
