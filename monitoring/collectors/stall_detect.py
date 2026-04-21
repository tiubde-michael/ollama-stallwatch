#!/usr/bin/env python3
"""Stall detector — flags GPU-loaded but idle while ollama serve is hot.

Runs two parallel criteria:

  STRICT: vram>min AND util<=max AND power<=strict_max AND serve_cpu>=min
          for `strict_consecutive_polls` polls in a row.

  LOOSE:  vram>min AND util<=max AND power<=loose_max AND serve_cpu>=min
          for at least `loose_window_match_fraction` of the last
          `loose_window_polls` polls (sliding window).

When either fires, a stall_event row is opened with `confidence` set to
'strict' or 'loose'. If a 'loose' event later sees a strict match, the
event's confidence is upgraded to 'strict' in place. Stack capture runs
once per event regardless of which criterion opened it.
"""

import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

CONTAINER = CONFIG["ollama"]["container_name"]
S = CONFIG["stall"]
POLL_INTERVAL = S["poll_interval_sec"]
VRAM_MIN = S["vram_min_mib"]
UTIL_MAX = S["util_max_percent"]
SERVE_CPU_MIN = S["ollama_serve_cpu_min_percent"]
STRICT_POWER_MAX = S["strict_power_max_watts"]
STRICT_NEEDED = S["strict_consecutive_polls"]
LOOSE_POWER_MAX = S["loose_power_max_watts"]
LOOSE_WINDOW = S["loose_window_polls"]
LOOSE_FRAC = S["loose_window_match_fraction"]
COOLDOWN = S["cooldown_sec"]
CAPTURE_GDB = S["capture_gdb"]
CAPTURE_PROC = S["capture_proc_stacks"]
GDB = CONFIG["gdb_path"]
SUDO = CONFIG["sudo_path"]
STALLS_DIR = CONFIG["stalls_dir"]
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_filename():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def query_nvidia_smi():
    r = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=index,memory.used,utilization.gpu,power.draw",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        out.append({"gpu_id": int(parts[0]), "vram": int(parts[1]),
                    "util": int(parts[2]), "power": float(parts[3])})
    return out


def get_serve_pid():
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Pid}}", CONTAINER],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    try:
        pid = int(r.stdout.strip())
        return pid if pid > 0 else None
    except ValueError:
        return None


def read_proc_stat_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        rest = data[data.rindex(")") + 2:].split()
        return int(rest[11]) + int(rest[12])
    except Exception:
        return None


def read_proc_rss_mib(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return None


def get_loaded_model():
    try:
        import urllib.request, json
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=2) as r:
            data = json.load(r)
        models = data.get("models") or []
        if models:
            return models[0].get("name")
    except Exception:
        return None
    return None


def has_active_request():
    try:
        r = subprocess.run(
            ["docker", "logs", "--tail", "200", "--since", "60s", CONTAINER],
            capture_output=True, text=True, timeout=5
        )
        text = r.stdout + r.stderr
        completions = text.count('msg="completion request"')
        finishes = text.count('POST     "/api/generate"') + text.count('POST     "/api/chat"')
        return 1 if completions > finishes else 0
    except Exception:
        return None


def capture_stacks(serve_pid):
    STALLS_DIR.mkdir(exist_ok=True)
    fname = f"{now_filename()}_pid{serve_pid}.txt"
    path = STALLS_DIR / fname
    with open(path, "w") as out:
        out.write(f"# Stall capture {now_iso()} pid={serve_pid}\n\n")
        out.write("## Threads (/proc/<tid>/{stat,wchan,status})\n\n")
        task_dir = Path(f"/proc/{serve_pid}/task")
        if task_dir.exists():
            for tid_dir in sorted(task_dir.iterdir(), key=lambda p: int(p.name)):
                tid = tid_dir.name
                try:
                    stat = (tid_dir / "stat").read_text()
                    rest = stat[stat.rindex(")") + 2:].split()
                    state = rest[0]
                    utime = int(rest[11]); stime = int(rest[12])
                    wchan = (tid_dir / "wchan").read_text().strip() or "0"
                    out.write(f"  TID {tid:>8}  state={state}  "
                              f"cpu_ticks={utime + stime:>10}  wchan={wchan}\n")
                except Exception as e:
                    out.write(f"  TID {tid}  read-failed: {e}\n")

        if CAPTURE_PROC:
            out.write("\n## Kernel stacks (/proc/<tid>/stack)\n\n")
            try:
                tid_paths = [str(p / "stack") for p in task_dir.iterdir()]
                if tid_paths:
                    r = subprocess.run(
                        [SUDO, "head", "-50"] + tid_paths,
                        capture_output=True, text=True, timeout=10
                    )
                    out.write(r.stdout)
                    if r.stderr:
                        out.write(f"\n[stderr]\n{r.stderr}\n")
            except Exception as e:
                out.write(f"  failed: {e}\n")

        if CAPTURE_GDB and Path(GDB).exists():
            out.write("\n## gdb backtrace (Go binary, symbols stripped)\n\n")
            try:
                r = subprocess.run(
                    [SUDO, "timeout", "15", GDB, "-batch", "-nx",
                     "-ex", "set pagination off",
                     "-ex", "thread apply all bt",
                     "-p", str(serve_pid)],
                    capture_output=True, text=True, timeout=20
                )
                out.write(r.stdout)
                if r.stderr:
                    out.write(f"\n[stderr]\n{r.stderr}\n")
            except Exception as e:
                out.write(f"  failed: {e}\n")
    return path


def main():
    conn = connect()

    strict_count = defaultdict(int)
    loose_window = defaultdict(lambda: deque(maxlen=LOOSE_WINDOW))
    open_event_id = None
    open_event_confidence = None
    last_close_t = 0.0
    prev_serve_ticks = None
    prev_t = None

    running = True
    def shutdown(signum, frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"stall_detect: poll={POLL_INTERVAL}s "
          f"strict=({STRICT_NEEDED}*{POLL_INTERVAL}s, power<={STRICT_POWER_MAX}W) "
          f"loose=({int(LOOSE_FRAC*100)}% of {LOOSE_WINDOW}*{POLL_INTERVAL}s, "
          f"power<={LOOSE_POWER_MAX}W) "
          f"vram>{VRAM_MIN}MiB util<={UTIL_MAX}% serve_cpu>={SERVE_CPU_MIN}%",
          file=sys.stderr)

    while running:
        try:
            serve_pid = get_serve_pid()
            now_t = time.monotonic()

            serve_cpu = 0.0
            if serve_pid is not None:
                ticks = read_proc_stat_ticks(serve_pid)
                if ticks is not None and prev_serve_ticks is not None and prev_t is not None:
                    dt = now_t - prev_t
                    if dt > 0:
                        serve_cpu = ((ticks - prev_serve_ticks) / CLK_TCK / dt) * 100
                prev_serve_ticks = ticks
                prev_t = now_t

            gpus = query_nvidia_smi()
            any_strict = False
            any_loose = False
            trigger_gpu = None
            trigger_vram = None

            for g in gpus:
                gid = g["gpu_id"]
                base = (g["vram"] > VRAM_MIN
                        and g["util"] <= UTIL_MAX
                        and serve_cpu >= SERVE_CPU_MIN)
                strict_match = base and g["power"] <= STRICT_POWER_MAX
                loose_match = base and g["power"] <= LOOSE_POWER_MAX

                strict_count[gid] = strict_count[gid] + 1 if strict_match else 0
                loose_window[gid].append(1 if loose_match else 0)

                if strict_count[gid] >= STRICT_NEEDED:
                    any_strict = True
                    if trigger_gpu is None:
                        trigger_gpu, trigger_vram = gid, g["vram"]
                if (len(loose_window[gid]) == LOOSE_WINDOW
                        and sum(loose_window[gid]) / LOOSE_WINDOW >= LOOSE_FRAC):
                    any_loose = True
                    if trigger_gpu is None:
                        trigger_gpu, trigger_vram = gid, g["vram"]

            stalled_now = any_strict or any_loose

            # State machine: open / upgrade / close
            if stalled_now and open_event_id is None and (now_t - last_close_t) > COOLDOWN:
                conf = "strict" if any_strict else "loose"
                rss = read_proc_rss_mib(serve_pid) if serve_pid else None
                model = get_loaded_model()
                active = has_active_request()
                path = None
                try:
                    path = capture_stacks(serve_pid) if serve_pid else None
                except Exception as e:
                    print(f"capture failed: {e}", file=sys.stderr)
                cur = conn.execute(
                    "INSERT INTO stall_events (start_ts, gpu_id, vram_used_mib, "
                    "ollama_serve_cpu, ollama_serve_rss_mib, model, stack_path, "
                    "request_active, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (now_iso(), trigger_gpu, trigger_vram, round(serve_cpu, 1), rss,
                     model, str(path) if path else None, active, conf)
                )
                open_event_id = cur.lastrowid
                open_event_confidence = conf
                print(f"stall_detect: STALL #{open_event_id} ({conf}) gpu={trigger_gpu} "
                      f"vram={trigger_vram}MiB serve_cpu={serve_cpu:.0f}% stack={path}",
                      file=sys.stderr)

            elif open_event_id is not None and any_strict and open_event_confidence != "strict":
                # Upgrade loose → strict
                conn.execute(
                    "UPDATE stall_events SET confidence='strict' WHERE id=?",
                    (open_event_id,)
                )
                open_event_confidence = "strict"
                print(f"stall_detect: stall #{open_event_id} upgraded to STRICT",
                      file=sys.stderr)

            elif not stalled_now and open_event_id is not None:
                conn.execute(
                    "UPDATE stall_events SET end_ts=? WHERE id=?",
                    (now_iso(), open_event_id)
                )
                print(f"stall_detect: stall #{open_event_id} ({open_event_confidence}) ended",
                      file=sys.stderr)
                open_event_id = None
                open_event_confidence = None
                last_close_t = now_t

        except Exception as e:
            print(f"stall_detect error: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
