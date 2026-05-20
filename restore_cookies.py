#!/usr/bin/env python3
"""
Flow2API Cookie 自动恢复脚本
在容器重启后自动注入备份的 cookies 并导航到 labs.google/fx
使用方式: docker exec flow2api-headed python3 /app/restore_cookies.py
"""
import json, urllib.request, asyncio, sys, glob, time, os

COOKIE_BACKUP = "/app/src/tmp/cookies_backup.json"
FLOW_URL = "https://labs.google/fx/tools/flow"
MAX_WAIT = 60

def find_cdp_port():
    for f in glob.glob("/proc/*/cmdline"):
        try:
            with open(f, "rb") as fh:
                cmd = fh.read().decode(errors="replace").replace(chr(0), " ")
                if "chrome" in cmd.lower() and "--remote-debugging-port=" in cmd and "--type" not in cmd:
                    for part in cmd.split():
                        if part.startswith("--remote-debugging-port="):
                            return int(part.split("=")[1])
        except:
            pass
    return None

def wait_for_browser():
    for i in range(MAX_WAIT):
        port = find_cdp_port()
        if port:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=3)
                return port
            except:
                pass
        time.sleep(1)
    return None

def main():
    try:
        with open(COOKIE_BACKUP) as f:
            cookies = json.load(f)
    except FileNotFoundError:
        print("ERROR: No cookie backup found at %s" % COOKIE_BACKUP)
        sys.exit(1)

    print("Loaded %d cookies from backup" % len(cookies))
    print("Waiting for Chrome to start...")

    cdp_port = wait_for_browser()
    if not cdp_port:
        print("ERROR: Chrome did not start within %ds" % MAX_WAIT)
        sys.exit(1)

    print("Chrome CDP port: %d" % cdp_port)

    try:
        import websockets
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "-q"])
        import websockets

    resp = urllib.request.urlopen("http://127.0.0.1:%d/json" % cdp_port, timeout=5)
    pages = json.loads(resp.read())
    ws_url = None
    for p in pages:
        if p.get("type") == "page":
            ws_url = p.get("webSocketDebuggerUrl")
            break

    if not ws_url:
        print("ERROR: No browser page found")
        sys.exit(1)

    async def inject():
        async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
            injected = 0
            for i, c in enumerate(cookies):
                params = {
                    "name": c["name"], "value": c["value"],
                    "domain": c.get("domain", ""), "path": c.get("path", "/"),
                    "secure": c.get("secure", False), "httpOnly": c.get("httpOnly", False),
                }
                if c.get("sameSite"): params["sameSite"] = c["sameSite"]
                if c.get("expires") and c["expires"] > 0: params["expires"] = c["expires"]

                await ws.send(json.dumps({"id": i+1, "method": "Network.setCookie", "params": params}))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if resp.get("result", {}).get("success"):
                    injected += 1

            print("Injected: %d/%d cookies" % (injected, len(cookies)))

            await ws.send(json.dumps({"id": 9999, "method": "Page.navigate", "params": {"url": FLOW_URL}}))
            await asyncio.wait_for(ws.recv(), timeout=10)
            print("Navigated to %s" % FLOW_URL)

    asyncio.run(inject())

    time.sleep(3)

    async def extract_and_register():
        resp = urllib.request.urlopen("http://127.0.0.1:%d/json" % cdp_port, timeout=5)
        pages = json.loads(resp.read())
        ws_url = None
        for p in pages:
            if "labs.google" in p.get("url", "") and p.get("type") == "page":
                ws_url = p.get("webSocketDebuggerUrl")
                break
        if not ws_url:
            for p in pages:
                if p.get("type") == "page":
                    ws_url = p.get("webSocketDebuggerUrl")
                    break

        async with websockets.connect(ws_url, max_size=10*1024*1024) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Network.getCookies", "params": {"urls": ["https://labs.google", "https://labs.google/fx"]}}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            for c in resp.get("result", {}).get("cookies", []):
                if c.get("name") == "__Secure-next-auth.session-token":
                    return c["value"]
        return None

    st = asyncio.run(extract_and_register())
    if st:
        print("Session token: %d chars" % len(st))
        admin_username = os.environ.get("FLOW2API_ADMIN_USERNAME", "admin")
        admin_password = os.environ.get("FLOW2API_ADMIN_PASSWORD", "admin")
        login_data = json.dumps({"username": admin_username, "password": admin_password}).encode()
        req = urllib.request.Request("http://127.0.0.1:8000/api/admin/login", data=login_data, headers={"Content-Type": "application/json"}, method="POST")
        admin_token = json.loads(urllib.request.urlopen(req).read())["token"]

        req = urllib.request.Request("http://127.0.0.1:8000/api/tokens", headers={"Authorization": "Bearer " + admin_token})
        tokens = json.loads(urllib.request.urlopen(req).read())

        if tokens:
            tid = tokens[0]["id"]
            req = urllib.request.Request("http://127.0.0.1:8000/api/tokens/%d" % tid, data=json.dumps({"st": st}).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + admin_token}, method="PUT")
            try:
                urllib.request.urlopen(req)
                print("Token %d updated" % tid)
            except:
                req = urllib.request.Request("http://127.0.0.1:8000/api/tokens/%d" % tid, headers={"Authorization": "Bearer " + admin_token}, method="DELETE")
                try: urllib.request.urlopen(req)
                except: pass
                req = urllib.request.Request("http://127.0.0.1:8000/api/tokens", data=json.dumps({"st": st}).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + admin_token}, method="POST")
                urllib.request.urlopen(req)
                print("Token re-registered")
        else:
            req = urllib.request.Request("http://127.0.0.1:8000/api/tokens", data=json.dumps({"st": st}).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + admin_token}, method="POST")
            urllib.request.urlopen(req)
            print("New token registered")

        print("DONE - Cookie restore complete")
    else:
        print("WARNING: No session token found - may need manual login")

if __name__ == "__main__":
    main()
