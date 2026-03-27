# V100 SXM2 Setup & Diagnose — Session 12.02.2026

## System

| Komponente | Details |
|---|---|
| Motherboard | ASUS PRIME B860-PLUS WIFI |
| CPU | Intel Core Ultra 5 225F |
| GPU | NVIDIA Tesla V100 SXM2 32GB (auf PCIe-Adapter vormontiert) |
| GPU UUID | GPU-9105749b-3678-9faf-6cfb-77106c1a3aa0 |
| GPU Serial | 1563320010751 |
| NVIDIA Driver | 580.126.09 (proprietäre Kernel-Module, nicht open) |
| CUDA | 13.0 |
| OS | Ubuntu 24.04, Kernel 6.8.0-100-generic |
| Gehäuse | Gaming PC Tower (liegend) |

## Was wurde gemacht

### 1. Driver-Wechsel: open → proprietary
- **Problem**: V100 (Volta) wird von nvidia-open Kernel-Modulen nicht unterstützt
- **Lösung**: `nvidia-dkms-580-open` → `nvidia-dkms-580` (proprietär) gewechselt
- **Hinweis**: RTX 5060 Ti braucht open-Module → beide GPUs können nicht gleichzeitig unter einem Treiber laufen
- **Entscheidung**: RTX 5060 Ti ausgebaut, System läuft headless nur mit V100

### 2. GPU-Pinning in Docker
- `docker-compose.yml`: `NVIDIA_VISIBLE_DEVICES` auf UUID der V100 gesetzt
- `.env`: `GPU_UUID` aktualisiert
- Ollama Container sieht nur die V100

### 3. Cooling / Lüftersteuerung
- **Problem**: V100 SXM2 hat keinen eigenen Lüfter (passiv gekühlt)
- **Idle-Temp ohne Steuerung**: 77-82°C (viel zu hoch)
- **Lösung**: Lüfter umgebaut + Fan-Control-Service eingerichtet

#### Lüfter-Zuordnung (nct6799 Super I/O, hwmon dynamisch):
| Fan-Header | Physischer Lüfter |
|---|---|
| Fan 2 | CPU-Kühler (~1800 RPM max) |
| Fan 3 | BOOSTBOXX Controller HUB ARGB — PWM-Eingang (steuert Hub-Lüfter) |
| Fan 4 | **V100-Lüfter — nah an der Karte** (~1300 RPM max) |
| Fan 5 | **V100-Lüfter — weiter entfernt** (~1200 RPM max) |

#### Lüfter-Umbau:
- Hinterer Gehäuselüfter (war Exhaust hinter CPU, hat V100-Airflow umgangen) → **umgedreht auf Intake**
- Fan 4 + Fan 5 direkt an Motherboard-Header (statt über BOOSTBOXX Hub)
- Fan 3 → PWM-Eingang des BOOSTBOXX Hubs

#### Fan-Control-Service (`gpu-fan-control.service`):
- Script: `/srv/Container/gpu-fan-control.sh`
- Findet nct6799 hwmon-Pfad dynamisch (überlebt hwmon-Nummerierungsänderungen)
- Steuert Fan 3, 4, 5 basierend auf GPU-Temperatur:
  - ≤40°C: PWM 60/255 (~24%, leise)
  - 40-60°C: linearer Anstieg
  - ≥60°C: PWM 255/255 (100%)
  - GPU nicht erreichbar: 100% (Sicherheit)
- `nct6775` Modul wird via `/etc/modules-load.d/nct6775.conf` bei Boot geladen

### 4. Power Limit
- **Problem**: V100 TDP 300W → 83°C Thermal Throttling mit aktueller Kühlung
- **Stress-Test Ergebnis bei 300W**: Erreicht 83°C, throttled auf ~100W
- **Lösung**: Power Limit auf 150W gesetzt
- Service: `nvidia-powerlimit.service` (setzt 150W + Persistence Mode bei Boot)

### 5. PCIe-Stabilität (Kernel-Parameter)
Gesetzt in `/etc/default/grub` → `GRUB_CMDLINE_LINUX_DEFAULT`:
```
pcie_aspm=off              # PCIe Power Saving aus (verhindert Link-Drops)
pci=noaer                  # Advanced Error Reporting aus (verhindert Resets bei marginalen Links)
reboot=acpi                # ACPI-Reboot-Methode (verhindert Hänger nach GPU-Crash)
nvidia.NVreg_EnablePCIeGen3=0  # Versuch Gen2 zu erzwingen (vom Treiber ignoriert)
```

### 6. Systemd-Services erstellt
| Service | Funktion |
|---|---|
| `gpu-fan-control.service` | GPU-temperaturbasierte Lüftersteuerung |
| `nvidia-powerlimit.service` | 150W Power Limit + Persistence Mode bei Boot |

---

## Offenes Problem: Xid 79 — GPU fällt vom Bus

### Symptom
Die V100 verschwindet nach 2-50 Minuten mit Kernel-Fehler:
```
NVRM: Xid (PCI:0000:02:00): 79, GPU has fallen off the bus.
NVRM: Xid (PCI:0000:02:00): 154, GPU recovery action changed from 0x0 (None) to 0x1 (GPU Reset Required)
```

Nach dem Crash zeigt `lspci`: `!!! Unknown header type 7f` (korruptes PCI Config Space).

### Crash-Verlauf
| Boot | Stabil für | Kernel-Params aktiv |
|---|---|---|
| Boot 1 | ~20 min | Nein |
| Boot 2 | GPU nicht gefunden (Post-Crash, brauchte Power Cycle) | Nein |
| Boot 3 | ~2.5 min | Nein |
| Boot 4 | **~50 min** | Ja (pcie_aspm=off, pci=noaer) |

Kernel-Parameter haben geholfen (50 statt 2-20 Min), aber das Problem bleibt.

### PCIe-Link-Analyse
```
V100 Endpoint:  LnkCap x16 Gen3 → LnkSta x8 Gen3 (downgraded!)
Root Port:      LnkCap x16 Gen5 → LnkSta x8 Gen3
```
- 8 von 16 Lanes funktionieren nicht → Adapter routet nur 8 Lanes oder 8 Lanes haben schlechte Signalqualität
- Kein PCIe Replay-Counter erhöht (Replays = 0), Crash kommt ohne Vorwarnung

### ECC / Hardware-Fehler
- ECC Errors (corrected + uncorrected): **0**
- Retired Pages: **keine**
- InfoROM: OEM 1.1, ECC 5.0, Power N/A
→ GPU-Speicher und GPU selbst sind in Ordnung

### Ausgeschlossene Ursachen
- **Thermisch**: Crash passiert auch bei 31°C idle
- **Stromversorgung**: 2x 8-Pin über Y-Kabel → 300W+ verfügbar
- **Software/Treiber**: Crash passiert bei jeder Aktivität (nvtop, nvidia-smi)
- **ECC/VRAM-Fehler**: Alle Zähler auf 0

### Wahrscheinlichste Ursache
**Der SXM2-to-PCIe Adapter** (vormontiert, China-Produktion):
- Nur x8 statt x16 → 8 Lanes haben schlechte Signalführung
- PCIe Gen3 (8 GT/s) ist für den Adapter zu schnell
- Nach zufälliger Zeit akkumulieren sich Signalprobleme → Link-Failure

### Noch zu testen
1. **PCIe Gen2 im BIOS erzwingen** (ASUS BIOS → Advanced → PCI Subsystem Settings → PCIe Link Speed → Gen2/5.0 GT/s) — halbiert die Signalfrequenz, deutlich toleranter
2. **V100 vom Adapter trennen und Adapter inspizieren**:
   - SXM2-Sockel: V100 fest und gleichmäßig? Alle Schrauben?
   - Lötstellen am PCIe-Goldfinger: kalt/matt/rissig?
   - Kondensatoren auf dem Adapter: alle vorhanden und fest?
   - PCIe-Kontakte: Kratzer, Oxidation?
3. **Adapter austauschen** — qualitativ besserer SXM2-to-PCIe Adapter
4. **V100 PCIe-Version kaufen** statt SXM2 (hat native PCIe-Goldfinger, braucht keinen Adapter)

### Reboot-Problem
System hängt beim Reboot (SSH trennt, aber kein Neustart — Power-Button nötig).
- `reboot=acpi` Kernel-Parameter gesetzt, hat nicht geholfen
- Ursache: NVIDIA-Treiber im Crash-Zustand blockiert den ACPI-Shutdown
- Workaround: `sudo shutdown -h now` statt `sudo reboot` (Power Off statt Reboot, dann manuell einschalten)

---

## Konfigurationsdateien

### /etc/default/grub
```
GRUB_CMDLINE_LINUX_DEFAULT="pcie_aspm=off pci=noaer reboot=acpi nvidia.NVreg_EnablePCIeGen3=0"
```

### /etc/modules-load.d/nct6775.conf
```
nct6775
```

### /etc/sudoers.d/ollama_admin
```
ollama_admin ALL=(ALL) NOPASSWD:ALL
```

### Systemd Services
- `/etc/systemd/system/gpu-fan-control.service`
- `/etc/systemd/system/nvidia-powerlimit.service`

### Docker
- `/srv/Container/docker-compose.yml` — V100 per UUID gepinnt
- `/srv/Container/.env` — GPU_UUID gesetzt

### Fan Control Script
- `/srv/Container/gpu-fan-control.sh`
