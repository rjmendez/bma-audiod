#!/usr/bin/env python3
"""Builds the design-review page (HTML, self-contained) from template.html, the measurement JSON and a few prototype screenshots.
    python build_artifact.py AUDIT_JSON PROTO_JSON SHOTS_DIR OUT_HTML"""
import base64, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
cur = json.load(open(sys.argv[1], encoding="utf-8"))
pro = json.load(open(sys.argv[2], encoding="utf-8"))
shots = sys.argv[3]
html = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
kb = lambda b: "%.1f" % (b / 1024.0)


def img(name, alt, cls=""):
    with open(os.path.join(shots, name), "rb") as f:
        return '<figure class="%s"><img alt="%s" src="data:image/png;base64,%s"><figcaption>%s</figcaption></figure>' % (cls, alt, base64.b64encode(f.read()).decode(), alt)


s = ['<section><h2>The prototype, rendered</h2><p class="muted">Chrome, headless, synthetic data. The waterfall is WebGL2 fed by one WebSocket.</p><div class="shots">']
s.append(img("live_1280.png", "Live, desktop 1280 px"))
s.append(img("live_390.png", "Live, phone 390 px", "phone"))
s.append(img("review_1280.png", "Review, desktop 1280 px"))
s.append(img("review_390.png", "Review, phone 390 px", "phone"))
s.append("</div></section>")
html = html.replace("<!--SHOTS-->", "\n".join(s))

fl, pl = cur["static"]["first_load_requests"], pro["load"]
rows = ["<tr><th>Measure (Edge, same method)</th><th class='n'>Current</th><th class='n'>Prototype</th></tr>",
        "<tr><td>Panels</td><td class='n'>30 in 5 tabs</td><td class='n'>12 designed in 4 views, 10 built</td></tr>",
        "<tr><td>First load requests</td><td class='n'>%d</td><td class='n'>%d</td></tr>" % (cur["static"]["first_load_total"][0], pl["requests"]),
        "<tr><td>First load HTTP bytes</td><td class='n'>%s KB</td><td class='n'>%s KB</td></tr>" % (kb(cur["static"]["first_load_total"][1]), kb(pl["bytes"])),
        "<tr><td>Spectrum backlog on connect</td><td class='n'>none</td><td class='n'>%s KB, 60 s of 9 beams</td></tr>" % kb(pl["ws"][1])]
for ck, pk, name in (("overview", "live", "Live / Overview"), ("detections", "det", "Detections")):
    c, p = cur["tabs"][ck]["desktop"], pro["steady"][pk]["desktop"]
    rows.append("<tr><td>%s: steady network</td><td class='n'>%s KB/s</td><td class='n'>%s KB/s</td></tr>" % (name, kb(c["bytes_per_s"]), kb(p["bytes_per_s"])))
    rows.append("<tr><td>%s: DOM nodes</td><td class='n'>%d</td><td class='n'>%d</td></tr>" % (name, c["dom_nodes"], p["dom_nodes"]))
    rows.append("<tr><td>%s: CPU on a 4x-throttled phone</td><td class='n'>%s %%</td><td class='n'>%s %%</td></tr>" % (name, cur["tabs"][ck]["phone_4x"]["cpu_pct"], pro["steady"][pk]["phone_4x"]["cpu_pct"]))
rows.append("<tr><td>System: CPU on a 4x-throttled phone</td><td class='n'>%s %%</td><td class='n'>%s %%</td></tr>" % (cur["tabs"]["system"]["phone_4x"]["cpu_pct"], pro["steady"]["sys"]["phone_4x"]["cpu_pct"]))
html = html.replace("<!--NUMBERS-->", '<section><h2>Measured against the current dashboard</h2><div class="card wrap"><table>%s</table></div><p class="muted">The machine ran at 97 to 100 %% CPU during all runs: read ratios, not absolute values. Full tables and commands are in <code>docs/ui-next</code>.</p></section>' % "".join(rows))

choices = [
    ("Live listening present at all", "off, flag, owner approval", "Off"),
    ("Speech while listening", "mute when the latest tagger window has speech or is stale", "Mute on speech or stale tagger; say plainly that the first seconds can pass"),
    ("Server-side copy of live audio", "none", "None"),
    ("Keep this moment", "through the existing clip pipeline only", "Clip pipeline only; needs an analyzer endpoint"),
    ("Browser-side recording or download", "no button", "Off"),
    ("Audit trail", "local log of start, stop, beam; no audio", "On"),
    ("Concurrent listeners and length", "1 listener, 10 minutes, new consent to continue", "1 and 10 minutes"),
    ("Hidden tab", "stop after 30 s", "Stop after 30 s"),
]
t = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % c for c in choices)
html = html.replace("<!--CHOICES-->", '<section><h2>Open choices for the owner</h2><p>Live listening to a room is an interception question for an all-party-consent state; the prototype makes it explicit and removable and does not decide the law. Recommended defaults:</p><div class="card wrap"><table><tr><th>Choice</th><th>Option</th><th>Recommended default</th></tr>%s</table></div></section>' % t)
open(sys.argv[4], "w", encoding="utf-8", newline="\n").write(html)
print("wrote", sys.argv[4], len(html), "bytes")
