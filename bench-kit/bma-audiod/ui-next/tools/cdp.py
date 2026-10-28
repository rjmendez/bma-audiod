"""Headless Edge or Chrome over the DevTools protocol, stdlib only: page navigation, emulation, screenshots, performance counters and network
events (bytes per request). The one browser driver of tests and tools; the live dashboard is never contacted."""
import atexit, base64, json, os, shutil, socket, struct, subprocess, sys, tempfile, time, urllib.request


def _first(paths):
    return next((p for p in paths if p and os.path.exists(p)), "")


def _program_files(*rel):
    return [os.path.join(os.environ.get(v, ""), *rel) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]


def find_edge():
    return shutil.which("msedge") or shutil.which("msedge.exe") or _first(_program_files("Microsoft", "Edge", "Application", "msedge.exe"))


def find_chrome():
    return (shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("chrome")
            or _first(_program_files("Google", "Chrome", "Application", "chrome.exe")))


def browser_exe(name):
    """Path of a Chromium-family browser by name: 'edge' (default) or 'chrome'; '' when it is not installed. UINEXT_BROWSER overrides both."""
    return os.environ.get("UINEXT_BROWSER") or (find_chrome() if name == "chrome" else find_edge())


class WS:
    def __init__(self, url):
        host, rest = url[5:].split("/", 1)
        h, port = host.split(":")
        self.s = socket.create_connection((h, int(port)), timeout=120)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        self.rest = buf.split(b"\r\n\r\n", 1)[1]
        self.id = 0
        self.events = []

    def _read(self, n):
        while len(self.rest) < n:
            self.rest += self.s.recv(1 << 20)
        out, self.rest = self.rest[:n], self.rest[n:]
        return out

    def _recv(self):
        payload = b""
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload += self._read(n)
            if b0 & 0x80:
                break
        return json.loads(payload) if payload else {}

    def call(self, method, params=None, timeout=120):
        self.id += 1
        data = json.dumps({"id": self.id, "method": method, "params": params or {}}).encode()
        n, mask = len(data), os.urandom(4)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        end = time.time() + timeout
        while time.time() < end:
            msg = self._recv()
            if msg.get("id") == self.id:
                return msg
            if "method" in msg:
                self.events.append(msg)
        raise TimeoutError(method)

    def pump(self, seconds):
        """Collect events for `seconds` (the socket has a read timeout: short polls)."""
        end = time.time() + seconds
        self.s.settimeout(0.25)
        try:
            while time.time() < end:
                try:
                    msg = self._recv()
                except (socket.timeout, TimeoutError):
                    continue
                if "method" in msg:
                    self.events.append(msg)
        finally:
            self.s.settimeout(120)


FLAGS = ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--enable-unsafe-webgpu", "--ignore-gpu-blocklist", "--autoplay-policy=no-user-gesture-required"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """One headless Chromium-family browser (Edge or Chrome) with a throw-away profile. port=None picks a free port; close() ends exactly the process tree started here."""

    def __init__(self, port=None, exe=None, extra=()):
        self.exe = exe or find_edge()
        if not self.exe:
            raise RuntimeError("browser not found")
        self.prof = tempfile.mkdtemp(prefix="uinext_prof_")
        self.port = port or free_port()
        self.proc = subprocess.Popen([self.exe, *FLAGS, *extra, "--remote-debugging-port=%d" % self.port, "--user-data-dir=" + self.prof, "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(self.close)                                  # an uncaught exception in a caller must not leave a browser tree behind
        pages, deadline = [], time.time() + 45
        while time.time() < deadline and not pages:
            try:
                pages = [t for t in json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/list" % self.port, timeout=2)) if t.get("type") == "page"]
            except OSError:
                pass
            if not pages:
                time.sleep(0.4)
        if not pages:
            self.close()
            raise RuntimeError("the browser exposed no page")
        self.ws = WS(pages[0]["webSocketDebuggerUrl"])
        for m in ("Page.enable", "Network.enable", "Performance.enable", "Runtime.enable"):
            self.ws.call(m)

    def viewport(self, w, h, mobile=False, dpr=1, cpu=1):
        self.ws.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": dpr, "mobile": mobile})
        self.ws.call("Emulation.setTouchEmulationEnabled", {"enabled": mobile})
        self.ws.call("Emulation.setCPUThrottlingRate", {"rate": cpu})

    def go(self, url, settle=2.0):
        self.ws.call("Page.navigate", {"url": "about:blank"})
        time.sleep(0.3)
        self.ws.events.clear()
        self.ws.call("Page.navigate", {"url": url})
        self.ws.pump(settle)

    def eval(self, expr, await_promise=True):
        for _ in range(6):                                           # a navigation can destroy the execution context between two calls: answered by an error, retried
            msg = self.ws.call("Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
            if "result" in msg:
                break
            self.ws.pump(0.5)
        else:
            raise RuntimeError("Runtime.evaluate failed: %s" % str(msg)[:300])
        r = msg["result"]
        if "exceptionDetails" in r:
            return {"exception": str(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"].get("text")))[:300]}
        return r["result"].get("value")

    def metrics(self):
        return {m["name"]: m["value"] for m in self.ws.call("Performance.getMetrics")["result"]["metrics"]}

    def shot(self, path, full=False):
        r = self.ws.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": full})["result"]["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(r))
        return os.path.getsize(path)

    def net(self):
        """-> {url: [count, encoded bytes]} from the events collected so far."""
        urls, out = {}, {}
        for e in self.ws.events:
            p = e.get("params", {})
            if e["method"] == "Network.requestWillBeSent":
                urls[p["requestId"]] = p["request"]["url"].split("?")[0].split("//", 1)[-1].split("/", 1)[-1]
            elif e["method"] == "Network.loadingFinished":
                u = "/" + urls.get(p["requestId"], "?")
                c = out.setdefault(u, [0, 0])
                c[0] += 1
                c[1] += int(p.get("encodedDataLength", 0))
        return out

    def ws_bytes(self):
        """-> [frames, payload bytes] received on WebSockets since the events were last cleared (binary payloads arrive base64 coded: decoded length is counted)."""
        n = b = 0
        for e in self.ws.events:
            if e["method"] == "Network.webSocketFrameReceived":
                pd = e["params"]["response"]
                n += 1
                b += (len(pd["payloadData"]) * 3 // 4) if pd.get("opcode") == 2 else len(pd["payloadData"].encode("utf-8"))
        return [n, b]

    def close(self):
        """End the whole tree. On Windows the process Popen started can hand over to another one and exit, so its pid alone kills nothing: the tree is found by the
        throw-away profile directory on the command line (unique per Browser), then killed with /T."""
        if getattr(self, "proc", None):
            if os.name == "nt":
                script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains('%s') -and $_.CommandLine -notmatch '--type=' } "
                          "| ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }" % self.prof.replace("'", "''"))
                subprocess.run(["powershell", "-NoProfile", "-Command", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.proc.kill()
            self.proc.wait(timeout=30)
            self.proc = None
        for _ in range(10):                                          # the profile is released a moment after the processes end
            shutil.rmtree(self.prof, ignore_errors=True)
            if not os.path.exists(self.prof):
                break
            time.sleep(0.5)
