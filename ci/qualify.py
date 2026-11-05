#!/usr/bin/env python3
"""Linux ARM64 OCI qualification for POOF Instant candidates. Runs on a disposable GitHub runner.

  qualify.py probe   --out DIR            isolation gate: must pass before any third-party image starts
  qualify.py control --out DIR            first-party control image (built here) through the same path
  qualify.py app ID  --out DIR            pull by digest, verify, run hardened, test, record evidence
  add --dry-run to print the docker commands instead of running them (no images pulled or started)

Results are Linux container qualification only. They do NOT prove POOF compatibility on macOS.
"""
import argparse, json, os, secrets, shlex, subprocess, sys, threading, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APPS = json.load(open(os.path.join(ROOT, "apps.json")))
LOCK = json.load(open(os.path.join(ROOT, "images.lock.json")))["images"]
CLIENT = APPS["client"]["image"]
NET, DATA = "poofq", "/mnt/poofq-data"
HARDEN = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
          "--network", NET, "--label", "poofq=1", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m"]

ap = argparse.ArgumentParser()
ap.add_argument("mode", choices=["probe", "control", "app"])
ap.add_argument("app", nargs="?")
ap.add_argument("--out", default="evidence")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()
os.makedirs(a.out, exist_ok=True)
LOG = open(os.path.join(a.out, f"commands-{a.mode}{'-' + a.app if a.app else ''}.log"), "a")


def sh(cmd, check=True, timeout=600, capture=True):
    LOG.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n"); LOG.flush()
    if a.dry_run:
        print("DRY-RUN:", " ".join(shlex.quote(c) for c in cmd))
        return subprocess.CompletedProcess(cmd, 0, "{}" if "inspect" in cmd else "", "")
    r = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    LOG.write((r.stdout or "")[-4000:] + (r.stderr or "")[-4000:] + f"\n→ exit {r.returncode}\n"); LOG.flush()
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd[:3]} failed ({r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    return r


CLIENT_CODE = r'''
import base64, json, sys, time, urllib.request
host, steps = sys.argv[1], json.loads(sys.argv[2])
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj 3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
out = []
for s in steps:
    deadline = time.time() + s.get("retrySeconds", 0)
    while True:
        url = "http://" + host + s["path"]; headers = {}; data = None
        if "json" in s: data = json.dumps(s["json"]).encode(); headers["Content-Type"] = "application/json"
        if "multipartPdf" in s:
            b = "poofqboundary"; parts = []
            for k, v in s.get("fields", {}).items(): parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{s["multipartPdf"]}"; filename="q.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode() + PDF + b"\r\n")
            data = b"".join(parts) + f"--{b}--\r\n".encode(); headers["Content-Type"] = "multipart/form-data; boundary=" + b
        if "basicAuth" in s: headers["Authorization"] = "Basic " + base64.b64encode(s["basicAuth"].encode()).decode()
        req = urllib.request.Request(url, data=data, headers=headers, method=s.get("method", "GET"))
        try:
            with urllib.request.urlopen(req, timeout=30) as r: code, body = r.status, r.read()
        except urllib.error.HTTPError as e: code, body = e.code, e.read()
        except Exception as e: code, body = 0, str(e).encode()
        ok = (s.get("expectStatus") in (None, code)) and (("expectContains" not in s) or s["expectContains"].encode() in body) \
             and (("expectBodyPrefix" not in s) or body.startswith(s["expectBodyPrefix"].encode())) \
             and (("expectStatusBelow" not in s) or (0 < code < s["expectStatusBelow"]))
        if ok or time.time() > deadline: break
        time.sleep(1)
    out.append({"step": s["method"] + " " + s["path"] if "method" in s else "GET " + s["path"], "status": code, "ok": ok, "bytes": len(body)})
    if not ok: break
print(json.dumps(out))
'''


def client(steps, timeout=180):
    r = sh(["docker", "run", "--rm", *HARDEN, "--read-only", "--memory", "128m", CLIENT,
            "python", "-c", CLIENT_CODE, "app:" + str(spec["port"]), json.dumps(steps)], check=False, timeout=timeout)
    res = json.loads(r.stdout or "[]") if not a.dry_run else [{"ok": True, "dry": True}]
    return all(x["ok"] for x in res) and bool(res), res


def wait_ready(timeout=180):
    mode = spec.get("readyMode", "2xx")
    step = {"method": "GET", "path": spec["ready"], "retrySeconds": timeout}
    step.update({"expectStatusBelow": 500} if mode == "lt500" else {"expectStatusBelow": 400})
    t0 = time.time(); ok, res = client([step], timeout=timeout + 30)
    return ok, time.time() - t0, res


class Peaks(threading.Thread):
    def __init__(s): super().__init__(daemon=True); s.mem = 0; s.cpu = 0.0; s.samples = 0; s.stop = False
    def run(s):
        while not s.stop and not a.dry_run:
            r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}", "app"], capture_output=True, text=True)
            try:
                st = json.loads(r.stdout.strip().splitlines()[0]); s.samples += 1
                s.cpu = max(s.cpu, float(st["CPUPerc"].rstrip("%")))
                used = st["MemUsage"].split("/")[0].strip(); n = float(used[:-3]); u = used[-3:]
                s.mem = max(s.mem, int(n * {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(u, 1)))
            except Exception: pass
            time.sleep(2)


def run_app(image):
    # --init: same as POOF on macOS, so the app gets stop signals instead of being force-killed as PID 1.
    cmd = ["docker", "run", "-d", "--init", "--name", "app", "--network-alias", "app", *HARDEN,
           "--memory", spec["memory"], "--memory-swap", spec["memory"], "--cpus", "2"]
    for c in spec.get("capAdd", []): cmd += ["--cap-add", c]
    if spec.get("dataPath"): cmd += ["-v", f"{DATA}:{spec['dataPath']}"]
    sh(cmd + [image])


def evidence(name, obj):
    json.dump(obj, open(os.path.join(a.out, name), "w"), indent=1, sort_keys=True)


# ---------------------------------------------------------------- probe (gate)
if a.mode == "probe":
    code = r'''
import json, os, socket
def tcp(h, p):
    s = socket.socket(); s.settimeout(3)
    try: s.connect((h, p)); return "OPEN"
    except Exception as e: return type(e).__name__
    finally: s.close()
gw = [l.split()[2] for l in open("/proc/net/route") if l.split()[1] == "00000000"]
st = {l.split(":")[0]: l.split(":")[1].strip() for l in open("/proc/self/status") if ":" in l}
print(json.dumps({"internet": tcp("1.1.1.1", 443), "imds": tcp("169.254.169.254", 80), "wireserver": tcp("168.63.129.16", 80),
  "runner_via_bridge": {p: tcp("172.31.250.1", p) for p in (22, 80, 443, 2375, 2376)}, "default_route": gw,
  "docker_socket": os.path.exists("/var/run/docker.sock"), "cap_eff": st.get("CapEff"), "no_new_privs": st.get("NoNewPrivs"),
  "env": sorted(os.environ), "generated_files_are_files": {f: os.path.isfile(f) and not os.path.islink(f) for f in ("/etc/resolv.conf", "/etc/hostname", "/etc/hosts")}, "mounts": [l.split()[1] for l in open("/proc/mounts") if l.split()[2] in ("ext4", "xfs", "overlay", "nfs", "9p")]}))
'''
    r = sh(["docker", "run", "--rm", *HARDEN, "--read-only", "--memory", "128m", "-v", f"{DATA}:/probe-data",
            CLIENT, "python", "-c", code], check=False)
    p = json.loads(r.stdout or "{}") if not a.dry_run else {}
    gates = {
        "no internet egress": p.get("internet") != "OPEN",
        "no cloud metadata (IMDS)": p.get("imds") != "OPEN",
        "no Azure wireserver": p.get("wireserver") != "OPEN",
        "runner host unreachable": all(v != "OPEN" for v in p.get("runner_via_bridge", {"x": "OPEN"}).values()),
        "no docker socket": p.get("docker_socket") is False,
        "all capabilities dropped": p.get("cap_eff") == "0000000000000000",
        "no-new-privileges": p.get("no_new_privs") == "1",
        "no runner credentials in env": not any(k.startswith(("GITHUB_", "ACTIONS_", "RUNNER_")) for k in p.get("env", [])),
        # Docker bind-mounts three generated, per-container files into every container; nothing else is allowed.
        "only the capped data mount (+ Docker's per-container resolv.conf/hostname/hosts)":
            set(p.get("mounts", ["?"])) <= {"/", "/probe-data", "/etc/resolv.conf", "/etc/hostname", "/etc/hosts"}
            and all(p.get("generated_files_are_files", {}).values() or [False]),
    }
    evidence("probe.json", {"observed": p, "gates": gates, "dryRun": a.dry_run})
    for k, v in gates.items(): print(("PASS " if v else "FAIL ") + k)
    if a.dry_run: sys.exit(0)
    sys.exit(0 if all(gates.values()) else 3)

# ---------------------------------------------------------------- control / app
app_id = "control-notes" if a.mode == "control" else a.app
spec = APPS["apps"][app_id]
marker = f"POOFQ-{uuid.uuid4().hex[:10]}"
password = secrets.token_urlsafe(18)       # disposable test instance only; never a stored secret
fill = lambda steps: json.loads(json.dumps(steps).replace("{marker}", marker).replace("{password}", password))
result = {"app": app_id, "platform": "linux/arm64 (GitHub-hosted ubuntu-24.04-arm)", "scope": "Linux OCI qualification only; not POOF-on-macOS compatibility",
          "dryRun": a.dry_run, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
peaks = Peaks()
try:
    if a.mode == "control":
        image = "poofq-control:local"
        sh(["docker", "build", "--label", "poofq=1", "-t", image, os.path.join(ROOT, spec["build"])])
        result["image"] = {"ref": image, "source": "first-party, built in this job"}
    else:
        lock = LOCK[app_id]
        image = f"{lock['image']}@{lock['indexDigest']}"
        t0 = time.time()
        sh(["docker", "pull", "--platform", "linux/arm64", image], timeout=900)
        result["pullSeconds"] = round(time.time() - t0, 1)
        ins = json.loads(sh(["docker", "image", "inspect", image]).stdout or "[{}]")
        info = ins[0] if isinstance(ins, list) and ins else {}
        result["image"] = {"ref": image, "repoDigests": info.get("RepoDigests"), "architecture": info.get("Architecture"),
                           "os": info.get("Os"), "sizeBytes": info.get("Size"), "provenance": lock["provenance"]}
        if not a.dry_run and (info.get("Architecture") != "arm64" or lock["indexDigest"] not in " ".join(info.get("RepoDigests") or [])):
            raise RuntimeError("pulled image is not the pinned linux/arm64 digest")
        if lock["provenance"]["cosignSignatureTag"]:
            v = sh(["./cosign", "verify", "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
                    "--certificate-identity-regexp", "^https://github.com/" + lock["source"].split("github.com/")[1] + "/", image], check=False)
            result["image"]["cosignVerified"] = v.returncode == 0
    peaks.start()
    t0 = time.time(); run_app(image)
    ok, secs, res = wait_ready(); result["ready"] = {"ok": ok, "seconds": round(secs, 1), "detail": res}
    if not ok: raise RuntimeError("never became ready")
    result["timeToReadySeconds"] = round(time.time() - t0, 1)
    ok, res = client(fill(spec["functional"])); result["functional"] = {"ok": ok, "steps": res}
    t1 = time.time(); r = sh(["docker", "stop", "-t", "20", "app"]); result["gracefulStopSeconds"] = round(time.time() - t1, 1)
    result["exitCodeAfterStop"] = (sh(["docker", "inspect", "-f", "{{.State.ExitCode}}", "app"]).stdout or "").strip()
    sh(["docker", "rm", "app"])
    run_app(image); ok, _, _ = wait_ready()
    ok2, res = client(fill(spec["persisted"])); result["persistence"] = {"ok": ok and ok2, "steps": res, "note": spec.get("persistenceNote")}
    sh(["docker", "kill", "app"]); sh(["docker", "rm", "app"])
    t2 = time.time(); run_app(image); ok, _, _ = wait_ready()
    ok3, res = client(fill(spec["persisted"])); result["recovery"] = {"ok": ok and ok3, "seconds": round(time.time() - t2, 1), "steps": res}
    du = sh(["du", "-sb", DATA], check=False).stdout.split()
    result["dataBytes"] = int(du[0]) if du and du[0].isdigit() else None
    result["verdict"] = "dry-run (nothing executed)" if a.dry_run else (
        "qualified" if all(result.get(k, {}).get("ok") for k in ("functional", "persistence", "recovery")) else "not qualified")
except Exception as e:
    result["verdict"] = "error"; result["error"] = str(e)[-600:]
finally:
    peaks.stop = True
    result["peak"] = {"memoryBytes": peaks.mem, "cpuPercent": peaks.cpu, "samples": peaks.samples, "memoryLimit": spec["memory"]}
    logs = sh(["docker", "logs", "--tail", "300", "app"], check=False)
    open(os.path.join(a.out, f"{app_id}-container.log"), "w").write((logs.stdout or "") + (logs.stderr or ""))
    sh(["docker", "rm", "-f", "app"], check=False)
    if a.mode == "app": sh(["docker", "rmi", "-f", image], check=False)
    result["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    evidence(f"{app_id}.json", result)
    print(json.dumps({k: result.get(k) for k in ("app", "verdict", "timeToReadySeconds", "error")}))
sys.exit(0 if result.get("verdict") == "qualified" or a.dry_run else 4)
