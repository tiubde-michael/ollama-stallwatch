#!/usr/bin/env python3
"""Stack-Check TI-30 — ist die Maschine im Soll-Zustand?

Gedacht als Zwei-Minuten-Prüfung vor dem Betriebstag und nach jedem Eingriff.
Prüft nicht nur, ob Dienste laufen, sondern ob die Maschine tatsächlich
antwortet — ein Container "healthy" und ein `/api/tags` 200 sagen nichts
darüber, ob ein Modell noch lädt und Text erzeugt.

Bewusst enthalten, weil auf dieser Maschine dreimal ein konfigurierter Wert
still nicht eingehalten wurde (Kontext-Deckelung, NUM_PARALLEL, TEI
--auto-truncate): der Check liest die Soll-Werte NICHT nur aus der
Konfiguration, sondern misst nach, wo das billig geht.

    python3 tests/stack_check.py            # voller Lauf
    python3 tests/stack_check.py --schnell  # ohne Inferenz (kein Modell-Load)

Exit 0 = alles im Soll · 1 = mindestens eine harte Prüfung gerissen
       · 2 = nur weiche Hinweise.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMPOSE = "/srv/Container/docker-compose.yml"
REPO = "/srv/Container"
OLLAMA = "http://127.0.0.1:11434"
DASHBOARD = "http://127.0.0.1:3002"

CONTAINER = ["ollama", "openwebui", "tei-embed", "tei-rerank", "rerank-adapter"]
DIENSTE = ["ollama-gpu-logger", "ollama-log-parser", "ollama-process-logger",
           "ollama-stall-detector", "ollama-dashboard"]

# Modelle, die den Betrieb tragen. Klinisch = das Modell, dessen Antworten sich
# nicht ändern dürfen; agentisch = das Modell für Studio-/Agenten-Last.
MODELL_KLINISCH = "alibayram/medgemma:27b"
MODELL_AGENTISCH = "qwen3.6:35b-a3b-q4_K_M"

# Soll-Werte aus docker-compose.yml. Weichen sie ab, hat jemand die Maschine
# umkonfiguriert — unabhängig davon, ob das absichtlich war.
SOLL_ENV = {
    "OLLAMA_CONTEXT_LENGTH": "131072",
    "OLLAMA_NUM_PARALLEL": "2",
    "OLLAMA_MAX_LOADED_MODELS": "2",
    "OLLAMA_FLASH_ATTENTION": "1",
}

ERG: list[dict] = []


def pruefe(name: str, hart: bool, ok: bool, detail: str = "") -> bool:
    ERG.append({"name": name, "hart": hart, "ok": ok, "detail": detail})
    marke = "OK  " if ok else ("FAIL" if hart else "WARN")
    print(f"  [{marke}] {name}" + (f"  —  {detail}" if detail else ""))
    return ok


def sh(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:  # noqa: BLE001 — jede Störung ist hier ein Fail
        return 1, f"{type(e).__name__}: {e}"


def hole(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def poste(url: str, body: dict, timeout: float = 900.0):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()), time.time() - t0


# ------------------------------------------------------------------ Container
def check_container() -> None:
    print("\nContainer")
    if shutil.which("docker") is None:
        pruefe("docker vorhanden", True, False, "docker nicht im PATH")
        return
    rc, out = sh("docker", "ps", "--format", "{{.Names}}\t{{.Status}}")
    laufend = dict(z.split("\t", 1) for z in out.splitlines() if "\t" in z)
    for c in CONTAINER:
        st = laufend.get(c, "")
        pruefe(f"Container {c}", True, st.startswith("Up"), st or "läuft nicht")


def check_dienste() -> None:
    print("\nMonitoring-Dienste")
    for d in DIENSTE:
        rc, out = sh("systemctl", "is-active", d)
        pruefe(f"systemd {d}", True, out == "active", out)


# ------------------------------------------------------- Konfigurations-Drift
def check_konfig() -> None:
    print("\nKonfiguration")
    rc, out = sh("git", "-C", REPO, "status", "--porcelain", "docker-compose.yml")
    pruefe("docker-compose.yml unverändert gegenüber git", True, out == "",
           "lokal geändert: " + out if out else "sauber")

    try:
        text = open(COMPOSE, encoding="utf-8").read()
    except OSError as e:
        pruefe("docker-compose.yml lesbar", True, False, str(e))
        return
    for schluessel, soll in SOLL_ENV.items():
        ist = ""
        for zeile in text.splitlines():
            if zeile.strip().startswith(schluessel + ":"):
                ist = zeile.split(":", 1)[1].strip().strip('"')
                break
        pruefe(f"{schluessel} = {soll}", True, ist == soll, f"ist: {ist or 'fehlt'}")


# --------------------------------------------------------------------- Ollama
def check_ollama(schnell: bool) -> None:
    print("\nOllama")
    try:
        tags = hole(OLLAMA + "/api/tags")
    except Exception as e:  # noqa: BLE001
        pruefe("Ollama erreichbar", True, False, f"{type(e).__name__}: {e}")
        return
    namen = {m["name"] for m in tags.get("models", [])}
    pruefe("Ollama erreichbar", True, True, f"{len(namen)} Modelle")
    pruefe(f"Modell vorhanden: {MODELL_KLINISCH}", True, MODELL_KLINISCH in namen)
    pruefe(f"Modell vorhanden: {MODELL_AGENTISCH}", True, MODELL_AGENTISCH in namen)

    if schnell:
        print("       (--schnell: Inferenz übersprungen)")
        return

    # Der eigentliche Punkt: erzeugt die Maschine Text? Ein Modell-Kaltstart
    # kostet hier gemessen ~92 s, deshalb ist das Timeout großzügig.
    body = {"model": MODELL_KLINISCH, "stream": False, "think": False,
            "messages": [{"role": "user", "content": "Antworte mit genau einem Wort: bereit"}],
            "options": {"num_predict": 12, "temperature": 0}}
    try:
        d, w = poste(OLLAMA + "/api/chat", body)
    except Exception as e:  # noqa: BLE001
        pruefe("klinisches Modell antwortet", True, False, f"{type(e).__name__}: {e}")
        return
    inhalt = ((d.get("message") or {}).get("content") or "").strip()
    pruefe("klinisches Modell antwortet", True, bool(inhalt),
           f"{w:.1f}s, {len(inhalt)} Zeichen: {inhalt[:40]!r}")
    # Kaltstart ist kein Fehler, aber der Grund, warum der erste Request des
    # Tages langsam ist — hier sichtbar machen statt später rätseln.
    pruefe("Antwortzeit unter 30 s (sonst Kaltstart)", False, w < 30, f"{w:.1f}s")


# ----------------------------------------------------------------- Monitoring
def check_monitoring() -> None:
    print("\nMonitoring")
    try:
        h = hole(DASHBOARD + "/api/health", timeout=5)
        pruefe("Dashboard-API", True, bool(h.get("ok")), f"host={h.get('host')}")
    except Exception as e:  # noqa: BLE001
        pruefe("Dashboard-API", True, False, f"{type(e).__name__}: {e}")
        return
    try:
        g = hole(DASHBOARD + "/api/gpu/live", timeout=15)
        gpus = g.get("gpus", [])
        pruefe("GPUs sichtbar", True, len(gpus) >= 1,
               ", ".join(f"GPU{x['gpu_id']} {x['mem_used_mib']}/{x['mem_total_mib']} MiB"
                         for x in gpus))
    except Exception as e:  # noqa: BLE001
        pruefe("GPUs sichtbar", True, False, f"{type(e).__name__}: {e}")

    # Schreibt der Log-Parser? Wenn der Inferenz-Check oben lief, muss in den
    # letzten Minuten eine Zeile entstanden sein.
    try:
        r = hole(DASHBOARD + "/api/requests?limit=1", timeout=10)
        items = r.get("items", [])
        letzte = items[0]["timestamp"] if items else "keine"
        pruefe("Request-Log wird geschrieben", False, bool(items),
               f"jüngste Zeile: {letzte}")
    except Exception as e:  # noqa: BLE001
        pruefe("Request-Log wird geschrieben", False, False, f"{type(e).__name__}: {e}")


def check_platte() -> None:
    print("\nSystem")
    rc, out = sh("df", "--output=pcent,avail", "-h", REPO)
    zeile = out.splitlines()[-1].strip() if out else ""
    belegt = int(zeile.split("%")[0]) if "%" in zeile else 0
    pruefe("Plattenplatz unter 85 %", False, belegt < 85, zeile)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schnell", action="store_true",
                    help="ohne Inferenz-Test (löst keinen Modell-Load aus)")
    a = ap.parse_args()

    print(f"### Stack-Check TI-30 — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    check_container()
    check_dienste()
    check_konfig()
    check_ollama(a.schnell)
    check_monitoring()
    check_platte()

    hart = [e for e in ERG if e["hart"] and not e["ok"]]
    weich = [e for e in ERG if not e["hart"] and not e["ok"]]
    print("\n" + "=" * 66)
    print(f"{sum(1 for e in ERG if e['ok'])}/{len(ERG)} im Soll | "
          f"gerissen: {len(hart)} | Hinweise: {len(weich)}")
    if hart:
        print("NICHT BETRIEBSBEREIT: " + ", ".join(e["name"] for e in hart))
        return 1
    if weich:
        print("betriebsbereit, mit Hinweisen: " + ", ".join(e["name"] for e in weich))
        return 2
    print("betriebsbereit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
