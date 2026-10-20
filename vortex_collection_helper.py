#!/usr/bin/env python3
"""
vortex_collection_helper.py

Automates the FREE-account (non-Premium) Vortex collection download loop.

Each mod of a collection normally costs you a browser round trip:

    Vortex:  "Download mod X - 15/N"  ->  [Download manually]
                        |
                        v  (Vortex would open your browser)
    Browser: mod page -> [Slow download] -> 5s wait -> nxm:// link
                        |
                        v  (Windows hands nxm:// to Vortex.exe)
    Vortex:  downloads + installs, then shows the next prompt

This script does that loop for you:

  * Vortex is driven through its own DevTools protocol (DOM), not by clicking pixels.
  * The browser is driven by Playwright with real (trusted) clicks.
  * Vortex's attempt to open YOUR browser is intercepted and dropped, so your
    own Edge/Chrome never opens. The URL is handed to this script instead.
  * One reusable tab, recreated every N downloads so memory does not creep.

REQUIREMENTS
------------
1. Vortex must be running with a debug port:

     "<your Vortex folder>\\Vortex.exe" --remote-debugging-port=9222

   If Vortex is closed, this script can start it (see --restart-vortex to also
   handle the case where Vortex is already running WITHOUT the port).

2. Python packages: websocket-client, playwright
   pip install websocket-client playwright

3. First run: a browser window opens. Log in to Nexus Mods in it. The session
   is stored in the profile directory, so it is a one-time step.

USAGE
-----
    python vortex_collection_helper.py
    python vortex_collection_helper.py --once                    # test one mod
    python vortex_collection_helper.py --on-failure skip         # unattended
    python vortex_collection_helper.py --recycle-every 3
    python vortex_collection_helper.py --profile "C:\\path\\to\\profile"

Already logged in elsewhere? Point --profile at an existing Edge profile dir
(close that browser first - a profile can only be used by one process).

Stop with Ctrl+C. Vortex keeps running; the browser closes.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    sys.exit("Missing dependency: pip install websocket-client")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("Missing dependency: pip install playwright")


# --------------------------------------------------------------------------
# JavaScript that runs INSIDE Vortex's renderer
# --------------------------------------------------------------------------

# Intercept the function Vortex uses to open URLs. We keep the URL and return
# a resolved promise WITHOUT calling the original, so your own browser is never
# launched. Re-running is safe.
def find_vortex_exe():
    """Locate Vortex.exe without assuming one machine's install folder.

    The nxm:// protocol handler is the authoritative answer - it is the exact
    executable Windows launches when a download link is handed over.
    """
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Classes\nxm\shell\open\command") as key:
                    cmd = winreg.QueryValueEx(key, "")[0]
            except OSError:
                continue
            m = (re.search(r'"([^"]*Vortex\.exe)"', cmd, re.I)
                 or re.search(r"(\S*Vortex\.exe)", cmd, re.I))
            if m and os.path.exists(m.group(1)):
                return m.group(1)
    for cand in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Vortex", "Vortex.exe"),
                 r"C:\Program Files\Vortex\Vortex.exe"):
        if os.path.exists(cand):
            return cand
    return None


JS_HOOK = r"""
(() => {
  if (window.__vch_installed) return "already";
  const api = window.api;
  if (!api || !api.shell || typeof api.shell.openUrl !== "function") return "no-api";
  window.__vch_urls = [];
  window.__vch_origOpenUrl = api.shell.openUrl.bind(api.shell);
  api.shell.openUrl = function (u) {
    try { window.__vch_urls.push(String(u)); } catch (e) {}
    return Promise.resolve();
  };
  window.__vch_installed = true;
  return "installed";
})()
"""

JS_UNHOOK = r"""
(() => {
  if (!window.__vch_installed) return "not-installed";
  try { window.api.shell.openUrl = window.__vch_origOpenUrl; } catch (e) {}
  window.__vch_installed = false;
  return "restored";
})()
"""

# Read the current per-mod prompt (the Free/Premium "Download mod ..." dialog).
JS_PROMPT = r"""
(() => {
  const vis = el => el.offsetParent !== null;
  const btns = [...document.querySelectorAll('button')].filter(vis);
  const dm = btns.find(b => /^download manually$/i.test((b.innerText || '').trim()));
  const dlg = [...document.querySelectorAll('[role=dialog],.modal')]
      .find(d => /download mod/i.test(d.innerText || ''));
  const text = dlg ? dlg.innerText.replace(/\s+/g, ' ') : '';
  const m = text.match(/(\d+)\s*\/\s*(\d+)/);
  const n = text.match(/download mod\s+(.+?)\s+by\s+/i);
  return JSON.stringify({
    hasPrompt: !!dm,
    index: m ? parseInt(m[1], 10) : null,
    total: m ? parseInt(m[2], 10) : null,
    modName: n ? n[1] : null,
    text: text.slice(0, 240)
  });
})()
"""

# Click "Download manually" - a plain DOM click. Vortex's own handler runs and
# calls openUrl, which our hook captures.
JS_CLICK_DOWNLOAD_MANUALLY = r"""
(() => {
  const b = [...document.querySelectorAll('button')]
      .find(e => e.offsetParent !== null &&
                 /^download manually$/i.test((e.innerText || '').trim()));
  if (!b) return "none";
  b.click();
  return "clicked";
})()
"""

# Vortex blocks the whole install queue on a mod installer (FOMOD) dialog
# until a human clicks Finish. Scoped to the installer footer so it can never
# hit an unrelated dialog button.
JS_CLICK_INSTALLER = r"""
(() => {
  const vis = el => el.offsetParent !== null;
  for (const nav of [...document.querySelectorAll('.fomod-nav-buttons')].filter(vis)) {
    const btns = [...nav.querySelectorAll('button')].filter(vis);
    const fin = btns.find(b => /^(finish|install)$/i.test((b.innerText || '').trim()));
    const nxt = btns.find(b => /^next$/i.test((b.innerText || '').trim()));
    const t = fin || nxt;
    if (t) { t.click(); return "clicked:" + t.innerText.trim(); }
  }
  return "none";
})()
"""

JS_CLICK_SKIP_MOD = r"""
(() => {
  const vis = el => el.offsetParent !== null;
  const b = [...document.querySelectorAll('button')].filter(vis)
      .find(e => /^skip mod$/i.test((e.innerText || '').trim()));
  if (!b) return "none";
  b.click();
  return "clicked";
})()
"""

# Drain captured URLs.
JS_TAKE_URLS = r"""
(() => {
  const u = window.__vch_urls || [];
  window.__vch_urls = [];
  return JSON.stringify(u);
})()
"""


# --------------------------------------------------------------------------
# Vortex side
# --------------------------------------------------------------------------

class VortexBridge:
    """Talks to Vortex's renderer over the Chrome DevTools protocol."""

    def __init__(self, port):
        self.port = port
        self.ws = None
        self._id = 0

    def connect(self):
        url = "http://127.0.0.1:%d/json/list" % self.port
        with urllib.request.urlopen(url, timeout=5) as fh:
            targets = json.load(fh)
        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("Vortex is reachable but exposes no page target")
        self.ws = websocket.create_connection(
            pages[0]["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        return self

    def js(self, expr, timeout=30):
        """Evaluate an expression in Vortex and return its value."""
        if self.ws is None:
            self.connect()
        self._id += 1
        mid = self._id
        try:
            self.ws.settimeout(timeout)
            self.ws.send(json.dumps({
                "id": mid, "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True,
                           "awaitPromise": True}}))
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("id") == mid:
                    res = msg.get("result", {})
                    if "exceptionDetails" in res:
                        raise RuntimeError(res["exceptionDetails"]
                                           .get("exception", {})
                                           .get("description", "JS error"))
                    return res.get("result", {}).get("value")
        except (websocket.WebSocketException, OSError):
            # Renderer restarted or socket died - reconnect once and retry.
            self.ws = None
            self.connect()
            return self.js(expr, timeout)

    def ensure_hook(self):
        return self.js(JS_HOOK)

    def prompt(self):
        raw = self.js(JS_PROMPT)
        return json.loads(raw) if raw else {"hasPrompt": False}

    def click_installer(self):
        """Click Finish/Install/Next in a FOMOD dialog. "none" if none is up."""
        return self.js(JS_CLICK_INSTALLER)

    def click_skip_mod(self):
        """Press 'Skip mod' in the Free/Premium prompt. "none" if absent."""
        return self.js(JS_CLICK_SKIP_MOD)

    def click_download_manually(self):
        return self.js(JS_CLICK_DOWNLOAD_MANUALLY)

    def take_urls(self):
        raw = self.js(JS_TAKE_URLS)
        return json.loads(raw) if raw else []


# --------------------------------------------------------------------------
# Browser side
# --------------------------------------------------------------------------

class BrowserSide:
    """Owns one tab, reused and recycled."""

    def __init__(self, profile, channel):
        self.profile = profile
        self.channel = channel
        self._pw = None
        self.ctx = None
        self.page = None

    def start(self):
        self._pw = sync_playwright().start()
        os.makedirs(self.profile, exist_ok=True)
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(
                self.profile,
                channel=self.channel,
                headless=False,
                no_viewport=True,
                # Playwright appends --no-sandbox unless you ask for the sandbox,
                # which makes Edge show a 'command line flag' warning bar.
                chromium_sandbox=True,
                args=[
                    # Keeps navigator.webdriver false; without it the browser
                    # announces itself and Nexus's login captcha fails.
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=Translate",
                ],
            )
        except Exception as exc:
            msg = str(exc)
            if "existing browser session" in msg or "already in use" in msg:
                raise RuntimeError(
                    "the browser profile\n    %s\nis already open - another run "
                    "or browser window is using it.\nClose it and start again."
                    % self.profile)
            raise
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(30000)

    def recycle(self):
        """Swap in a fresh tab (memory hygiene).

        The replacement is opened first: closing the only tab of a persistent
        context can take the whole browser down with it.
        """
        try:
            fresh = self.ctx.new_page()
        except Exception:
            self._revive()
            return
        old = self.page
        self.page = fresh
        self.page.set_default_timeout(30000)
        try:
            old.close()
        except Exception:
            pass

    def _revive(self, log=None):
        """Make sure there is a usable tab; rebuild the browser if needed."""
        say = log or (lambda *_: None)
        if self.page is not None:
            try:
                if not self.page.is_closed():
                    return
            except Exception:
                pass
        try:
            pages = [p for p in self.ctx.pages if not p.is_closed()]
            if pages:
                self.page = pages[-1]
                self.page.set_default_timeout(30000)
                say("browser: reattached to an open tab")
                return
            self.page = self.ctx.new_page()
            self.page.set_default_timeout(30000)
            say("browser: opened a fresh tab")
            return
        except Exception:
            pass
        say("browser: window/tab was lost - restarting the browser")
        self.close()
        time.sleep(2)
        self.start()
        say("browser: restarted")

    def _login_state(self):
        """True = signed in, False = signed out, None = undecided.

        Nexus leaves a signed-out visitor on /users/myaccount instead of
        always redirecting, so the URL proves nothing. The header does:
        signed out shows a "Log in" link, signed in shows an avatar.
        """
        try:
            if self.page.locator('input[type="password"]').count():
                return False
            if self.page.locator('a[href*="sign_in"]').count():
                return False
            body = self.page.inner_text("body")
            if not body.strip():
                return None
            if "Log in" in body:
                return False
            return True
        except Exception:
            return None

    def ensure_login(self, log):
        self._revive(log)
        self.page.goto("https://www.nexusmods.com/users/myaccount",
                       wait_until="domcontentloaded")
        # The redirect to the sign-in page happens after domcontentloaded, so
        # wait for the page to actually settle before deciding.
        state = None
        for _ in range(40):
            state = self._login_state()
            if state is not None:
                break
            time.sleep(0.5)
        if state is False:
            log("")
            log("  *** Log in to Nexus Mods in the browser window that just opened. ***")
            log("  You only have to do this once per profile; solve any captcha normally.")
            log("  The script carries on by itself the moment you are in.")
            while self._login_state() is not True:
                try:
                    gone = self.page is None or self.page.is_closed()
                except Exception:
                    gone = True
                if gone:
                    # Somebody closed the window. Waiting here would be waiting
                    # for a login that can no longer happen - reopen instead.
                    log("  the browser window went away - reopening it")
                    self._revive(log)
                    try:
                        self.page.goto("https://www.nexusmods.com/users/myaccount",
                                       wait_until="domcontentloaded")
                    except Exception:
                        pass
                time.sleep(3)
        elif state is None:
            log("  could not tell whether you are logged in - check the browser window")
            log("  Press Enter here when you can see your account page.")
            input()
        log("browser: logged in")

    def _page_text(self):
        try:
            return self.page.inner_text("body")[:4000]
        except Exception:
            return ""

    def _goto(self, url, log):
        """Navigate to a mod page. Returns "ok" | "aborted" | "fail".

        Never raises: one navigation hiccup must not kill a long run.
        ERR_ABORTED is expected here - Chromium reports it when the page hands
        off to an external protocol or starts a download instead of rendering.
        """
        for attempt in (1, 2):
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                return "ok"
            except Exception as exc:
                first = str(exc).splitlines()[0]
                if "ERR_ABORTED" in first:
                    log("  navigation cancelled by the browser (handoff/download)")
                    return "aborted"
                if attempt == 1:
                    log("  navigation failed (%s) - retrying once" % first[:70])
                    time.sleep(3)
                    continue
                log("  navigation failed twice (%s)" % first[:70])
                return "fail"
        return "fail"

    def do_download(self, url, log):
        """Open the mod page and click the free-tier 'Slow download' button.

        Returns: "ok" | "rate-limited" | "challenge" | "no-button" | "login"
        """
        self._revive(log)
        nav = self._goto(url, log)
        if nav == "fail":
            return "nav-failed"
        time.sleep(2.5)

        if "sign_in" in self.page.url:
            return "login"

        title = (self.page.title() or "")
        if "Just a moment" in title or "Attention Required" in title:
            return "challenge"

        try:
            btn = self.page.locator('button:has-text("Slow download")').first
            btn.wait_for(state="visible", timeout=25000 if nav == "ok" else 9000)
        except PWTimeout:
            if nav == "aborted":
                # Chromium cancels a navigation both when the page hands the mod
                # to the nxm:// handler AND when the page never rendered. The
                # main loop checks Vortex's log to find out which.
                log("  page cancelled the navigation - checking whether Vortex got it")
                return "uncertain"
            text = self._page_text()
            if "download limit" in text.lower() or "try again in" in text.lower():
                return "rate-limited"
            if "Just a moment" in (self.page.title() or ""):
                return "challenge"
            return "no-button"

        btn.click()          # real, trusted click - required for the nxm:// launch
        time.sleep(2)

        text = self._page_text()
        if "download limit" in text.lower() or "try again in" in text.lower():
            return "rate-limited"
        return "ok"

    def close(self):
        try:
            if self.ctx is not None:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_logger(path):
    fh = open(path, "a", encoding="utf-8")

    def log(msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        fh.write(line + "\n")
        fh.flush()

    return log


def port_open(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port,
                                    timeout=2):
            return True
    except Exception:
        return False


def vortex_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Vortex.exe"],
                             capture_output=True, text=True, timeout=10).stdout
        return "Vortex.exe" in out
    except Exception:
        return False


def wait_for_prompt(bridge, log, idle_exit, started_already):
    """Wait until a per-mod prompt exists. Returns prompt dict or None."""
    waited = 0
    announced = False
    settle = None
    while True:
        try:
            p = bridge.prompt()
        except Exception as exc:
            log("vortex: reconnecting (%s)" % str(exc)[:80])
            time.sleep(3)
            continue
        if p.get("hasPrompt"):
            # The dialog shows before it is filled in ("Loading..."), and
            # reading it then yields a bogus mod number. Give it a moment.
            if p.get("index") is None:
                if settle is None:
                    settle = time.time()
                elif time.time() - settle >= 20:
                    log("  prompt never filled in - using it as-is")
                    return p
                time.sleep(1)
                continue
            return p
        settle = None
        if not announced:
            log("waiting for Vortex's download prompt...")
            announced = True
        time.sleep(2)
        waited += 2
        if started_already and idle_exit and waited >= idle_exit:
            return None


VORTEX_LOG = os.path.join(os.environ.get("APPDATA", ""), "Vortex", "vortex.log")


def mod_key(url):
    """(modId, fileId) from an nxm:// or nexusmods URL - the mod's real identity.

    The dialog index is useless for this: it flickers, and it can repeat.
    """
    url = url or ""
    m = re.search(r"/mods/(\d+)/files/(\d+)", url)          # nxm:// form
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"/mods/(\d+)\?", url)                    # web page form
    f = re.search(r"[?&]file_id=(\d+)", url)
    if m and f:
        return (int(m.group(1)), int(f.group(1)))
    return None


def log_mark():
    """(bytes, created) for vortex.log - enough to notice a rewrite.

    Size catches truncation. Creation time catches replacement by a fresh
    file, which may well be larger than the offset we saved from the old one.
    """
    try:
        st = os.stat(VORTEX_LOG)
        return (st.st_size, st.st_ctime)
    except OSError:
        return (0, 0.0)


def saw_handoff(mark, key):
    """True once Vortex's log shows it received this mod+file since `mark`.

    A browser handoff always lands in vortex.log as
        [RENDERER] handling url {"url":"nxm://.../mods/<id>/files/<fid>?key=..."}
    Unlike the dialog counter, that cannot flicker: the download is real.

    Vortex rewrites vortex.log when it reaches ~5 MB. If the file shrank or
    was replaced since `mark` was taken, the mark is meaningless and the whole
    file is searched - otherwise the run would sit out the download timeout.
    """
    if not key:
        return False
    size, created = mark
    try:
        st = os.stat(VORTEX_LOG)
    except OSError:
        return False
    if st.st_ctime != created or st.st_size < size:
        size = 0
    try:
        with open(VORTEX_LOG, "rb") as fh:
            fh.seek(size)
            chunk = fh.read().decode("utf-8", "replace")
    except OSError:
        return False
    want = "/mods/%d/files/%d" % key
    return any("handling url" in line and want in line for line in chunk.splitlines())


def wait_handoff(mark, key, seconds, log):
    for _ in range(max(1, seconds // 3)):
        if saw_handoff(mark, key):
            log("  Vortex received the download")
            return True
        time.sleep(3)
    return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Automate the free-tier Vortex collection download loop.")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--vortex-exe", default=None,
                    help="full path to Vortex.exe (default: found via the nxm:// handler)")
    ap.add_argument("--profile",
                    default=os.path.join(os.environ.get("LOCALAPPDATA", here),
                                         "vortex-collection-helper", "profile"))
    ap.add_argument("--channel", default="msedge", help="msedge (default) or chromium")
    ap.add_argument("--recycle-every", type=int, default=3,
                    help="close and reopen the browser tab every N downloads")
    ap.add_argument("--idle-exit", type=int, default=180,
                    help="seconds without a prompt before declaring the run finished")
    ap.add_argument("--download-timeout", type=int, default=1800,
                    help="max seconds to wait for Vortex to download+install one mod")
    ap.add_argument("--on-failure", choices=["pause", "skip"], default="pause",
                    help="pause and ask (default), or keep going")
    ap.add_argument("--once", action="store_true", help="process a single mod, then stop")
    ap.add_argument("--restart-vortex", action="store_true",
                    help="if Vortex is running without the debug port, kill and relaunch it")
    args = ap.parse_args()

    log = make_logger(os.path.join(here, "vortex_collection_helper.log"))
    log("=" * 68)
    log("vortex collection helper starting")

    if not args.vortex_exe:
        args.vortex_exe = find_vortex_exe()
    if not args.vortex_exe:
        log("could not find Vortex.exe - pass --vortex-exe with its full path")
        return 1

    # ---- make sure Vortex is reachable -----------------------------------
    if not port_open(args.port):
        if vortex_running():
            if not args.restart_vortex:
                log("Vortex is running WITHOUT the debug port.")
                log("Close Vortex and rerun, or rerun with --restart-vortex")
                log("(that will kill the running Vortex).")
                return 1
            log("restarting Vortex with the debug port...")
            subprocess.run(["taskkill", "/IM", "Vortex.exe", "/F"],
                           capture_output=True)
            time.sleep(5)
        log("starting Vortex with --remote-debugging-port=%d" % args.port)
        subprocess.Popen([args.vortex_exe,
                          "--remote-debugging-port=%d" % args.port])
        for _ in range(40):
            time.sleep(1)
            if port_open(args.port):
                break
        else:
            log("Vortex did not come up with the debug port. Start it manually:")
            log('   "%s" --remote-debugging-port=%d' % (args.vortex_exe, args.port))
            return 1
    log("vortex: debug port %d reachable" % args.port)

    bridge = VortexBridge(args.port)
    bridge.connect()
    # A Vortex that was just launched has not exposed its renderer API yet.
    # Retrying here keeps the hook honest instead of quietly relying on the
    # per-mod retry and logging "no-api" at startup.
    hook = bridge.ensure_hook()
    for _ in range(20):
        if hook != "no-api":
            break
        time.sleep(1)
        hook = bridge.ensure_hook()
    log("vortex: hooked openUrl -> %s" % hook)

    browser = BrowserSide(args.profile, args.channel)
    log("browser: profile %s" % args.profile)
    browser.start()

    processed = skipped = failed = 0
    done_keys = set()
    dup_tries = {}
    same_fail = 0
    last_fail = None
    since_recycle = 0
    started = time.time()

    try:
        browser.ensure_login(log)

        name = "(unknown mod)"
        idx = None
        while True:
            try:
                prompt = wait_for_prompt(bridge, log, args.idle_exit,
                                         started_already=processed > 0)
                if prompt is None:
                    log("no prompt for %ds - run looks finished" % args.idle_exit)
                    break

                idx = prompt.get("index")
                total = prompt.get("total")
                name = prompt.get("modName") or "(unknown mod)"
                log("mod %s/%s: %s" % (idx, total, name))

                # 1) ask Vortex what it wants
                bridge.ensure_hook()
                bridge.take_urls()                      # clear stale entries
                if bridge.click_download_manually() != "clicked":
                    log("  could not click 'Download manually' - retrying next cycle")
                    time.sleep(3)
                    continue

                url = None
                for _ in range(15):
                    urls = bridge.take_urls()
                    if urls:
                        url = urls[-1]
                        break
                    time.sleep(1)
                if not url:
                    log("  Vortex gave no URL for this mod")
                    failed += 1
                    if not handle_failure(args, log, name):
                        break
                    skipped += 1
                    continue

                key = mod_key(url)
                log_off = log_mark()
                if key and key in done_keys:
                    # This exact mod+file was already downloaded during this run.
                    # Vortex asked again because its counter repeated - taking it
                    # a second time installs a duplicate copy of the mod.
                    dup_tries[key] = dup_tries.get(key, 0) + 1
                    if dup_tries[key] <= 3:
                        log("  already handled %d/%d - not downloading it twice" % key)
                        what = bridge.click_skip_mod()
                        log("  %s" % ("pressed 'Skip mod'" if what == "clicked"
                                      else "could not press 'Skip mod'"))
                        if what == "clicked":
                            skipped += 1
                        time.sleep(2)
                        continue
                    log("  cannot get Vortex past %d/%d - taking it again" % key)

                # 2) do the browser round trip
                status = browser.do_download(url, log)

                if status == "uncertain":
                    # The page cancelled the navigation. Vortex's own log is the
                    # authority on whether the handoff actually happened.
                    status = ("ok" if wait_handoff(log_off, key, 30, log)
                              else "no-button")

                if status == "login":
                    log("  session expired - log in again in the browser window")
                    browser.ensure_login(log)
                    status = browser.do_download(url, log)

                if status in ("challenge", "rate-limited"):
                    wait_s = 300
                    log("  %s - waiting %ds before retrying" % (status, wait_s))
                    time.sleep(wait_s)
                    status = browser.do_download(url, log)

                if status != "ok":
                    log("  browser failed for this mod (%s)" % status)
                    log("  url: %s" % url)
                    failed += 1
                    same_fail = same_fail + 1 if idx == last_fail else 1
                    last_fail = idx
                    if same_fail >= 5:
                        # Nothing is advancing. Make Vortex move on instead of
                        # retrying the same mod forever.
                        what = bridge.click_skip_mod()
                        log("  %d attempts on mod %s - %s" % (
                            same_fail, idx,
                            "pressed 'Skip mod'" if what == "clicked"
                            else "could not press 'Skip mod', stopping"))
                        same_fail = 0
                        last_fail = None
                        if what != "clicked":
                            break
                        skipped += 1
                        time.sleep(2)
                        continue
                    if not handle_failure(args, log, name):
                        break
                    skipped += 1
                    continue

                # 3) wait for Vortex to receive, download, install, and advance
                t0 = time.time()
                ok = False
                next_report = 60
                absent_since = None
                nudge_at = 45
                moved_at = None
                while time.time() - t0 < args.download_timeout:
                    time.sleep(3)
                    elapsed = int(time.time() - t0)
                    if elapsed >= nudge_at:
                        # Vortex stalls the whole queue on a mod installer (FOMOD)
                        # dialog until a human clicks Finish. Click it for them.
                        what = bridge.click_installer()
                        if what != "none":
                            log("  mod installer dialog: %s" % what)
                            nudge_at = elapsed + 5
                        else:
                            nudge_at = elapsed + 20
                    try:
                        now = bridge.prompt()
                    except Exception:
                        continue
                    note = ""
                    if now.get("hasPrompt") and now.get("index") != idx:
                        # The counter flickers while the dialog reloads, and it can
                        # move before the download has even reached Vortex - that is
                        # exactly how one mod once got downloaded twice. Require
                        # proof from the log before believing it.
                        if key and not saw_handoff(log_off, key):
                            if moved_at is None:
                                moved_at = time.time()
                            elif time.time() - moved_at >= 120:
                                log("  Vortex moved on but its log never showed the"
                                    " handoff - assuming it landed")
                                ok = True
                                break
                            note = " (vortex moved on, no handoff in log yet)"
                        else:
                            ok = True
                            break
                    elif not now.get("hasPrompt"):
                        # The dialog can blink out mid-install. Only believe this mod
                        # is finished once the prompt has stayed gone for a while.
                        if absent_since is None:
                            absent_since = time.time()
                        elif time.time() - absent_since >= 20:
                            ok = True
                            break
                    else:
                        absent_since = None
                        moved_at = None
                    if elapsed >= next_report:
                        log("  still downloading/installing (%ds)%s" % (elapsed, note))
                        next_report += 60
                if ok:
                    processed += 1
                    since_recycle += 1
                    same_fail = 0
                    last_fail = None
                    if key:
                        done_keys.add(key)
                    log("  done in %ds (installed: %d)" % (int(time.time() - t0), processed))
                else:
                    failed += 1
                    log("  timed out waiting for Vortex after %ds" % args.download_timeout)
                    if not handle_failure(args, log, name):
                        break
                    continue

                if args.once:
                    log("--once: stopping after one mod")
                    break

                # 4) memory hygiene: fresh tab every N mods
                if since_recycle >= args.recycle_every:
                    log("  refreshing browser tab (every %d mods)" % args.recycle_every)
                    browser.recycle()
                    since_recycle = 0
            except Exception as exc:
                # one bad mod must never end the run
                first = str(exc).splitlines()[0] if str(exc) else repr(exc)
                log("  unexpected error: %s" % first[:140])
                failed += 1
                if not handle_failure(args, log, name):
                    break
                skipped += 1

        mins = (time.time() - started) / 60.0
        log("-" * 68)
        log("finished: %d installed, %d failed, %d skipped, %.1f min"
            % (processed, failed, skipped, mins))
    except KeyboardInterrupt:
        log("interrupted by user")
    finally:
        try:
            bridge.js(JS_UNHOOK)
            log("vortex: openUrl restored")
        except Exception:
            pass
        browser.close()
        log("browser closed")

    return 0


def handle_failure(args, log, name):
    """Returns True to keep going, False to stop."""
    if args.on_failure == "skip":
        log("  --on-failure skip: continuing (skipped: %s)" % name)
        return True
    log("  skipping this mod is a click away in Vortex ('Skip mod'),")
    log("  or press Enter here to keep going, 'q' to stop.")
    try:
        return input().strip().lower() not in ("q", "quit", "exit")
    except EOFError:
        return False


if __name__ == "__main__":
    sys.exit(main())
