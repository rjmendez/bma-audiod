// Loaded with ?dev: reloads the page when a file under static/ changes (long-poll on the mock server's /__reload).
let m = 0;
(async function loop() {
  for (;;) {
    try { const r = await (await fetch("/__reload?m=" + m, { cache: "no-store" })).json(); if (m && r.m > m) return location.reload(); m = r.m; }
    catch (e) { await new Promise(res => setTimeout(res, 1000)); }
  }
})();
