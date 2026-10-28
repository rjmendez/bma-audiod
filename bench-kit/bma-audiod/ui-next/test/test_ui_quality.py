"""UI quality gates, machine-checked: palette contrast in both themes, the type scale, touch targets, horizontal overflow at 360/390/768 px,
the waterfall inset, a reduced-motion rule for every animation and a CSP-clean stylesheet.
The static half needs no browser and always runs; the browser half prints SKIP with the reason when no Chrome/Edge is installed."""
import os
import re
import sys

from browsertest import Fixture, parser, UI
import cdp

CSS = open(os.path.join(UI, "static", "app.css"), encoding="utf-8").read()
HTML = open(os.path.join(UI, "static", "index.html"), encoding="utf-8").read()
TEMPLATE = open(os.path.join(UI, "tools", "artifact", "template.html"), encoding="utf-8").read()
VIEWS = ("live", "review", "det", "sys")
HEX = r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b"


def lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def palettes():
    light = dict(re.findall(HEX, re.search(r":root \{(.*?)\n\}", CSS, re.S).group(1)))
    dark1 = dict(re.findall(HEX, re.search(r':root\[data-theme="dark"\] \{(.*?)\}', CSS, re.S).group(1)))
    dark2 = dict(re.findall(HEX, re.search(r'prefers-color-scheme: dark\) \{ :root:not\(\[data-theme="light"\]\) \{(.*?)\}', CSS, re.S).group(1)))
    return light, dark1, dark2


TEXT_PAIRS = [("ink", "bg"), ("ink", "panel"), ("muted", "bg"), ("muted", "panel"), ("accent", "panel"), ("accent", "bg"), ("good", "panel"),
              ("warn", "panel"), ("bad", "panel"), ("on-accent", "accent"), ("on-rec", "rec")]
CONTROL_PAIRS = [("ctl", "panel"), ("ctl", "bg")]


def static_checks(fx):
    light, dark1, dark2 = palettes()
    fx.check("static: the two dark blocks carry the identical palette", dark1 == dark2 and len(dark1) >= 10, "%s vs %s" % (dark1, dark2))
    for name, pal in (("light", light), ("dark", {**light, **dark1})):
        for a, b in TEXT_PAIRS:
            r = ratio(pal["--" + a], pal["--" + b])
            fx.check("static: %s text --%s on --%s = %.2f >= 4.5" % (name, a, b, r), r >= 4.5, r)
        for a, b in CONTROL_PAIRS:
            r = ratio(pal["--" + a], pal["--" + b])
            fx.check("static: %s control border --%s on --%s = %.2f >= 3" % (name, a, b, r), r >= 3.0, r)
    fs = [int(x) for x in re.findall(r"--fs-\d:\s*(\d+)px", CSS)]
    fx.check("static: type tokens are exactly 12/15/19/24", fs == [12, 15, 19, 24], fs)
    fx.check("static: every step is at least 1.25x the one below", all(b / a >= 1.25 for a, b in zip(fs, fs[1:])), fs)
    raw = re.findall(r"font-size:\s*\d+px", CSS)
    fx.check("static: no raw px font-size outside the tokens", raw == [], raw)
    lits = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", re.sub(r":root[^{]*\{[^}]*\}", "", CSS))
    fx.check("static: no colour literal outside the token blocks", lits == [], lits)
    anims = len(re.findall(r"animation:\s+(?!none)", CSS))
    still = len(re.findall(r"prefers-reduced-motion: reduce\)[^\n]*animation:\s*none", CSS))
    fx.check("static: every animation (%d) has a reduced-motion rule (%d)" % (anims, still), anims >= 1 and still == anims, (anims, still))
    fx.check("static: no inline style attribute in the shell (CSP style-src self)", " style=" not in HTML, "style= found")
    sizes = [float(x) for x in re.findall(r"(?:font-size:|font:|--fs-\d:)\s*(\d+(?:\.\d+)?)px", TEMPLATE)]
    fx.check("static: artifact template sets no text under 12 px (sizes %s)" % sorted(set(sizes)), sizes != [] and min(sizes) >= 12, sizes)
    fx.check("static: artifact template body is 14 px", re.search(r"--fs-1:\s*14px", TEMPLATE) is not None and "font: var(--fs-1)" in TEMPLATE, "body token")
    fx.check("static: no pure black literal", re.search(r"#000(?:000)?\b", CSS) is None, "#000")


SMALL_JS = """(function(){var out=[],w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  while(w.nextNode()){var n=w.currentNode;if(!n.nodeValue.trim())continue;var e=n.parentElement;if(!e||e.closest('script,style,[hidden]'))continue;
    if(!e.getClientRects().length)continue;var s=parseFloat(getComputedStyle(e).fontSize);if(s<12)out.push(e.tagName+'.'+e.className+':'+s)}
  return out.slice(0,5)})()"""

HIT_JS = """(function(){var out=[];document.querySelectorAll('button,select,input,summary').forEach(function(e){
  if(e.closest('[hidden],dialog:not([open])')||!e.getClientRects().length||e.type==='hidden')return;
  var t=e.type==='checkbox'?(e.closest('label')||e):e,r=t.getBoundingClientRect();
  if(r.height<43.5||(r.width<43.5&&e.tagName==='BUTTON'))out.push((e.id||e.className||e.tagName)+':'+Math.round(r.width)+'x'+Math.round(r.height))});
  return out.slice(0,6)})()"""


def browser_checks(fx):
    fx.go("/", 1280, 800)
    fx.wait("window.__ui && document.querySelector('h1')", 15)
    s = fx.ev("""(function(){var f=function(q){return parseFloat(getComputedStyle(document.querySelector(q)).fontSize)};
      return {h1:f('h1'),h2:f('.queue summary'),body:f('body'),label:f('.strip')};})()""")
    fx.check("browser: computed h1 24, queue heading 19, body 15, label 12", (s["h1"], s["h2"], s["body"], s["label"]) == (24, 19, 15, 12), s)
    fx.check("browser: computed steps >= 1.25", s["h1"] / s["h2"] >= 1.25 and s["h2"] / s["body"] >= 1.25 and s["body"] / s["label"] >= 1.25, s)
    for v in VIEWS:
        fx.nav(v)
        small = fx.ev(SMALL_JS)
        fx.check("browser: view %s has no text under 12 px" % v, small == [], small)
    fx.nav("live")
    wf = fx.ev("""(function(){var w=document.querySelector('.wf-wrap'),c=getComputedStyle(w),k=document.querySelector('.wf-wrap canvas').getBoundingClientRect(),r=w.getBoundingClientRect();
      return {l:parseFloat(c.paddingLeft),t:parseFloat(c.paddingTop),cw:k.width,ww:r.width};})()""")
    fx.check("browser: .wf-wrap inset is 10 px and the canvas fills the rest of the width", wf["l"] == 10 and wf["t"] == 10 and abs(wf["cw"] - (wf["ww"] - 22)) <= 1, wf)
    fx.nav("review")
    fx.wait("window.__lab && __lab.S.fr", 30)
    r0 = fx.ev("__lab.S.stats.rafs"); fx.br.ws.pump(1.5); r1 = fx.ev("__lab.S.stats.rafs")
    fx.check("browser: the review frame loop runs while Review is shown (%s -> %s frames in 1.5 s; headless rAF is throttled under load, so only progress is required)" % (r0, r1), r1 - r0 >= 1, (r0, r1))
    fx.nav("live")
    fx.br.ws.pump(0.5); r0 = fx.ev("__lab.S.stats.rafs"); fx.br.ws.pump(1.5); r1 = fx.ev("__lab.S.stats.rafs")
    fx.check("browser: the review frame loop idles while another view is shown (%s -> %s)" % (r0, r1), r1 == r0, (r0, r1))
    fx.nav("review")
    fx.br.ws.pump(0.5); r0 = fx.ev("__lab.S.stats.rafs"); fx.br.ws.pump(1.0); r1 = fx.ev("__lab.S.stats.rafs")
    fx.check("browser: the frame loop restarts when Review is shown again (%s -> %s)" % (r0, r1), r1 - r0 >= 1, (r0, r1))
    for w, h in ((360, 740), (390, 844), (768, 1024)):
        fx.go("/", w, h, mobile=True)
        fx.wait("window.__ui && document.querySelector('h1')", 15)
        for v in VIEWS:
            fx.nav(v)
            fx.br.ws.pump(0.4)
            ov = fx.ev("document.documentElement.scrollWidth - window.innerWidth")
            fx.check("browser: %d px, view %s: horizontal overflow %s px <= 0" % (w, v, ov), ov <= 0, ov)
            if w <= 390:
                bad = fx.ev(HIT_JS)
                fx.check("browser: %d px, view %s: every control is >= 44 px high, buttons also wide" % (w, v), bad == [], bad)
    fx.go("/", 1280, 800)
    fx.check("browser: no page errors", fx.errors() == [], fx.errors())


def main():
    args = parser(__doc__).parse_args()
    if not cdp.browser_exe(args.browser):
        pre = Fixture.__new__(Fixture)
        pre.args, pre.n, pre.fails, pre.skips = args, 0, [], 0
        static_checks(pre)
        pre.skip("browser half", "%s not found" % args.browser)
        return pre.finish()
    with Fixture(args) as fx:
        static_checks(fx)
        browser_checks(fx)
        return fx.finish()


if __name__ == "__main__":
    sys.exit(main())
