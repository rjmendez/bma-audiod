// Passkey sign-in for /login (WebAuthn). Loaded only when the server was started with --passkey-origin. The server verifies everything.
(function () {
  "use strict";
  var btn = document.getElementById("passkey-login");
  if (!btn || !window.PublicKeyCredential || !navigator.credentials) return;
  btn.hidden = false;
  function u2b(s) { s = s.replace(/-/g, "+").replace(/_/g, "/"); while (s.length % 4) s += "="; var b = atob(s), a = new Uint8Array(b.length); for (var i = 0; i < b.length; i++) a[i] = b.charCodeAt(i); return a.buffer; }
  function b2u(buf) { var a = new Uint8Array(buf), s = ""; for (var i = 0; i < a.length; i++) s += String.fromCharCode(a[i]); return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
  function say(t) { var p = document.querySelector(".msg"); if (!p) { p = document.createElement("p"); p.className = "msg"; p.setAttribute("role", "alert"); document.querySelector("main").insertBefore(p, document.querySelector("main").children[1]); } p.textContent = t; }
  var JSONH = { "Content-Type": "application/json" };
  btn.addEventListener("click", function () {
    var opts;
    fetch("/api/passkeys/login/options", { method: "POST", headers: JSONH, body: "{}" }).then(function (r) { if (!r.ok) throw new Error("options " + r.status); return r.json(); }).then(function (o) {
      opts = o;
      return navigator.credentials.get({ publicKey: { challenge: u2b(o.challenge), rpId: o.rpId, timeout: o.timeout, userVerification: o.userVerification, allowCredentials: [] } });
    }).then(function (c) {
      var r = c.response;
      return fetch("/api/passkeys/login/verify", { method: "POST", headers: JSONH, body: JSON.stringify({ challenge: opts.challenge, credential: { id: c.id, clientDataJSON: b2u(r.clientDataJSON), authenticatorData: b2u(r.authenticatorData), signature: b2u(r.signature) } }) });
    }).then(function (r) { if (r.ok) location.replace("/"); else say(r.status === 429 ? "Too many attempts: wait and retry." : "The passkey was not accepted."); })
      .catch(function () { say("Passkey sign-in did not complete."); });
  });
})();
