/* Auto-refresh: reload this tab when the frontend changes or the server
 * restarts, so edits made by the agent become visible without a manual F5.
 * How: poll the Last-Modified of a static asset (no extra /api route needed).
 * The server runs with uvicorn --reload, so backend edits restart it; if the
 * server is briefly down we reload once it is back. Skipped while the user is
 * typing in a field to avoid losing input. Included from index.html. */
(function () {
  var TARGET = "/static/app.js";
  var INTERVAL = 1500;
  var stamp = null;
  var down = false;

  function busyEditing() {
    var a = document.activeElement;
    var tag = a && a.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
  }

  function reloadNow() {
    if (busyEditing()) return false; // defer to the next tick
    location.reload();
    return true;
  }

  async function check() {
    try {
      var r = await fetch(TARGET, { method: "HEAD", cache: "no-store" });
      if (!r.ok) return;
      var m = r.headers.get("last-modified") || r.headers.get("etag") || "";
      if (!m) return;
      if (down) {
        if (reloadNow()) down = false; // server came back after a restart
        return;
      }
      if (stamp === null) { stamp = m; return; }
      if (m !== stamp) reloadNow();
    } catch (e) {
      down = true; // server unreachable (uvicorn reload in progress)
    }
  }

  setInterval(check, INTERVAL);
  setTimeout(check, 900);
})();
