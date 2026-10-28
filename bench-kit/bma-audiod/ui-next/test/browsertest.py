"""Shared fixture of the browser tests: the mock server in a thread, one headless browser (Edge or Chrome) over DevTools, PASS/FAIL bookkeeping.

A test file does:   with Fixture(args) as fx: fx.check(name, ok, detail) ... ; sys.exit(fx.finish())
Exit codes: 0 all passed, 1 a failure, 77 no browser installed (printed as SKIP: nothing ran, this is not a pass)."""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(UI, "tools"))
sys.path.insert(0, UI)
import cdp  # noqa: E402
import mock_server  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")             # check names may carry non-ASCII text; a Windows console is cp1252


def parser(desc):
    ap = argparse.ArgumentParser(description=desc)
    ap.add_argument("--browser", default=os.environ.get("UINEXT_BROWSER_NAME", "chrome"), choices=("edge", "chrome"))
    ap.add_argument("--require-webgl", action="store_true", help="a missing WebGL2 is a failure (default: the WebGL checks are skipped with the reason)")
    return ap


def poll(evaluate, pump, cond, timeout=25.0, step=0.25):
    """True when the JavaScript condition `cond` is truthy within the timeout. A page error (cdp.eval answers {'exception': ...}, which is truthy) never counts."""
    end = time.time() + timeout
    while time.time() < end:
        pump(step)
        v = evaluate("!!(%s)" % cond)
        if v is True:
            return True
    return False


class Fixture:
    def __init__(self, args):
        self.args, self.n, self.fails, self.skips = args, 0, [], 0
        self.exe = cdp.browser_exe(args.browser)

    def __enter__(self):
        if not self.exe:
            print("SKIP: %s not found; nothing ran, this is not a pass" % self.args.browser)
            sys.exit(77)
        self.srv = mock_server.Server(("127.0.0.1", 0), mock_server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.br = cdp.Browser(exe=self.exe)
        return self

    def __exit__(self, *exc):
        self.br.close()
        self.srv.shutdown()

    # ---- bookkeeping
    def check(self, name, ok, detail=""):
        self.n += 1
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  [%s]" % (detail,)), flush=True)
        if not ok:
            self.fails.append(name)

    def skip(self, name, why):
        self.n += 1
        self.skips += 1
        print("SKIP %s  [%s]" % (name, why), flush=True)

    def finish(self):
        print("\n%d checks, %d failure(s), %d skipped" % (self.n, len(self.fails), self.skips))
        return 1 if self.fails else 0

    # ---- page helpers
    def go(self, path, w=1280, h=800, mobile=False, cpu=1):
        self.br.viewport(w, h, mobile=mobile, cpu=cpu)
        self.br.go(self.base + path, 0.5)

    def ev(self, expr):
        return self.br.eval(expr)

    def wait(self, cond, timeout=25.0, step=0.25):
        """Poll a JavaScript condition; True when it held within the timeout."""
        return poll(self.br.eval, self.br.ws.pump, cond, timeout, step)

    def errors(self):
        out = []
        for e in self.br.ws.events:
            if e["method"] == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                out.append("exception: " + str(d.get("exception", {}).get("description", d.get("text")))[:200])
            elif e["method"] == "Runtime.consoleAPICalled" and e["params"].get("type") == "error":
                out.append("console.error: " + json.dumps([x.get("value", x.get("description")) for x in e["params"]["args"]])[:200])
        return out

    def key(self, k, text=None):
        self.ev("document.activeElement && document.activeElement.blur()")
        self.br.ws.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": k, "text": text if text is not None else (k if len(k) == 1 else "")})
        self.br.ws.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": k})
        self.br.ws.pump(0.15)

    def mouse(self, kind, x, y):
        self.br.ws.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "buttons": 0 if kind == "mouseReleased" else 1, "clickCount": 1})

    def rect(self, selector):
        return self.ev("(function(){var r=document.querySelector(%s).getBoundingClientRect();return {x:r.left,y:r.top,w:r.width,h:r.height};})()" % json.dumps(selector))

    def nav(self, view):
        self.ev("document.querySelector('[data-view=%s]').click()" % view)
        self.br.ws.pump(0.3)
