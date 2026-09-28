import base64
import binascii

import requests

from config import host, api

# Same RSA public key the official web login page embeds in login.js
# (fe-static-yuketang.yuketang.cn/fe/static/vue/<ver>/login.js). The password
# field is RSA/PKCS1v15-encrypted client-side before POSTing.
_PUBLIC_KEY_DER_BASE64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCQBaPX7crEH6/jS4hRD7lZrsFRIdfwEhH30on"
    "FnrnWxiRATzP9WEneXJEZHopmzudkNS5bDp51SCnBUGGgfL/sUUrlrhV2xnTSe1jRl924ejV5rk"
    "Vkiii85jp9G8eJrJN6klHs0PfYfp4EVJ8688qpi5iETtg+q4ITocyEyD1+7wIDAQAB"
)

login_type_map = {
    "phone": "PP",
    "email": "E",
}


def _encrypt_password(password: str) -> str:
    """RSA-encrypt the password exactly like the official login page does."""
    try:
        from Crypto.Cipher import PKCS1_v1_5
        from Crypto.PublicKey import RSA
    except ImportError:
        # pycryptodome is expected in requirements; fail loudly if missing.
        raise

    der = base64.b64decode(_PUBLIC_KEY_DER_BASE64)
    key = RSA.import_key(der)
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(password.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def login(name: str, password: str, login_type: str = "phone"):
    """Password login. Returns the new sessionid, or raises on failure.

    The server may answer 300416 (Tencent captcha required) — in that case
    programmatic login is impossible from this egress IP and the caller
    should fall back to a manually refreshed SESSION.
    """
    payload = {
        "type": login_type_map.get(login_type, "PP"),
        "name": name,
        "pwd": _encrypt_password(password),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/129.0.0.0 Safari/537.36",
        "Referer": host,
        "X-Requested-With": "XMLHttpRequest",
    }
    response = requests.post(host + api["login_user"], json=payload, headers=headers, timeout=15)
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"login: non-JSON reply HTTP {response.status_code}")

    if not body.get("success"):
        raise RuntimeError(f"login failed: {body.get('status_code')} {body.get('msg')}")

    session_id = response.cookies.get("sessionid")
    if not session_id:
        raise RuntimeError("login succeeded but no sessionid cookie returned")
    return session_id
