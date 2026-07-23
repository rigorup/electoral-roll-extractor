"""In-process ECINET client: EPIC number -> full voter record + document images.

Adapted from the standalone `epic_lookup/server.py` so the app can resolve EPIC
numbers **without a separate localhost server running**. Same two-call chain:

    EPIC number  ->  getSsrDetails           ->  epicId
    epicId       ->  getOnlineEnuDetailsNew  ->  full record

Session values (token, binding headers, jurisdiction) live in a config.json that
is re-read on every lookup, so a freshly pasted token takes effect immediately
with no restart. Point at it with the `EPIC_LOOKUP_CONFIG` env var.

Read-only and idempotent: safe to call in a loop.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

BASE = "https://gateway-officials.eci.gov.in/api/v1"

# Where config.json lives. Override with EPIC_LOOKUP_CONFIG.
DEFAULT_CONFIG = (
    "/Users/laxmi_narayan_verma/Documents/LBSNAA/DPT/Student_bank_account/"
    "script_for_merge/epic_lookup/config.json"
)


def config_path() -> Path:
    """Resolve config.json: env var -> project-local file -> original location."""
    env = os.getenv("EPIC_LOOKUP_CONFIG")
    if env:
        return Path(env)
    local = Path(__file__).with_name("config.json")
    if local.exists():
        return local
    return Path(DEFAULT_CONFIG)


def load_config() -> dict:
    with open(config_path(), encoding="utf-8") as f:
        return json.load(f)


def config_available() -> tuple[bool, str]:
    """(usable, human message) — lets the UI explain itself before any lookup."""
    p = config_path()
    if not p.exists():
        return False, f"config.json not found at {p}"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"config.json is not valid JSON: {e}"
    sessions = cfg.get("sessions") or []
    if not sessions:
        return False, "config.json has no sessions configured"
    acs = ", ".join(str(s.get("acNo")) for s in sessions)
    return True, f"{len(sessions)} AC session(s) configured: {acs}"


def configured_acs() -> list[int]:
    try:
        return [int(s["acNo"]) for s in (load_config().get("sessions") or [])
                if s.get("acNo") is not None]
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------ transport
def _headers(cfg: dict, sess: dict) -> dict:
    return {
        "Authorization": "Bearer " + sess["token"],
        "atkn_bnd": sess["atkn_bnd"],
        "rtkn_bnd": sess["rtkn_bnd"],
        "User-Agent": cfg["userAgent"],  # must match the UA the token was bound to
        "Accept": "application/json, text/plain, */*",
        "CurrentRole": "ero",
        "Origin": "https://ecinet.eci.gov.in",
        "PLATFORM-TYPE": "ECIWEB",
        "applicationName": "ECI-NET",
        "appname": "ECI-NET",
        "channelidobo": "ECI-NET",
        "state": cfg["stateCd"],
    }


def _request(req: urllib.request.Request, timeout: int = 30):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"message": raw[:500]}
    except Exception as e:  # noqa: BLE001
        return 0, {"message": str(e)}


def eci_post(path: str, body: dict, cfg: dict, sess: dict):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 method="POST")
    for k, v in {**_headers(cfg, sess), "content-type": "application/json"}.items():
        req.add_header(k, v)
    return _request(req)


def eci_get(path: str, cfg: dict, sess: dict):
    req = urllib.request.Request(BASE + path, method="GET")
    for k, v in _headers(cfg, sess).items():
        req.add_header(k, v)
    return _request(req)


# -------------------------------------------------------------------- lookups
def find_session(cfg: dict, ac) -> dict | None:
    for s in (cfg.get("sessions") or []):
        if str(s.get("acNo")) == str(ac):
            return s
    return None


def lookup_in_session(epic: str, cfg: dict, sess: dict):
    """(result | None, expired_bool) for one AC session."""
    search_body = {
        "stateCd": cfg["stateCd"],
        "districtCd": sess.get("districtCd"),
        "acNo": sess.get("acNo"),
        "pageNumber": 1,
        "pageSize": 25,
        "epicNumber": epic,
        "documentUploadedFlg": None,
        "submittedForRecommendation": None,
        "unCollectableReason": None,
        "category": None,
        "categoryType": None,
    }
    code, sres = eci_post("/eci-sir/pi/getSsrDetails", search_body, cfg, sess)
    if code == 401:
        return None, True
    arr = ((sres.get("payload") or {}).get("ssrResponse") or []) \
        if isinstance(sres, dict) else []
    if not arr:
        return None, False

    epic_id = arr[0].get("epicId")
    details_body = {"stateCd": cfg["stateCd"], "epicNo": epic, "epicId": int(epic_id)}
    code2, dres = eci_post("/enumerationFormData/getOnlineEnuDetailsNew",
                           details_body, cfg, sess)
    if code2 == 401:
        return None, True

    payload = dres.get("payload") if isinstance(dres, dict) else None
    base = {"ok": True, "acNo": sess.get("acNo"), "loginName": sess.get("loginName"),
            "officer": sess.get("officer"), "epicId": epic_id, "search": arr[0]}
    if not payload:
        # Found in search but details failed — report what we have.
        base["details"] = None
        base["message"] = (dres.get("message") if isinstance(dres, dict)
                           else "details unavailable")
        return base, False
    base["details"] = payload
    return base, False


def lookup(epic: str, cfg: dict | None = None) -> dict:
    """Try each configured AC session until the EPIC resolves (auto-routing)."""
    cfg = cfg or load_config()
    sessions = cfg.get("sessions") or []
    if not sessions:
        return {"ok": False, "message": "No sessions configured in config.json"}

    expired_acs, tried = [], []
    for sess in sessions:
        tried.append(sess.get("acNo"))
        res, expired = lookup_in_session(epic, cfg, sess)
        if res:
            return res
        if expired:
            expired_acs.append(sess.get("acNo"))

    if expired_acs and len(expired_acs) == len(sessions):
        return {"ok": False, "expired": True,
                "message": f"All tokens expired (AC {', '.join(map(str, expired_acs))}). "
                           "Paste fresh tokens into config.json."}
    msg = f"Not found in configured ACs {tried}."
    if expired_acs:
        msg += (f" Note: AC {', '.join(map(str, expired_acs))} token expired — "
                "that AC was not searched.")
    return {"ok": False, "message": msg, "expired": bool(expired_acs)}


# --------------------------------------------------------------------- images
def _sniff_ext(data: bytes) -> str:
    """ECI mislabels PNGs as image/jpeg, so trust the magic bytes instead."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"%PDF":
        return ".pdf"
    return ".bin"


def fetch_image(storage_path: str, cfg: dict, sess: dict,
                bucket: str = "objectstorage"):
    """(bytes, ext, None) for an object-storage key, or (None, None, error).

    Two ECI mechanisms, tried in order:
      1) document-adhoc/getPresignedFile -> {preSignedUrl} -> plain download
      2) document/getFile               -> {file: <base64>}
    """
    q = "?bucketName=" + quote(bucket) + "&fileName=" + quote(storage_path, safe="")

    code, res = eci_get("/document-adhoc/getPresignedFile" + q, cfg, sess)
    if code == 401:
        return None, None, "token expired"
    url = res.get("preSignedUrl") if isinstance(res, dict) else None
    if url:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            return data, _sniff_ext(data), None
        except Exception as e:  # noqa: BLE001
            return None, None, f"presigned download failed: {e}"

    code2, res2 = eci_get("/document/getFile" + q, cfg, sess)
    if code2 == 401:
        return None, None, "token expired"
    b64 = res2.get("file") if isinstance(res2, dict) else None
    if b64:
        try:
            data = base64.b64decode(b64)
            return data, _sniff_ext(data), None
        except Exception as e:  # noqa: BLE001
            return None, None, f"base64 decode failed: {e}"

    msg = (res.get("message") if isinstance(res, dict) else None) or \
          (res2.get("message") if isinstance(res2, dict) else None) or "file not found"
    return None, None, msg


def fetch_image_for_ac(storage_path: str, ac, cfg: dict | None = None):
    """Convenience wrapper: pick the session for `ac` (with sensible fallback)."""
    cfg = cfg or load_config()
    sess = find_session(cfg, ac)
    if sess is None:
        parts = storage_path.split("/")
        guess = (parts[2] if len(parts) > 2 and parts[1] == "SR_FORM"
                 else (parts[1] if len(parts) > 1 else None))
        sess = find_session(cfg, guess) or (cfg.get("sessions") or [None])[0]
    if sess is None:
        return None, None, "no session configured"
    return fetch_image(storage_path, cfg, sess)
