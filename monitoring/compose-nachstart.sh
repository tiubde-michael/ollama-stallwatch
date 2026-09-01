#!/usr/bin/env bash
# Holt den Container-Stack nach dem Boot nach — und NUR dann, wenn er fehlt.
#
# Warum es das gibt (fA-501 K2190):
#   Am 01.09.2026 lief ti-30 fuenf Stunden ohne einen einzigen Container. Vor dem
#   Abschalten war `docker compose down` gelaufen, das ENTFERNT die Container, und
#   `restart: unless-stopped` kann nichts starten, was es nicht mehr gibt. Das
#   Lastmanagement auf ti-nas-06 hat ti-30 daraufhin abgemeldet — zu Recht.
#   Das Tueckische: SSH, Tailscale und das Dashboard auf 3002 sind systemd-Units
#   und kamen von allein hoch. Von aussen sieht eine Maschine ohne Container aus
#   wie eine gesunde Maschine.
#
# Bauart uebernommen von gx10-01 (fA-507 K2012/K2025), wo dasselbe Thema zuerst
# auftrat und teuer bezahlt wurde. Vier Dinge davon sind hier eingebaut:
#   1. USER-Unit, kein Drop-in unter /etc/systemd/system/docker.service.d/.
#      Michaels Entscheid: `docker.service` bleibt im Auslieferungszustand.
#   2. Es wird auf die ADRESSE gewartet, nicht auf die Unit. Auf gx10-01 war
#      `tailscaled.service` um 12:07:30 bereits "Started", gebunden wurde erst um
#      12:07:34 — eine Ordnungsbeziehung auf die Unit haette nichts genuetzt.
#   3. FAIL-OPEN mit Grenze: laeuft eine Wartezeit ab, wird NICHTS angefasst und
#      der Grund landet im Journal. Ein Netzproblem darf kein Container-Karussell
#      ausloesen.
#   4. `--force-recreate`, wenn Container zwar laufen, aber ohne Port-Mapping.
#      Auf gx10-01 gemessen: ein blosses `up -d` zog den vorhandenen Container mit
#      `Ports: []` wieder hoch — ein Dienst, der auf gruen steht und nicht
#      erreichbar ist.
#
# Greift NICHT ein, wenn alles laeuft. Das ist der wichtigste Teil: kein blindes
# Neuanlegen bei jedem Boot, sonst zerlegt die Reparatur den gesunden Zustand.
#
#   bash monitoring/compose-nachstart.sh            # normal (die Unit ruft das)
#   bash monitoring/compose-nachstart.sh --pruefen  # nur berichten, nichts tun
#
# Exit 0 immer, wenn nichts kaputtgemacht wurde — auch beim Aufgeben. Diese Unit
# darf keinen Boot rot faerben.

set -uo pipefail
cd /srv/Container || exit 0

PRUEFEN=0
[ "${1:-}" = "--pruefen" ] && PRUEFEN=1

log() { printf '[compose-nachstart] %s\n' "$*"; }

WARTE_DOCKER=90     # s, bis der Daemon ansprechbar ist
WARTE_ADRESSE=60    # s, bis eine feste Bind-Adresse am Interface haengt
WARTE_PORTS=180     # s, bis die Ports nach dem Start tatsaechlich horchen

# ---------------------------------------------------------------- 1. Daemon
for _ in $(seq 1 "$WARTE_DOCKER"); do
  docker info >/dev/null 2>&1 && break
  sleep 1
done
if ! docker info >/dev/null 2>&1; then
  log "Docker-Daemon nach ${WARTE_DOCKER}s nicht ansprechbar — nichts angefasst."
  exit 0
fi

# ---------------------------------------------------------------- 2. Adresse
# Nur relevant, falls BIND_IP jemals von der Wildcard weg auf eine feste Adresse
# gestellt wird (heute: 0.0.0.0, deshalb kein Rennen). Steht dort eine konkrete
# Adresse, muss sie am Interface HAENGEN, bevor Docker die Ports anlegt —
# sonst: "cannot assign requested address", und der Container startet gar nicht.
BIND=$(sed -n 's/^BIND_IP=//p' .env | head -1 | tr -d '"'\''[:space:]')
if [ -n "$BIND" ] && [ "$BIND" != "0.0.0.0" ]; then
  for _ in $(seq 1 "$WARTE_ADRESSE"); do
    ip -4 -o addr show 2>/dev/null | grep -q "inet ${BIND}/" && break
    sleep 1
  done
  if ! ip -4 -o addr show 2>/dev/null | grep -q "inet ${BIND}/"; then
    log "BIND_IP=${BIND} haengt nach ${WARTE_ADRESSE}s an keinem Interface — nichts angefasst."
    exit 0
  fi
  log "BIND_IP=${BIND} ist da."
fi

# ---------------------------------------------------------------- 3. Zustand
# Zwei Fragen, weil es zwei verschiedene Ausfaelle gibt:
#   (a) laeuft fuer jeden Dienst ein Container?      -> fehlt er, wurde er entfernt
#   (b) horcht jeder deklarierte Host-Port?          -> faengt "gruen ohne Mapping"
DIENSTE=$(docker compose config --services 2>/dev/null)
PORTS=$(docker compose config --format json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
gesehen = set()
for s in d.get("services", {}).values():
    for p in (s.get("ports") or []):
        if p.get("published"):
            gesehen.add(str(p["published"]))
print(" ".join(sorted(gesehen)))
')

if [ -z "$DIENSTE" ]; then
  log "docker compose config liefert keine Dienste — falsches Verzeichnis oder kaputte Datei. Nichts angefasst."
  exit 0
fi

fehlend=""; nicht_laufend=""
for d in $DIENSTE; do
  cid=$(docker compose ps -q "$d" 2>/dev/null)
  if [ -z "$cid" ]; then
    fehlend="$fehlend $d"
  else
    st=$(docker inspect "$cid" --format '{{.State.Status}}' 2>/dev/null)
    [ "$st" = "running" ] || nicht_laufend="$nicht_laufend $d($st)"
  fi
done

stille_ports=""
for p in $PORTS; do
  ss -ltn 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" || stille_ports="$stille_ports $p"
done

# ---------------------------------------------------------------- 4. Urteil
if [ -z "$fehlend" ] && [ -z "$nicht_laufend" ] && [ -z "$stille_ports" ]; then
  log "Stack vollstaendig (Dienste: $(echo $DIENSTE | wc -w), Ports:$(echo " $PORTS")) — nichts zu tun."
  exit 0
fi

[ -n "$fehlend" ]      && log "FEHLENDE Container:$fehlend"
[ -n "$nicht_laufend" ] && log "nicht laufend:$nicht_laufend"
[ -n "$stille_ports" ] && log "Port deklariert, horcht aber nicht:$stille_ports"

if [ "$PRUEFEN" = "1" ]; then
  log "--pruefen: hier wuerde nachgeholt. Nichts angefasst."
  exit 0
fi

# Container existieren, aber ohne Mapping -> neu anlegen. Sonst reicht `up -d`.
# Die Unterscheidung stammt aus der Messung auf gx10-01 (fA-507 K2012).
if [ -z "$fehlend" ] && [ -n "$stille_ports" ]; then
  log "hole nach: docker compose up -d --force-recreate"
  docker compose up -d --force-recreate 2>&1 | sed 's/^/    /'
else
  log "hole nach: docker compose up -d"
  docker compose up -d 2>&1 | sed 's/^/    /'
fi

# ---------------------------------------------------------------- 5. Gegenprobe
# Ohne die waere es ein Skript, das behauptet statt zu belegen. Die TEI-Container
# brauchen auf CPU spuerbar Anlauf — am 01.09. gemessen: :8001 nach ~1 min,
# :8082 nach ~3 min. Deshalb WARTE_PORTS grosszuegig.
for _ in $(seq 1 "$WARTE_PORTS"); do
  offen=1
  for p in $PORTS; do
    ss -ltn 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" || { offen=0; break; }
  done
  [ "$offen" = "1" ] && break
  sleep 1
done

rest=""
for p in $PORTS; do
  ss -ltn 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" || rest="$rest $p"
done
if [ -z "$rest" ]; then
  log "Ergebnis: alle Ports horchen ($PORTS)."
else
  log "Ergebnis: NACH ${WARTE_PORTS}s horchen diese Ports weiterhin nicht:$rest"
fi
exit 0
