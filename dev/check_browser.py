"""Check the browser side: launch, navigate, recycle the tab, close."""
import importlib.util, os

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(HERE, "..", "vortex_collection_helper.py")
spec = importlib.util.spec_from_file_location("vch", MOD)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

tmp = os.path.join(os.environ.get("TEMP", "."), "opencode", "vch-smoke-profile")
b = m.BrowserSide(tmp, "msedge")
print("starting browser (visible window)...")
b.start()
print("pages:", len(b.ctx.pages))
b.page.goto("https://www.nexusmods.com/games/skyrimspecialedition/collections",
            wait_until="domcontentloaded")
print("title:", (b.page.title() or "")[:70])
b.recycle()
print("recycled, pages:", len(b.ctx.pages))
b.close()
print("closed cleanly")
