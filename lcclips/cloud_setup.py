"""One-click share-server setup through the Cloudflare REST API (no Node.js,
no wrangler, no scripts): verifies an API token the user created, then creates
the R2 bucket, uploads the share Worker (server/src/worker.js) with its
bindings and a fresh upload key, turns on its workers.dev address and waits
for it to answer. Safe to re-run: existing bucket and clips are kept."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__

log = logging.getLogger(__name__)

API = os.environ.get("LCCLIPS_CF_API", "https://api.cloudflare.com/client/v4")
BUCKET = "lcclips"
SCRIPT = "lcclips"
COMPAT_DATE = "2025-09-01"
UA = f"LCClips/{__version__}"

# Opens Cloudflare's "Create API token" page with the needed permissions pre-selected.
_PERMS = [{"key": "workers_scripts", "type": "edit"}, {"key": "workers_r2", "type": "edit"},
          {"key": "account_settings", "type": "read"}]
TOKEN_URL = ("https://dash.cloudflare.com/profile/api-tokens?permissionGroupKeys="
             + urllib.parse.quote(json.dumps(_PERMS, separators=(",", ":")))
             + "&name=LCClips%20share%20server&accountId=*&zoneId=all")
SIGNUP_URL = "https://dash.cloudflare.com/sign-up"
R2_URL = "https://dash.cloudflare.com/?to=/:account/r2/overview"
PERMISSIONS_TEXT = ("Account → Workers Scripts → Edit\n"
                    "Account → Workers R2 Storage → Edit\n"
                    "Account → Account Settings → Read")


class CFError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, hint: str = ""):
        super().__init__(message)
        self.code, self.hint = code, hint


class R2NotEnabled(CFError):
    pass


@dataclass
class Account:
    id: str
    name: str


def worker_source() -> str:
    here = Path(__file__).resolve().parent
    for p in (here / "worker.js", here.parent / "server" / "src" / "worker.js"):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError("share server code (worker.js) is missing from this install")


class Cloudflare:
    def __init__(self, token: str):
        self.token = token.strip()

    def _req(self, method: str, path: str, body: dict | None = None, raw: bytes | None = None,
             ctype: str | None = None, ok_codes: tuple[int, ...] = ()):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": UA}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif raw is not None:
            data, headers["Content-Type"] = raw, ctype or "application/octet-stream"
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                raise CFError(f"Cloudflare returned HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise CFError(f"Couldn't reach Cloudflare ({getattr(e, 'reason', e)})") from None
        if payload.get("success", False):
            return payload.get("result")
        errs = payload.get("errors") or [{}]
        code, msg = errs[0].get("code"), errs[0].get("message") or "unknown error"
        if code in ok_codes:
            return None
        if code == 10042 or "enable r2" in msg.lower():
            raise R2NotEnabled("R2 storage isn't turned on for this Cloudflare account yet.", code)
        if code in (9109, 10000, 1000) or "authentication" in msg.lower() or "unauthorized" in msg.lower():
            raise CFError("Cloudflare didn't accept this token, or it's missing a permission.", code,
                          hint="Create it with the three permissions listed and copy the whole token.")
        raise CFError(f"Cloudflare: {msg} (code {code})", code)

    # ------------------------------------------------------------------ steps
    def verify(self) -> None:
        r = self._req("GET", "/user/tokens/verify")
        if r and r.get("status") not in (None, "active"):
            raise CFError(f"This token is {r.get('status')}.")

    def accounts(self) -> list[Account]:
        res = self._req("GET", "/accounts?per_page=50") or []
        accts = [Account(a["id"], a.get("name") or a["id"]) for a in res]
        if not accts:
            raise CFError("This token can't see any Cloudflare account.",
                          hint="Add 'Account Settings: Read' and choose 'All accounts' (or yours).")
        return accts

    def subdomain(self, acct: str) -> str | None:
        try:
            r = self._req("GET", f"/accounts/{acct}/workers/subdomain")
        except R2NotEnabled:
            raise
        except CFError:
            return None
        return (r or {}).get("subdomain") or None

    def create_subdomain(self, acct: str, name: str) -> str:
        r = self._req("PUT", f"/accounts/{acct}/workers/subdomain", {"subdomain": name})
        return (r or {}).get("subdomain") or name

    def ensure_bucket(self, acct: str) -> None:
        # 10004: "the bucket you tried to create already exists, and you own it" - fine (keeps clips).
        self._req("POST", f"/accounts/{acct}/r2/buckets", {"name": BUCKET}, ok_codes=(10004,))

    def deploy_worker(self, acct: str, upload_token: str, key_name: str) -> None:
        meta = {
            "main_module": "worker.js",
            "compatibility_date": COMPAT_DATE,
            # Keep the upload keys of other computers already set up on this server.
            "keep_bindings": ["secret_text"],
            "bindings": [
                {"type": "r2_bucket", "name": "CLIPS", "bucket_name": BUCKET},
                {"type": "plain_text", "name": "SITE_NAME", "text": "LCClips"},
                {"type": "secret_text", "name": key_name, "text": upload_token},
            ],
        }
        boundary = "----LCClips" + secrets.token_hex(12)
        parts = [
            ("metadata", "", "application/json", json.dumps(meta).encode()),
            ("worker.js", "worker.js", "application/javascript+module", worker_source().encode()),
        ]
        body = b""
        for name, filename, ctype, content in parts:
            disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
            body += (f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n").encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        self._req("PUT", f"/accounts/{acct}/workers/scripts/{SCRIPT}", raw=body,
                  ctype=f"multipart/form-data; boundary={boundary}")

    def enable_workers_dev(self, acct: str) -> None:
        self._req("POST", f"/accounts/{acct}/workers/scripts/{SCRIPT}/subdomain",
                  {"enabled": True, "previews_enabled": False})


def suggest_subdomain(account_name: str) -> str:
    name = account_name.lower().split("@")[0]
    name = re.sub(r"['’]s account$|\s+account$", "", name).replace("'", "").replace("’", "")
    base = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")[:40] or "clips"
    return f"{base}-clips" if not base.endswith("clips") else base


def wait_online(url: str, upload_token: str, timeout: float = 120.0) -> bool:
    """Polls /api/ping until the new worker answers with our key (new workers.dev names take a moment)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            req = urllib.request.Request(url + "/api/ping", headers={"Authorization": f"Bearer {upload_token}",
                                                                     "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    return False


def setup(cf: Cloudflare, account: Account, subdomain: str | None,
          progress: Callable[[str], None] = lambda s: None,
          online: Callable[[str, str], bool] = wait_online) -> tuple[str, str, bool]:
    """Runs every step. Returns (server_url, upload_key, online_now)."""
    progress("Creating your storage bucket")
    cf.ensure_bucket(account.id)
    progress("Setting up your link address")
    sub = cf.subdomain(account.id)
    if not sub:
        if not subdomain:
            raise CFError("Choose a name for your link address first.")
        sub = cf.create_subdomain(account.id, subdomain)
    upload_token = secrets.token_urlsafe(32)
    progress("Uploading your share server")
    # Each computer gets its own key (UPLOAD_TOKEN_<id>); other computers' keys are kept.
    cf.deploy_worker(account.id, upload_token, f"UPLOAD_TOKEN_{secrets.token_hex(4).upper()}")
    progress("Turning on its web address")
    cf.enable_workers_dev(account.id)
    url = f"https://{SCRIPT}.{sub}.workers.dev"
    progress("Waiting for it to come online")
    ok = online(url, upload_token)
    log.info("Share server set up at %s (online=%s)", url, ok)
    return url, upload_token, ok
