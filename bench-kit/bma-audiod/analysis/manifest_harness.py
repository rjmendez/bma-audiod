#!/usr/bin/env python3
"""Shared harness of the manifest tests (analysis/test_manifest.py, docker/test_install_lists.py, test_indices.py, test_mixscore.py): a paced synthetic 8-beam UDP stream
(free_port, wait_listening, stream) for host.py end-to-end runs, and readers of the (source, destination) pairs pc/install_task.ps1 really copies (the manifest
block run in PowerShell with Copy-Item replaced by a recorder; NoPowerShell where there is none) and analyzer.Dockerfile really COPYs.
It was the capture half of the one-time golden proof of the manifest refactor (see docs/design/w3-plugin-manifest.md, 12.7); the golden check is retired."""

import json, os, re, shutil, socket, struct, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
BMA = os.path.dirname(HERE)
FS = 48000


def lf(text):
    return text.replace("\r\n", "\n")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def bmaa(slot, block, x):
    import numpy as np

    s24 = np.clip((x * 8388608.0).astype(np.int64), -8388608, 8388607).astype("<i4")
    return b"BMAA" + bytes([1, 0, slot, 0]) + struct.pack("<III", 0, block & 0xFFFFFFFF, len(x)) + s24.tobytes()


def wait_listening(port, proc, timeout=90.0):
    """True once the host holds the UDP port (a second bind fails with address in use); False when it exited first or never did."""
    end = time.time() + timeout
    while time.time() < end and proc.poll() is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
        finally:
            s.close()
        time.sleep(0.2)
    return False


def stream(port, seconds):
    """Deterministic 8-beam noise (seed 1234) with a source in beam 6 for the second half, paced by the stream clock."""
    import numpy as np

    rng = np.random.default_rng(1234)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blk, n_blocks, t_start = 0, 0, time.time()
    for sec in range(seconds):
        x = (rng.standard_normal((FS, 8)) * 10 ** (-60 / 20)).astype(np.float32)
        if sec >= seconds // 2:
            x[:, 5] += (rng.standard_normal(FS) * 10 ** (-38 / 20)).astype(np.float32)
        for off in range(0, FS, 256):
            for slot in range(8):
                sk.sendto(bmaa(slot, blk + off, x[off : off + 256, slot]), ("127.0.0.1", port))
            n_blocks += 1
            d = t_start + n_blocks * 256 / FS - time.time()
            if d > 0:
                time.sleep(d)
        blk += FS
    sk.close()


def ps1_pairs_text(text):
    """(source, destination) pairs the Copy-Item lines of pc/install_task.ps1 put into $app. Old form: literal lines and
    `foreach ($f in 'a.py', ...) { Copy-Item (Join-Path $src "analysis\\$f") $app }`."""
    pairs = set()
    for m in re.finditer(r"Copy-Item \(Join-Path \$src '([^']+)'\)\s+\$app\b", text):
        s = m.group(1).replace("\\", "/")
        pairs.add((s, s.rsplit("/", 1)[-1]))
    for m in re.finditer(r"foreach \(\$f in ([^)]*)\) \{ Copy-Item \(Join-Path \$src \"([a-z]+)\\\$f\"\) \$app", text):
        for f in re.findall(r"'([^']+)'", m.group(1)):
            pairs.add((m.group(2) + "/" + f, f))
    return pairs


class NoPowerShell(Exception):
    """No pwsh / powershell on PATH (WSL, Linux): the effective installer pairs cannot be evaluated there."""


def ps1_pairs_manifest(text, shell=None):
    """New form: the block between the plugin-manifest markers is run in PowerShell with Copy-Item replaced by a recorder."""
    m = re.search(r"# >>> plugin-manifest files\n(.*?)# <<< plugin-manifest files", text, re.S)
    if not m:
        return set()
    shell = shell or shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise NoPowerShell("no PowerShell to evaluate the manifest block of install_task.ps1")
    script = (
        "$src = '" + BMA.replace("'", "''") + "'\n$app = 'APPDIR'\n$global:rec = @()\n"
        "function Copy-Item { param($Path, $Destination, [switch]$Force) $global:rec += ((($Path.Substring($src.Length + 1)) -replace '\\\\','/') + '|' + (Split-Path -Leaf $Path)) }\n"
        + m.group(1)
        + "\n$global:rec | ForEach-Object { $_ }\n"
    )
    p = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise SystemExit("manifest block failed in PowerShell: " + p.stderr)
    return {tuple(l.strip().split("|", 1)) for l in p.stdout.splitlines() if "|" in l}


def ps1_pairs(shell=None):
    with open(os.path.join(BMA, "pc", "install_task.ps1"), encoding="utf-8") as f:
        text = f.read()
    return ps1_pairs_text(text) | ps1_pairs_manifest(text, shell)


def docker_pairs_text(text):
    text = lf(text).replace("\\\n", " ")
    pairs = set()
    for m in re.finditer(r"^COPY\s+(.+?)\s+(/\S+/)\s*$", text, re.M):
        for s in m.group(1).split():
            pairs.add((s, m.group(2) + s.rsplit("/", 1)[-1]))
    return pairs


def docker_pairs():
    with open(os.path.join(BMA, "docker", "analyzer.Dockerfile"), encoding="utf-8") as f:
        return docker_pairs_text(f.read())
