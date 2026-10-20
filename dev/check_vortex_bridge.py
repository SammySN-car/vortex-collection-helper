"""Check the Vortex side: attach, install the openUrl hook, read the prompt, restore."""
import importlib.util, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(HERE, "..", "vortex_collection_helper.py")
spec = importlib.util.spec_from_file_location("vch", MOD)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

print("port_open(9222):", m.port_open(9222))
print("vortex_running():", m.vortex_running())

b = m.VortexBridge(9222)
b.connect()
print("ensure_hook ->", b.ensure_hook())
print("prompt ->", json.dumps(b.prompt()))
print("take_urls ->", b.take_urls())
print("unhook ->", b.js(m.JS_UNHOOK))
