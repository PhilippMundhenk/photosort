"""Decision-model client. Two primitives are used by the pipeline:

  choice(state, options) -> (option, {option: prob}, conf)
  noul(state, question)  -> (prob_yes, conf)

Backends:
  RuleBackend  - deterministic heuristics; used when kev_url is empty, and as fallback on errors.
  HttpBackend  - talks to any server implementing TypeSafe's System One API, which Jev and its
                 open reproductions (Kev, Laya via laya-server, ...) share:

    POST <kev_url>/v1/systemone
      {"state": "<text>", "model": "<optional>",
       "questions": {"<id>": {"type": "choice"|"noul"|"score",
                              "instructions": "<what to judge>",
                              "criteria": {"<option>": "<description or null>", ...}}}}
    -> {"model": "...",
        "answers": {"<id>": {"type": "choice", "choice": "<option>",
                             "probabilities": {"<option>": p, ...}, "confidence": c}
                    | {"type": "noul", "noul": p_yes, "confidence": c}}}

  kev_url is the server's base URL (http://laya:8000); a URL already ending in /v1/systemone
  is used as-is. Health: GET /health (Kev) or GET / (laya-server, answers once the model is loaded).
"""
from __future__ import annotations

import logging
import uuid

import httpx

from . import events
from .config import Config

log = logging.getLogger("photosort.kev")

SYSTEMONE_PATH = "/v1/systemone"


class RuleBackend:
    name = "rule"

    def choice(self, state: dict, options: list[str], instructions: str = "",
               criteria: dict | None = None) -> tuple[str, dict, float]:
        # Only one Choice is asked today: home burst -> occasion / busy_day
        if set(options) == {"occasion", "busy_day"}:
            n = state.get("photos", 0)
            ratio = state.get("burst_ratio", 1.0)     # photos vs baseline*factor threshold
            devices = state.get("devices", 1)
            score = 0.35 + min(ratio - 1, 2) * 0.15 + (0.2 if devices > 1 else 0) + (0.1 if n >= 30 else 0)
            p = max(0.05, min(0.95, score))
            probs = {"occasion": p, "busy_day": 1 - p}
        else:
            probs = {o: 1 / len(options) for o in options}
        best = max(probs, key=probs.get)
        return best, probs, probs[best]

    def noul(self, state: dict, question: str) -> tuple[float, float]:
        return 0.5, 0.0


class HttpBackend:
    name = "kev"

    def __init__(self, cfg: Config):
        base = cfg.kev_url.strip().rstrip("/")
        self.url = base if base.endswith(SYSTEMONE_PATH) else base + SYSTEMONE_PATH
        self.base = self.url[: -len(SYSTEMONE_PATH)]
        self.model = (cfg.kev_model or "").strip()
        self.timeout = cfg.kev_timeout_s

    def _ask(self, state: dict, question: dict) -> dict:
        payload: dict = {"state": _state_text(state), "questions": {"q": question}}
        if self.model:
            payload["model"] = self.model
        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(self.url, json=payload)
            r.raise_for_status()
            data = r.json()
        answers = data.get("answers") if isinstance(data, dict) else None
        if isinstance(answers, dict):
            ans = answers.get("q") or next(iter(answers.values()), None)
        elif isinstance(answers, list) and answers:       # other reproductions: list of answers
            ans = answers[0]
        else:
            ans = None
        if not isinstance(ans, dict):
            raise ValueError(f"no answer in System One response: {str(data)[:200]}")
        return ans

    def choice(self, state: dict, options: list[str], instructions: str = "",
               criteria: dict | None = None) -> tuple[str, dict, float]:
        crit = {o: (criteria or {}).get(o) for o in options}
        ans = self._ask(state, {"type": "choice", "instructions": instructions or "Pick the best option.",
                                "criteria": crit})
        probs = ans.get("probabilities") or {}
        if isinstance(probs, list):                       # some servers return a list aligned to options
            probs = dict(zip(options, probs))
        probs = {str(k): float(v) for k, v in probs.items()}
        best = ans.get("choice") or ans.get("answer") or (max(probs, key=probs.get) if probs else None)
        if best not in options:
            raise ValueError(f"System One answered {best!r}, not one of {options}")
        conf = ans.get("confidence")
        if conf is None:
            conf = probs.get(best, 0.0)
        return best, probs, float(conf)

    def noul(self, state: dict, question: str) -> tuple[float, float]:
        ans = self._ask(state, {"type": "noul", "instructions": question})
        p = ans.get("noul")
        if p is None:
            p = ans.get("probability")
        if p is None:
            probs = ans.get("probabilities") or {}
            p = probs.get("true", probs.get("yes", 0.5))
        p = float(p)
        conf = ans.get("confidence")
        return p, float(conf if conf is not None else max(p, 1 - p))

    def health(self) -> dict:
        last = "no response"
        with httpx.Client(timeout=3) as c:
            for path in ("/health", "/"):
                try:
                    r = c.get(self.base + path)
                except Exception as e:  # noqa: BLE001
                    last = str(e)
                    continue
                if r.status_code < 400:
                    return {"ok": True, "detail": f"HTTP {r.status_code} {path}"}
                last = f"HTTP {r.status_code} {path}"
        return {"ok": False, "detail": last}


def _state_text(state: dict) -> str:
    return "\n".join(f"{k}: {v}" for k, v in state.items())


class Decider:
    """Facade used by the pipeline: tries the System One server, falls back to rules, logs every decision."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rule = RuleBackend()
        self.http = HttpBackend(cfg) if cfg.kev_url.strip() else None

    def status(self) -> dict:
        if not self.http:
            return {"backend": "rule", "ok": True, "detail": "no kev_url configured"}
        h = self.http.health()
        return {"backend": "kev", "ok": h["ok"], "detail": f"{self.http.base}: {h['detail']}"}

    def choice(self, context: str, state: dict, options: list[str], instructions: str = "",
               criteria: dict | None = None) -> dict:
        by, best, probs, conf, error = self.rule.name, None, {}, 0.0, None
        if self.http:
            try:
                best, probs, conf = self.http.choice(state, options, instructions, criteria)
                by = self.http.name
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
                log.warning("System One choice failed, falling back to rules: %s", error)
        if best is None:
            best, probs, conf = self.rule.choice(state, options, instructions, criteria)
        did = uuid.uuid4().hex[:12]
        ev = {"id": did, "context": context, "by": by, "answer": best,
              "probs": {k: round(v, 3) for k, v in probs.items()}, "conf": round(conf, 3), "state": state}
        if error:
            ev["fallback_from"] = "kev"
            ev["error"] = error[:200]
        events.log("decision", **ev)
        return {"id": did, "by": by, "answer": best, "probs": probs, "conf": conf}
