"""
Runtime captcha config. Env-overridable. API-mutable. Thread-safe.
Persists to captcha_state.json so sitekeys survive restarts.
"""
import json
import os
import threading
from pathlib import Path
from typing import Dict, List

_LOCK = threading.RLock()
_STORE = Path(os.environ.get("CAPTCHA_STORE", "captcha_state.json"))


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class CaptchaConfig:
    def __init__(self):
        self.enabled = _env_bool("CAPTCHA_ENABLED", True)
        self.retries = int(os.environ.get("CAPTCHA_RETRIES", "2"))
        self.auto_solve = _env_bool("CAPTCHA_AUTO_SOLVE", True)
        self.service = os.environ.get("CAPTCHA_SERVICE", "").lower()
        self.api_key = os.environ.get("CAPTCHA_API_KEY", "")
        self.poll_sec = float(os.environ.get("CAPTCHA_POLL_SEC", "3"))
        self.max_wait = int(os.environ.get("CAPTCHA_MAX_WAIT", "120"))
        self.shop_sitekeys: Dict[str, str] = {}
        self.fallback_sitekeys: List[str] = []
        self._load()

    def _load(self):
        with _LOCK:
            if _STORE.exists():
                try:
                    data = json.loads(_STORE.read_text())
                    self.shop_sitekeys = data.get("shop_sitekeys", {}) or {}
                    self.fallback_sitekeys = data.get("fallback_sitekeys", []) or []
                    if "enabled" in data:
                        self.enabled = bool(data["enabled"])
                    if "auto_solve" in data:
                        self.auto_solve = bool(data["auto_solve"])
                    if "retries" in data:
                        self.retries = int(data["retries"])
                except Exception:
                    pass

    def _save(self):
        with _LOCK:
            try:
                _STORE.write_text(json.dumps({
                    "enabled": self.enabled,
                    "auto_solve": self.auto_solve,
                    "retries": self.retries,
                    "shop_sitekeys": self.shop_sitekeys,
                    "fallback_sitekeys": self.fallback_sitekeys,
                }, indent=2))
            except Exception:
                pass

    def snapshot(self) -> dict:
        with _LOCK:
            return {
                "enabled": self.enabled,
                "auto_solve": self.auto_solve,
                "retries": self.retries,
                "service": self.service or "local",
                "has_api_key": bool(self.api_key),
                "poll_sec": self.poll_sec,
                "max_wait": self.max_wait,
                "shop_sitekeys": dict(self.shop_sitekeys),
                "fallback_sitekeys": list(self.fallback_sitekeys),
            }

    def set_enabled(self, on: bool):
        with _LOCK:
            self.enabled = bool(on); self._save()

    def set_auto_solve(self, on: bool):
        with _LOCK:
            self.auto_solve = bool(on); self._save()

    def set_retries(self, n: int):
        with _LOCK:
            self.retries = max(0, int(n)); self._save()

    def set_shop_sitekey(self, host: str, key: str):
        with _LOCK:
            host = host.lower().strip()
            if key:
                self.shop_sitekeys[host] = key.strip()
            else:
                self.shop_sitekeys.pop(host, None)
            self._save()

    def set_fallback_sitekeys(self, keys: List[str]):
        with _LOCK:
            self.fallback_sitekeys = [k.strip() for k in keys if k and k.strip()]
            self._save()


CONFIG = CaptchaConfig()
