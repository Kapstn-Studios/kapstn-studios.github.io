#!/usr/bin/env python3
"""Tell IndexNow search engines (Bing, Yandex and others) which pages changed.

  python3 .github/scripts/indexnow.py --changed BEFORE AFTER [--dry-run]
  python3 .github/scripts/indexnow.py --all [--dry-run]

--changed maps the files changed between two commits to page URLs.
--all sends every page in sitemap.xml plus llms.txt.
Only URLs listed in sitemap.xml (before or after the change) or /llms.txt are
sent. Before sending, it waits until GitHub Pages serves the new content and
the key file, so search engines never fetch a stale page or a missing key.
Run it from the root of a clone of the site repo.
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SITE = "https://kapstn-studios.github.io"
HOST = "kapstn-studios.github.io"
ENDPOINT = "https://api.indexnow.org/indexnow"
EXTRA_URLS = {SITE + "/llms.txt"}
WAIT_SECONDS = 600
POLL_SECONDS = 15


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def git_ok(*args):
    return subprocess.run(["git", *args], capture_output=True).returncode == 0


def find_key():
    """The key file is <key>.txt at the root, containing exactly <key>."""
    for name in git("ls-files").decode().split("\n"):
        m = re.fullmatch(r"([A-Za-z0-9-]{8,128})\.txt", name)
        if m and open(name, encoding="utf-8").read().strip() == m.group(1):
            return m.group(1)
    sys.exit("No IndexNow key file found at the repo root")


def sitemap_urls(rev):
    if not git_ok("cat-file", "-e", f"{rev}:sitemap.xml"):
        return set()
    xml = git("show", f"{rev}:sitemap.xml").decode("utf-8")
    return set(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml))


def url_for(path):
    if path == "index.html":
        return SITE + "/"
    if path.endswith("/index.html"):
        return SITE + "/" + path[: -len("index.html")]
    if path.endswith(".html") or path == "llms.txt":
        return SITE + "/" + path
    return None


def path_for(url):
    rel = url[len(SITE) + 1:]
    return rel + "index.html" if rel == "" or rel.endswith("/") else rel


def changed(before, after):
    """Return {url: path} for pages changed between two commits."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}|HEAD(~\d+)?", before) or set(before) == {"0"} \
            or not git_ok("cat-file", "-e", before + "^{commit}"):
        print(f"No usable base commit ({before}); sending every page")
        return everything(after)
    allowed = sitemap_urls(before) | sitemap_urls(after) | EXTRA_URLS
    out = {}
    for path in git("diff", "--name-only", before, after).decode().split("\n"):
        url = url_for(path) if path else None
        if url and url in allowed:
            out[url] = path
    return out


def everything(rev):
    return {u: path_for(u) for u in sorted(sitemap_urls(rev) | EXTRA_URLS)}


def fetch(url):
    """(status, body) of the live URL, bypassing caches."""
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(url + sep + "indexnow=" + uuid.uuid4().hex,
                                 headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, TimeoutError):
        return None, b""


def expected_versions(path, after, fetched):
    """File contents that count as deployed: the version at AFTER, or any newer
    version on origin/main (a later push may already be live)."""
    revs = [after]
    if fetched:
        revs += git("rev-list", f"{after}..FETCH_HEAD", "--", path).decode().split()
    versions = set()
    for rev in revs:
        if git_ok("cat-file", "-e", f"{rev}:{path}"):
            versions.add(git("show", f"{rev}:{path}"))
        else:
            versions.add(None)  # deleted at that commit: expect 404
    return versions


def wait_until_live(pages, key, after):
    checks = dict(pages)
    checks[f"{SITE}/{key}.txt"] = f"{key}.txt"
    deadline = time.time() + WAIT_SECONDS
    while True:
        pending = []
        fetched = git_ok("fetch", "-q", "origin", "main")
        for url, path in checks.items():
            status, body = fetch(url)
            versions = expected_versions(path, after, fetched)
            live = (status == 200 and body in versions) or (status == 404 and None in versions)
            if not live:
                pending.append(url)
        if not pending:
            return
        if time.time() > deadline:
            sys.exit("GitHub Pages still serves old content for: " + ", ".join(pending))
        print(f"Waiting for GitHub Pages: {len(pending)} URL(s) not live yet")
        time.sleep(POLL_SECONDS)


def submit(urls, key):
    payload = {"host": HOST, "key": key, "keyLocation": f"{SITE}/{key}.txt", "urlList": urls}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    print(f"IndexNow answered HTTP {status} {body.decode('utf-8', 'replace')[:300]}".rstrip())
    # 200 = received; 202 = received, key check pending. Anything else is a failure.
    if status not in (200, 202):
        sys.exit(1)


def main(argv):
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if args[:1] == ["--all"]:
        after = "HEAD"
        pages = everything(after)
    elif args[:1] == ["--changed"] and len(args) == 3:
        after = args[2]
        pages = changed(args[1], after)
    else:
        sys.exit(__doc__)
    urls = sorted(pages)
    if not urls:
        print("No page changes to report")
        return
    print("Pages to report:\n  " + "\n  ".join(urls))
    if dry:
        return
    key = find_key()
    wait_until_live(pages, key, git("rev-parse", after).decode().strip())
    submit(urls, key)


if __name__ == "__main__":
    main(sys.argv[1:])
