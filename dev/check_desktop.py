"""Check desktop parking: launch the helper's browser, park it, come back."""
import importlib.util, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(HERE, "..", "vortex_collection_helper.py")
spec = importlib.util.spec_from_file_location("vch", MOD)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

try:
    import pyvda
    from pyvda import AppView, VirtualDesktop
except ImportError:
    sys.exit("Missing dependency: pip install pyvda")

tmp = os.path.join(os.environ.get("TEMP", "."), "opencode", "vch-desktop-profile")
before = len(pyvda.get_virtual_desktops())
print("desktops before :", before, "| you are on", VirtualDesktop.current().number)

b = m.BrowserSide(tmp, "msedge")
m._PARK["enabled"] = True
ok = False
try:
    print("starting the helper's browser...")
    b.start()
    b.page.goto("about:blank", wait_until="domcontentloaded")

    m.park_windows(tmp, print)
    wins, _vortex = m._find_own_windows(tmp, wait=5)
    here = VirtualDesktop.current().number
    nums = [AppView(h).desktop.number for h in wins]
    print("windows matched :", len(wins))
    print("their desktops  :", nums, "| you are on", here)
    print("desktops now    :", len(pyvda.get_virtual_desktops()))
    ok = bool(nums) and all(n != here for n in nums)
    print("RESULT          :", "PASS - browser is off your desktop"
          if ok else "FAIL - browser is still on your desktop")
finally:
    b.close()
    time.sleep(1.5)
    m.unpark_windows(print)
    after = len(pyvda.get_virtual_desktops())
    print("desktops after  :", after, "| you are on", VirtualDesktop.current().number)
    print("cleanup         :", "PASS - no leftover desktop" if after == before else "FAIL")

print("EXIT            :", "PASS" if ok and after == before else "FAIL")
