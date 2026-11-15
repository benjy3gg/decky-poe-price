"""Update the Awakened PoE Trade engine on the Deck from APT's GitHub releases.

The plugin ships an engine build (engine/dist) and a build kit (engine/kit: build.mjs, the stand-ins,
node_modules with esbuild). An update:
  1. downloads the release's source tarball from GitHub (codeload),
  2. extracts only what the engine needs (renderer/src, the English data, LICENSE),
  3. rebuilds with the plugin's Node.js and esbuild into a temporary folder,
  4. smoke-tests the new engine on a sample item, and
  5. swaps it into the runtime folder, keeping the previous build as dist.old.
Nothing is replaced unless every step worked.
"""
import json
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import time
import urllib.request

REPO = "SnosMe/awakened-poe-trade"
USER_AGENT = "decky-poe-price (Steam Deck Decky Loader plugin)"
KEEP = re.compile(r"^(renderer/src/.*|renderer/public/data/en/.*|renderer/public/data/(item-drop|patrons)\.json|LICENSE)$")


def _ssl():
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl())


def read_version(dist_dir):
    try:
        with open(os.path.join(dist_dir, "version.json")) as f:
            return json.load(f)
    except Exception:
        return None


def version_key(tag):
    """'v3.29.108' -> (3, 29, 108); anything else sorts lowest."""
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums) if re.match(r"^v?\d+(\.\d+)*$", tag or "") else ()


def latest_release():
    with _get(f"https://api.github.com/repos/{REPO}/releases/latest") as r:
        d = json.load(r)
    return {"tag": d["tag_name"], "published": d.get("published_at"), "url": d.get("html_url")}


def _download(tag, dest, progress):
    url = f"https://codeload.github.com/{REPO}/tar.gz/refs/tags/{tag}"
    done, last = 0, 0.0
    with _get(url, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if time.monotonic() - last > 0.5:
                progress(f"Downloading APT {tag}: {done / 1e6:.1f} MB")
                last = time.monotonic()
    progress(f"Downloaded APT {tag}: {done / 1e6:.1f} MB")


def _extract(archive, dest, progress):
    n = 0
    with tarfile.open(archive, "r:gz") as tar:
        for m in tar:
            parts = m.name.split("/", 1)  # "awakened-poe-trade-3.29.108/renderer/..."
            if len(parts) < 2 or not KEEP.match(parts[1]) or not (m.isfile() or m.isdir()):
                continue
            rel = os.path.normpath(parts[1])
            if rel.startswith("..") or os.path.isabs(rel):
                continue
            target = os.path.join(dest, rel)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    if not os.path.isdir(os.path.join(dest, "renderer/src")):
        raise RuntimeError("the release has no renderer/src: APT's layout changed")
    progress(f"Extracted {n} files")


def _make_executable(kit_dir):
    for rel in ("node_modules/@esbuild/linux-x64/bin/esbuild", "node_modules/esbuild/bin/esbuild"):
        p = os.path.join(kit_dir, rel)
        if os.path.isfile(p):
            os.chmod(p, 0o755)


def _run(args, env, cwd, timeout):
    r = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-600:] or f"exit {r.returncode}")
    return r.stdout


def update(tag, kit_dir, node, runtime_dir, env, progress):
    """Build the engine for APT release `tag` into runtime_dir/dist. Returns the new version.json."""
    work = os.path.join(runtime_dir, "update-work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        archive = os.path.join(work, "apt.tar.gz")
        _download(tag, archive, progress)
        src = os.path.join(work, "apt")
        _extract(archive, src, progress)
        os.remove(archive)

        progress("Building the engine…")
        _make_executable(kit_dir)
        out = os.path.join(work, "dist")
        benv = dict(env or os.environ)
        benv.update(APT_DIR=src, ENGINE_OUT=out, APT_VERSION=tag,
                    ESBUILD_BINARY_PATH=os.path.join(kit_dir, "node_modules/@esbuild/linux-x64/bin/esbuild"))
        _run([node, os.path.join(kit_dir, "build.mjs")], benv, kit_dir, 300)

        progress("Testing the new engine…")
        test = json.loads(_run([node, os.path.join(out, "engine.mjs"), "check",
                                os.path.join(kit_dir, "sample-item.txt"), "Standard"], benv, kit_dir, 60))
        if not test.get("presets") or not any(p.get("stats") for p in test["presets"]):
            raise RuntimeError("the new engine found no filters for the sample item")

        final = os.path.join(runtime_dir, "dist")
        old = os.path.join(runtime_dir, "dist.old")
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(final):
            os.rename(final, old)
        os.rename(out, final)
        progress(f"Updated to APT {tag}")
        return read_version(final)
    finally:
        shutil.rmtree(work, ignore_errors=True)
