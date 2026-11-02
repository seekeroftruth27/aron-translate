"""Screenshots for the quick start guide (QUICKSTART.pdf at the repo root).

Drives a headless Edge over the DevTools protocol against a running server
(default http://localhost:5001), in light mode with demo caption rows, and
saves PNGs beside this script. Then build.sh renders quick-start.html to the PDF.

    python docs/quickstart/shoot.py [http://localhost:5001]
"""
import base64, json, os, subprocess, sys, tempfile, time, urllib.request

from websockets.sync.client import connect

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
HERE = os.path.dirname(os.path.abspath(__file__))
URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5001"
PORT = 9333

ROWS = [
    ["Și Dumnezeu a zis: Să fie lumină! Și a fost lumină.", "And God said, “Let there be light,” and there was light."],
    ["Fraților, Domnul este păstorul meu, nu voi duce lipsă de nimic.", "Brothers and sisters, the Lord is my shepherd; I shall not want."],
]

BASE = r"""
(async () => {
  await new Promise(r => setTimeout(r, 2500));
  if (document.documentElement.dataset.theme !== 'light') document.getElementById('themeToggle').click();
  ROWS.forEach(([ro, en], i) => {
    addSrcRow(9000 + i); setSrcLine(9000 + i, ro, '', null, 'ro');
    const t = srcRows.get(9000 + i).tgtEl; t.className = 'line tgt'; t.textContent = en;
  });
  return 'ok';
})()
""".replace("ROWS", json.dumps(ROWS, ensure_ascii=False))

OPEN = r"""
(() => { if (document.getElementById('settings').hidden) document.getElementById('setBtn').click();
  return 'open'; })()
"""

# Speak on, so the Voice section and its light read as in use.
SPEAK = r"""
(() => { if (!ttsBox.checked) document.getElementById('speakBtn').click(); return 'speak'; })()
"""

# Where the overview's numbered pins go: each target's centre as a percentage
# of the overview shot (OVERVIEW below), printed for quick-start.html.
PINS = r"""
(() => { const [X, Y, W, H] = OVERVIEW; const at = s => { const b = document.querySelector(s).getBoundingClientRect();
    return [s, +((b.x + b.width / 2 - X) / W * 100).toFixed(1), +((b.y + b.height / 2 - Y) / H * 100).toFixed(1)]; };
  return ['#listen', '#langBtn', '#modeInd', '#speakBtn', '#project', '#clearWall', '#setBtn', '.lights',
          '#costBox', '#dlBtn', '#helpBtn', '.phead h2', '.phead h2 + h2'].map(at); })()
"""

TICK = r"""
(() => {
  modelSel.value = 'gpt-6.1-terra-fast'; modelSel.dispatchEvent(new Event('change'));
  routerBox.checked = true; routerBox.dispatchEvent(new Event('change'));
  return 'ticked';
})()
"""

EAR = r"""
(() => { const ear = [...earsBox.querySelectorAll('input')].find(i => i.value === 'gemini-live');
  if (ear && !ear.checked) { ear.checked = true; ear.dispatchEvent(new Event('change')); } return 'ear'; })()
"""


def post(path, body):
    req = urllib.request.Request(URL + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()


def main():
    prof = tempfile.mkdtemp(prefix="qs_edge_")
    edge = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={prof}", "--hide-scrollbars", "about:blank"])
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                break
            except OSError:
                time.sleep(0.2)
        ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        with connect(ws_url, max_size=None) as ws:
            n = [0]

            def cdp(method, **params):
                n[0] += 1
                ws.send(json.dumps({"id": n[0], "method": method, "params": params}))
                while True:
                    m = json.loads(ws.recv())
                    if m.get("id") == n[0]:
                        if "error" in m:
                            raise RuntimeError(m["error"])
                        return m.get("result", {})

            def js(expr):
                return cdp("Runtime.evaluate", expression=expr, awaitPromise=True,
                           returnByValue=True)["result"].get("value")

            def rect(sel):
                return js(f"(()=>{{const b=document.querySelector({json.dumps(sel)}).getBoundingClientRect();"
                          f"return [b.x,b.y,b.width,b.height]}})()")

            def shot(name, x, y, w, h):
                d = cdp("Page.captureScreenshot", format="png",
                        clip={"x": x, "y": y, "width": w, "height": h, "scale": 1})
                open(os.path.join(HERE, name), "wb").write(base64.b64decode(d["data"]))
                print("saved", name)

            def crop(name, sel, pad=6):
                js(f"document.querySelector({json.dumps(sel)}).scrollIntoView({{block:'start'}})")
                time.sleep(0.3)
                x, y, w, h = rect(sel)
                shot(name, x - pad, y - pad, w + 2 * pad, h + 2 * pad)

            # Tall, so the whole Settings drawer fits on screen.
            cdp("Emulation.setDeviceMetricsOverride", width=1440, height=1800, deviceScaleFactor=2, mobile=False)
            cdp("Page.enable")
            cdp("Page.navigate", url=URL)
            time.sleep(4)
            print(js(BASE))
            print(js(SPEAK))
            time.sleep(1)
            # First look, Settings closed: the lights, the ribbon and the transcript.
            overview = [80, 8, 1280, 470]
            shot("overview.png", *overview)
            print(json.dumps(js("const OVERVIEW=" + json.dumps(overview) + ";" + PINS)))
            crop("cost.png", "#costBox", 4)
            print(js(OPEN)); time.sleep(0.8)
            print(js(EAR)); time.sleep(0.5)
            crop("listen.png", "#sec-listen", 0)        # before the router greys Pace out
            crop("wall.png", "#sec-wall", 0)
            crop("voice.png", "#sec-voice", 0)
            print(js(TICK)); time.sleep(0.8)
            crop("translate.png", "#sec-translate", 0)

            # The projector window, fed two finished captions through /caption.
            post("/caption", {"action": "clear"})
            for i, (_, en) in enumerate(ROWS):
                post("/caption", {"id": 9100 + i, "text": en, "done": True})
            cdp("Emulation.setDeviceMetricsOverride", width=1280, height=400, deviceScaleFactor=2, mobile=False)
            cdp("Page.navigate", url=URL + "/projector")
            time.sleep(9)
            shot("projector.png", 0, 0, 1280, 400)
            post("/caption", {"action": "clear"})
    finally:
        edge.terminate()


if __name__ == "__main__":
    main()
