// Sign-out and passkey registration for the signed-in dashboard (needs GET /api/session; shows nothing on a server without a login).
(function () {
  "use strict";
  function u2b(s) { s = s.replace(/-/g, "+").replace(/_/g, "/"); while (s.length % 4) s += "="; var b = atob(s), a = new Uint8Array(b.length); for (var i = 0; i < b.length; i++) a[i] = b.charCodeAt(i); return a.buffer; }
  function b2u(buf) { var a = new Uint8Array(buf), s = ""; for (var i = 0; i < a.length; i++) s += String.fromCharCode(a[i]); return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
  fetch("/api/session", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (S) {
    if (!S || S.authenticated !== true) return;
    var bar = document.createElement("div");
    bar.id = "session-bar";
    function post(path, body) { return fetch(path, { method: "POST", credentials: "same-origin", redirect: "manual", headers: { "Content-Type": "application/json", "X-CSRF-Token": S.csrf }, body: JSON.stringify(body || {}) }); }
    function mk(id, text, fn) { var b = document.createElement("button"); b.type = "button"; b.id = id; b.textContent = text; b.addEventListener("click", fn); bar.appendChild(b); return b; }
    var note = document.createElement("span");
    note.id = "session-note";
    if (S.via === "session" || S.via === "cookie") {
      mk("signout", "Sign out", function () { post("/logout").then(function () { location.replace("/login"); }); });
    }
    if (S.passkeys && S.passkeys.enabled && window.PublicKeyCredential && navigator.credentials) {
      mk("passkey-add", "Add a passkey", function () {
        var opts;
        post("/api/passkeys/register/options").then(function (r) { if (!r.ok) throw new Error("options"); return r.json(); }).then(function (o) {
          opts = o;
          return navigator.credentials.create({ publicKey: { challenge: u2b(o.challenge), rp: o.rp, user: { id: u2b(o.user.id), name: o.user.name, displayName: o.user.displayName }, pubKeyCredParams: o.pubKeyCredParams, timeout: o.timeout, attestation: "none", authenticatorSelection: o.authenticatorSelection, excludeCredentials: o.excludeCredentials.map(function (c) { return { type: c.type, id: u2b(c.id) }; }) } });
        }).then(function (c) {
          return post("/api/passkeys/register/verify", { challenge: opts.challenge, label: "passkey", credential: { id: c.id, clientDataJSON: b2u(c.response.clientDataJSON), attestationObject: b2u(c.response.attestationObject) } });
        }).then(function (r) { note.textContent = r.ok ? "Passkey added." : "Passkey not added."; }).catch(function () { note.textContent = "Passkey not added."; });
      });
    }
    if (!bar.children.length) return;
    bar.appendChild(note);
    document.body.appendChild(bar);
  }).catch(function () {});
})();
