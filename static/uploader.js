// Resumable direct-to-storage uploads (browser -> R2, 16MB parts, 3 in flight,
// automatic retries). Resolves to {key} when the whole file is stored, or to
// null when direct upload isn't available (caller then uses the old path).
// Re-selecting the same file after a refresh/drop continues where it stopped.
window.wvDirectUpload = async function (file, onProgress, onStatus) {
  const sk = "wvUp:" + file.name + ":" + file.size;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(sk) || "null"); } catch (_) {}
  const body = { filename: file.name, size: file.size };
  if (saved) { body.resume_key = saved.key; body.resume_upload_id = saved.upload_id; }
  let d;
  try {
    const r = await fetch("/api/uploads/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || "start failed");
  } catch (e) { return null; }
  if (!d.direct) return null;
  try { localStorage.setItem(sk, JSON.stringify({ key: d.key, upload_id: d.upload_id })); } catch (_) {}

  const ps = d.part_size, n = d.urls.length;
  const done = new Set(d.done || []);
  const loaded = new Array(n).fill(0);
  done.forEach((p) => { loaded[p - 1] = Math.min(ps, file.size - (p - 1) * ps); });
  const total = file.size;
  const report = () => { const sum = loaded.reduce((a, b) => a + b, 0); onProgress && onProgress(Math.min(100, Math.round(sum / total * 100))); };
  report();
  if (done.size && onStatus) onStatus("Resuming — " + done.size + " of " + n + " parts already uploaded");

  const queue = [];
  for (let i = 1; i <= n; i++) if (!done.has(i)) queue.push(i);
  let successes = 0, firstFail = false;

  function putPart(i) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("PUT", d.urls[i - 1]);
      x.upload.onprogress = (e) => { loaded[i - 1] = e.loaded; report(); };
      x.onload = () => (x.status >= 200 && x.status < 300) ? resolve() : reject(new Error("part " + i + " failed (" + x.status + ")"));
      x.onerror = () => reject(new Error("network"));
      x.ontimeout = () => reject(new Error("timeout"));
      x.timeout = 10 * 60 * 1000;
      x.send(file.slice((i - 1) * ps, Math.min(file.size, i * ps)));
    });
  }
  async function worker() {
    while (queue.length) {
      const i = queue.shift();
      let ok = false, lastErr;
      for (let a = 0; a < 6 && !ok; a++) {
        try { await putPart(i); ok = true; } catch (e) {
          lastErr = e; loaded[i - 1] = 0;
          // Nothing has ever worked (CORS / signing problem): give up so the caller can fall back.
          if (successes === 0 && a >= 1) { firstFail = true; throw lastErr; }
          await new Promise((r) => setTimeout(r, 1500 * (a + 1)));
        }
      }
      if (!ok) throw lastErr;
      successes++;
      loaded[i - 1] = Math.min(ps, file.size - (i - 1) * ps);
      report();
    }
  }
  try {
    await Promise.all([worker(), worker(), worker()]);
  } catch (e) {
    if (firstFail) { try { localStorage.removeItem(sk); } catch (_) {} return null; }
    throw new Error("Upload interrupted — select the same file again to resume. (" + e.message + ")");
  }
  const r2 = await fetch("/api/uploads/complete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: d.key, upload_id: d.upload_id, size: file.size }) });
  const c = await r2.json().catch(() => ({}));
  if (!r2.ok) throw new Error(c.detail || "Couldn't finish the upload.");
  try { localStorage.removeItem(sk); } catch (_) {}
  return { key: d.key };
};
