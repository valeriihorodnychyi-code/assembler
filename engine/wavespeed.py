"""WaveSpeed AI (Seedance 2.5 video-edit for ⧉ parts). The key lives ONLY in
~/.assembler/config.json (wavespeed_api_key) / env WAVESPEED_API_KEY — never in the repo."""
import os

API = "https://api.wavespeed.ai/api/v3"


class WaveSpeedError(Exception):
    pass


def _key(api_key=None):
    k = (api_key or os.environ.get("WAVESPEED_API_KEY") or "").strip()
    if not k:
        raise WaveSpeedError("Немає ключа WaveSpeed — додай його в ⚙ Settings.")
    return k


def balance(requests, api_key=None):
    """→ USD balance (float). Also the key check: 401 = wrong key."""
    h = {"Authorization": f"Bearer {_key(api_key)}"}
    last = None
    for path in ("/balance", "/account/balance"):
        r = requests.get(API + path, headers=h, timeout=20)
        if r.status_code in (401, 403):
            raise WaveSpeedError("WaveSpeed не приймає ключ (401) — перевір, що скопійовано повністю.")
        if r.status_code == 200:
            d = r.json() or {}
            v = (d.get("data") or {}).get("balance", d.get("balance"))
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        last = r.status_code
    raise WaveSpeedError(f"WaveSpeed: не вдалося прочитати баланс (HTTP {last}).")
