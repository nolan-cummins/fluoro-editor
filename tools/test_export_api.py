"""Export end-to-end over HTTP, against a RUNNING server.

    python3 tools/test_export_api.py [base_url]

The unit checks in test_export.py stub library/render and never touch the
wire; these do, and they assert the things only a real run can: that a
frame export is full resolution rather than the 1600px viewport render,
that the mp4 is decodable and holds one frame per input, and that a
cancelled job leaves nothing downloadable behind.
"""
import sys

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800"

import json, time, urllib.request, urllib.error, os, subprocess
B = "http://127.0.0.1:8800"
fails = []
def check(l, c, d=""):
    print(("PASS" if c else "FAIL"), l, "" if c else d)
    if not c: fails.append(l)
def post(u, b):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        B+u, json.dumps(b).encode(), {"Content-Type":"application/json"}, method="POST")))
def get(u): return json.load(urllib.request.urlopen(B+u))
def wait(timeout=240):
    t0=time.time()
    while time.time()-t0 < timeout:
        p = get("/api/export/progress")
        # finished, NOT cancelled: the cancelled flag is set by cancel() while the
        # worker is still unwinding, and it is that worker which deletes the
        # half-written mp4. Returning on the flag raced the cleanup and made a
        # cancelled video look downloadable.
        if p["finished"]: return p
        time.sleep(0.4)
    return get("/api/export/progress")

lib = get("/api/library")["items"]
ids = [i["id"] for i in lib if not i.get("error")][:6]
check("library has usable frames", len(ids) >= 4, str(len(ids)))

# --- single frame, PNG
post("/api/export/start", {"kind":"frame","format":"png","ids":[ids[0]],"name":"e2e-frame","filters":True,"params":{"compression":1}})
p = wait()
check("frame export succeeds", p["finished"] and not p["error"] and p["file"], f"{p.get('error')} {p.get('file')}")
fn = p["file"]
check("frame export is a .png", fn.endswith(".png"), fn)
raw = urllib.request.urlopen(f"{B}/api/export/download?name={fn}").read()
check("download returns real bytes", raw[:8] == b"\x89PNG\r\n\x1a\n", repr(raw[:8]))
# full resolution, not the 1600px viewport render
import io
try:
    from PIL import Image
    im = Image.open(io.BytesIO(raw)); dims = im.size
except Exception:
    dims = None
src = next(i for i in lib if i["id"] == ids[0])
check("frame export is FULL resolution, not the viewport downscale",
      dims == (src["w"], src["h"]), f"{dims} vs {(src['w'], src['h'])}")

# --- path traversal is refused at the download endpoint too
for bad in ["../../etc/passwd", "/etc/passwd", "..%2f..%2fetc%2fpasswd"]:
    try:
        r = urllib.request.urlopen(f"{B}/api/export/download?name={bad}")
        check(f"download refuses {bad!r}", False, "it served something")
    except urllib.error.HTTPError as e:
        check(f"download refuses {bad!r}", e.code in (400,404), f"HTTP {e.code}")

# --- series as a real video
post("/api/export/start", {"kind":"series","format":"video","ids":ids,"name":"e2e-vid",
                           "fps":6,"max_side":320,"preset":"compact","filters":True,"params":{}})
p = wait()
check("video export succeeds", p["finished"] and not p["error"] and p["file"], f"{p.get('error')}")
if p.get("file"):
    v = p["file"]
    check("video export is an .mp4", v.endswith(".mp4"), v)
    data = urllib.request.urlopen(f"{B}/api/export/download?name={v}").read()
    check("mp4 has an ftyp box", data[4:8] == b"ftyp", repr(data[4:12]))
    open("/tmp/e2e.mp4","wb").write(data)
    pr = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-count_frames",
                         "-show_entries","stream=nb_read_frames,width,height","-of","csv=p=0","/tmp/e2e.mp4"],
                        capture_output=True, text=True)
    parts = pr.stdout.strip().split(",")
    # csv order is width,height,nb_read_frames - frames LAST.
    check("the video really holds every frame", len(parts)==3 and parts[2]==str(len(ids)),
          f"{(pr.stdout+pr.stderr).strip()[:150]} (want {len(ids)} frames)")
    check("the video is at the requested size", len(parts)==3 and int(parts[1])<=320 and int(parts[2])<=320,
          pr.stdout.strip())

# --- series as a zip
post("/api/export/start", {"kind":"series","format":"png","ids":ids,"name":"e2e-zip",
                           "filters":True,"params":{"compression":6}})
p = wait()
check("zip export succeeds", p["finished"] and not p["error"] and p["file"], f"{p.get('error')}")
if p.get("file"):
    import zipfile, io as _io
    z = zipfile.ZipFile(_io.BytesIO(urllib.request.urlopen(f"{B}/api/export/download?name={p['file']}").read()))
    check("zip holds one entry per frame", len(z.namelist())==len(ids), str(z.namelist()))
    # NOT "IMG_": Canon writes _MG_0018.JPG when the original IMG_ name was
    # already taken, and that is what this library actually contains. The check
    # asserts the entry names come from the LIBRARY, whatever it calls them.
    names = {i["name"] for i in lib}
    check("zip names entries after the frames",
          all(any(k.endswith(n[:-4]) for k in names) for n in z.namelist()),
          str(z.namelist()))
    check("zip entries are non-empty", all(z.getinfo(n).file_size>1000 for n in z.namelist()))

# --- bad input is rejected, not 500'd
for spec, why in [({"kind":"bogus","format":"png","ids":ids}, "bad kind"),
                  ({"kind":"frame","format":"exe","ids":ids}, "bad format"),
                  ({"kind":"frame","format":"png","ids":[]}, "no ids"),
                  ({"kind":"frame","format":"png","ids":["not-a-real-id"]}, "unknown id")]:
    try:
        post("/api/export/start", spec)
        check(f"rejects {why}", False, "accepted")
    except urllib.error.HTTPError as e:
        check(f"rejects {why}", 400 <= e.code < 500, f"HTTP {e.code}")

# --- cancel a long job
post("/api/export/start", {"kind":"series","format":"video","ids":[ids[0]]*170,
                           "name":"e2e-cancel","fps":30,"max_side":1200,"preset":"lossless","filters":False,"params":{}})
time.sleep(2.0)
post("/api/export/cancel", {})
p = wait(60)
check("cancel finishes the job", p["finished"], str(p)[:150])
check("cancel is reported", p["cancelled"])
try:
    urllib.request.urlopen(f"{B}/api/export/download?name=e2e-cancel.mp4")
    check("a cancelled video is not downloadable", False, "it was served")
except urllib.error.HTTPError:
    check("a cancelled video is not downloadable", True)

print("\nfails:", fails)
