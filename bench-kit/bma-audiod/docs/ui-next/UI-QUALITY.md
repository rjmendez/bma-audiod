# UI quality

Hand-run audit with the written rubric (a11y, performance, theming, responsive, implementation integrity; 0 to 4), anti-pattern checklist and a distill pass. The tool itself was not installed or run. Every number below is enforced by `ui-next/test/test_ui_quality.py` (67 checks; static half always, browser half in Chrome or Edge).

## Scores

| Dimension | Score | Key finding | Fix / state |
|---|---|---|---|
| Accessibility | 3 | focus ring, labels, `role=status`/`aria-live`, radiogroup for beams, dialog for consent, reduced-motion still alternative; the waterfall time selection is pointer-only (keyboard has beam keys 0-8 and zoom, not selection) | contrast of 11 text pairs and 2 control pairs in light and dark: all >= 4.5 / >= 3; open: keyboard range selection |
| Performance | 3 | the Review frame loop ran in every view (58 rAF/s idle) | loop idles while Review is hidden and restarts on show; checked by frame counters; Live 1.7 % CPU desktop, 8.7 % phone 4x |
| Theming | 4 | one token set, dark block identical in `data-theme` and media query; no colour literal outside tokens (overlay and scrim colours are tokens) | checks: identical palettes, no literals, no pure black |
| Responsive | 3 | no horizontal overflow at 360/390/768 in all four views; every control >= 44 px high on phones, buttons >= 44 wide (zoom buttons were 36 wide, fixed); drag surfaces: `touch-action` pan-y on Live and Pan mode, none in Select | not verified on a physical device (BROWSERS.md checklist) |
| Implementation integrity | 3 | CSP-clean (no inline style attribute, no inline script), no framework | open: `review.js` is 33 KB and keeps one large closure |

## Anti-pattern checklist

| Pattern | Result |
|---|---|
| Default-font identity | system stack by design (no network fonts); hierarchy comes from the 4-step scale and mono for numbers |
| Grey text on coloured background | none: `--muted` is checked at >= 4.5:1 on bg and panel |
| Pure black / pure grey | waterfall background `#070a0d`, neutrals tinted; `#000` banned by a check (the old `.wf-wrap` was `#000`) |
| Nested cards | none; panels are flat, sections use rules |
| Bounce easing | none; the only animation is the recording-pill pulse, with a still alternative |

## Detector findings (before and after)

| Finding | Before | After | Check |
|---|---|---|---|
| flat-type-hierarchy | ui-next shell: 12/13/15/16 px mixed; template 11/12/12.5/13/15/18/26 | tokens 12/15/19/24 (ratios 1.25, 1.27, 1.26); computed h1 24, heading 19, body 15, label 12; no text under 12 px in any view | static: tokens exact, steps >= 1.25, no raw px font-size; browser: computed sizes, tree walk of all four views |
| cramped-padding (`.wf-wrap`) | 0 inset, canvas touching the border | 10 px inset (`--inset`), canvas fills the rest of the width | browser: padding 10, canvas width = wrapper - 22 |
| tiny-text (artifact template) | 22 uses at 11 to 13 px | body 14 px; labels, ticks and code 12 px; nothing below | static: no size under 12, body token 14 |

Mutations run (each turned the suite red, then reverted): `--muted` to a light grey (3 failures), `--fs-2` 19 to 16 (5), `--inset` 10 to 2 (2), `--hit` 44 to 30 (9), reduced-motion rule removed (1), template label 12 to 11 (1), Review loop never idling (1).

## Distill

Removed or folded: 30 panels to 12; duplicate fact displays (ARCHITECTURE.md); Review settings behind one disclosure; Truth-box toggle and Auto range kept, the per-transform parameter rows moved into "Analysis settings"; the "Record to be saved" preview is collapsed by default.
