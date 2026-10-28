"use strict";
/* Tooltip system (DESIGN.md section 3): an info button + a bubble, both created with createElement/textContent only (never innerHTML),
   so explanation text stays in the DOM (screen readers via aria-describedby, tests via .tip-bubble) but is not on the page by default.
   Native Popover API (popover="manual"); without it the bubble is toggled with the `hidden` attribute. No dependencies. */
(function (root) {
  const HAS_POPOVER = typeof HTMLElement !== "undefined" && typeof HTMLElement.prototype.showPopover === "function";
  const T = { DELAY: 300, CLOSE_DELAY: 120, MARGIN: 8, seq: 0, openBtn: null, pinned: false, _timer: 0, _closeTimer: 0 };

  function mk(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

  /* the info button + bubble pair; `label` is the accessible name of the button */
  T.tip = function (text, o) {
    const id = "tip-" + (++T.seq), label = (o && o.label) || "More information";
    const btn = mk("button", "tip-btn", "i");
    btn.type = "button"; btn.setAttribute("aria-label", label); btn.setAttribute("aria-describedby", id); btn.setAttribute("aria-expanded", "false");
    const bub = mk("span", "tip-bubble", String(text));
    bub.id = id; bub.setAttribute("role", "tooltip");
    if (HAS_POPOVER) bub.setAttribute("popover", "manual"); else bub.hidden = true;
    const f = document.createDocumentFragment(); f.append(btn, bub); return f;
  };
  /* a terse visible badge (kind: warn | bad | info) that keeps the long text in a tooltip */
  T.badge = function (text, kind, tipText) {
    const b = mk("span", "tbadge " + (kind || "info")); b.append(mk("span", "tbadge-text", text));
    if (tipText) b.append(T.tip(tipText, { label: "About: " + text }));
    return b;
  };
  /* a heading/label followed by its info button */
  T.label = function (text, tipText) { const s = mk("span", "tip-label"); s.append(mk("span", null, text)); if (tipText) s.append(T.tip(tipText, { label: "About: " + text })); return s; };

  function bubbleOf(btn) { return btn && document.getElementById(btn.getAttribute("aria-describedby")); }
  function isOpen(bub) { return HAS_POPOVER ? bub.matches(":popover-open") : !bub.hidden; }

  function place(btn, bub) {
    const r = btn.getBoundingClientRect(), vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight, m = T.MARGIN;
    bub.style.left = "0px"; bub.style.top = "0px";
    const w = bub.offsetWidth, h = bub.offsetHeight;
    let x = Math.min(Math.max(m, r.left + r.width / 2 - w / 2), Math.max(m, vw - w - m));
    let y = r.bottom + 6;
    if (y + h + m > vh && r.top - 6 - h >= m) y = r.top - 6 - h;                 // flip above when it does not fit below
    bub.style.left = Math.round(x) + "px"; bub.style.top = Math.round(y) + "px";
  }
  T.open = function (btn, pin) {
    clearTimeout(T._timer); clearTimeout(T._closeTimer);
    const bub = bubbleOf(btn); if (!bub) return;
    if (T.openBtn && T.openBtn !== btn) T.close();
    if (HAS_POPOVER) { if (!bub.matches(":popover-open")) bub.showPopover(); } else { bub.hidden = false; bub.classList.add("fallback"); }
    btn.setAttribute("aria-expanded", "true"); T.openBtn = btn; T.pinned = !!pin; place(btn, bub);
  };
  T.close = function () {
    clearTimeout(T._timer); clearTimeout(T._closeTimer);
    const btn = T.openBtn; if (!btn) return;
    const bub = bubbleOf(btn);
    if (bub) { if (HAS_POPOVER) { if (bub.matches(":popover-open")) bub.hidePopover(); } else bub.hidden = true; }
    btn.setAttribute("aria-expanded", "false"); T.openBtn = null; T.pinned = false; T._hover = false;
  };
  const btnOf = t => (t && t.closest) ? t.closest(".tip-btn") : null;
  const bubOf = t => (t && t.closest) ? t.closest(".tip-bubble") : null;

  if (typeof document !== "undefined") {
    document.addEventListener("pointerover", e => {
      if (e.pointerType === "touch") return;
      const b = btnOf(e.target), bub = bubOf(e.target);
      if (b) { clearTimeout(T._closeTimer); if (T.openBtn !== b) { clearTimeout(T._timer); T._timer = setTimeout(() => { T.open(b, false); T._hover = true; }, T.DELAY); } }
      else if (bub) clearTimeout(T._closeTimer);                                  // hoverable: the pointer may enter the bubble
    });
    document.addEventListener("pointerout", e => {
      if (e.pointerType === "touch") return;
      const b = btnOf(e.target), bub = bubOf(e.target);
      if (!b && !bub) return;
      const to = e.relatedTarget;
      if ((b && (to === bubbleOf(b))) || (bub && btnOf(to) && bubbleOf(btnOf(to)) === bub) || bubOf(to) === bub) return;
      clearTimeout(T._timer);
      if (T.openBtn && !T.pinned && document.activeElement !== T.openBtn) T._closeTimer = setTimeout(T.close, T.CLOSE_DELAY);
    });
    document.addEventListener("focusin", e => { const b = btnOf(e.target); if (b && b.matches(":focus-visible")) T.open(b, false); });
    document.addEventListener("focusout", e => { const b = btnOf(e.target); if (b && T.openBtn === b) T.close(); });
    document.addEventListener("click", e => {
      const b = btnOf(e.target);
      if (b) {
        if (T.openBtn === b && !T.pinned && T._hover) { T.pinned = true; T._hover = false; return; }   // a click on a hover-opened bubble pins it
        if (T.openBtn === b) T.close(); else { T._hover = false; T.open(b, true); }
        return;
      }
      if (T.openBtn && !bubOf(e.target)) T.close();                                // a tap elsewhere closes
    });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && T.openBtn) { T.close(); } });   // dismissible without moving focus
    window.addEventListener("scroll", () => { if (T.openBtn) T.close(); }, true);
    window.addEventListener("resize", () => { if (T.openBtn) place(T.openBtn, bubbleOf(T.openBtn)); });
  }
  root.Tip = T;
  if (typeof module !== "undefined") module.exports = T;
})(typeof window !== "undefined" ? window : globalThis);
