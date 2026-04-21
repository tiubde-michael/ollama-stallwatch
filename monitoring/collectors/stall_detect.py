#!/usr/bin/env python3
"""Stall detector — flags GPU-loaded but idle while ollama serve is hot.

Trigger condition (per GPU, per poll):
  vram_used_mib > vram_min_mib
  AND utilization_gpu <= util_max_percent
  AND power_draw_w  <= power_max_watts
  AND ollama serve cpu_percent >= ollama_serve_cpu_min_percent

When this is true for `consecutive_polls_required` polls, we record a stall_event
and dump diagnostic stacks (gdb + /proc kernel stacks) to disk. Cooldown prevents
flooding while a single hang continues.
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import CONFIG, connect

CONTAINER = CONFIG["ollama"]["container_name"]
POLL_INTERVAL = CONFIG["stall"]["poll_interval_sec"]
VRAM_MIN = CONFIG["stall"]["vram_min_mib"]
UTIL_MAX = CONFIG["stall"]["util_max_percent"]
POWER_MAX = CONFIG["stall"]["power_max_watts"]
SERVE_CPU_MIN = CONFIG["stall"]["ollama_serve_cpu_min_percent"]
NEEDED = CONFIG["stall"]["consecutive_polls_required"]
COOLDOWN = CONFIG["stall"]["cooldown_sec"]
CAPTURE_GDB = CONFIG["stall"]["capture_gdb"]
CAPTURE_PROC = CONFIG["stall"]["capture_proc_stacks"]
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
        out.append({
            "gpu_id": int(parts[0]),
            "vram": int(parts[1]),
            "util": int(parts[2]),
            "power": float(parts[3]),
        })
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
        return int(rest[11]) + int(rest[12])  # utime + stime
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
    """Best-effort: return name of model currently in VRAM."""
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
    """Heuristic: was last GIN line in past 60s a still-open POST?"""
    try:
        r = subprocess.run(
            ["docker", "logs", "--tail", "200", "--since", "60s", CONTAINER],
            capture_output=True, text=True, timeout=5
        )
        # If a POST /api/generate or /api/chat appears WITHOUT a status code line
        # in the recent window, assume one is still in flight.
        # Simple proxy: count completion request lines vs final GIN POST lines.
        text = r.stdout + r.stderr
        completions = text.count('msg="completion request"')
        finishes = text.count('POST     "/api/generate"') + text.count('POST     "/api/chat"')
        return 1 if completions > finishes else 0
    except Exception:
        return None


def capture_stacks(serve_pid):
    """Dump diagnostic info for serve_pid + all its threads. Returns file path."""
    STALLS_DIR.mkdir(exist_ok=True)
    fname = f"{now_filename()}_pid{serve_pid}.txt"
    path = STALLS_DIR / fname
    with open(path, "w") as out:
        out.write(f"# Stall capture {now_iso()} pid={serve_pid}\n\n")

        # Per-thread state, wchan, time-on-cpu (helps identify hot vs sleeping)
        out.write("## Threads (/proc/<tid>/{stat,wchan,status})\n\n")
        task_dir = Path(f"/proc/{serve_pid}/task")
        if task_dir.exists():
            for tid_dir in sorted(task_dir.iterdir(), key=lambda p: int(p.name)):
                tid = tid_dir.name
                try:
                    stat = (tid_dir / "stat").read_text()
                    rest = stat[stat.rindex(")") + 2:].split()
                    state = rest[0]
                    utime = int(rest[11])
                    stime = int(rest[12])
                    wchan = (tid_dir / "wchan").read_text().strip() or "0"
                    out.write(f"  TID {tid:>8}  state={state}  "
                              f"cpu_ticks={utime + stime:>10}  wchan={wchan}\n")
                except Exception as e:
                    out.write(f"  TID {tid}  read-failed: {e}\n")

        if CAPTURE_PROC:
            out.write("\n## Kernel stacks (/proc/<tid>/stack, requires CAP_SYS_ADMIN)\n\n")
            try:
                # Single sudo cat call to avoid N forks
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
            out.write("\n## gdb backtrace (symbols are stripped — addresses only)\n\n")
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
    counters = {}        # gpu_id -> consecutive matches
    last_stall_ts = 0.0  # cooldown
    open_stall_id = None # row id of currently-active stall to close

    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"stall_detect: poll={POLL_INTERVAL}s threshold={NEEDED} polls "
          f"(vram>{VRAM_MIN}MiB util<={UTIL_MAX}% power<={POWER_MAX}W "
          f"serve_cpu>={SERVE_CPU_MIN}%)", file=sys.stderr)

    prev_serve_ticks = None
    prev_t = None

    while running:
        try:
            serve_pid = get_serve_pid()
            now_t = time.monotonic()

            # Compute serve CPU% over last interval
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
            stalled_now = False

            for g in gpus:
                gid = g["gpu_id"]
                cond = (g["vram"] > VRAM_MIN
                        and g["util"] <= UTIL_MAX
                        and g["power"] <= POWER_MAX
                        and serve_cpu >= SERVE_CPU_MIN)
                if cond:
                    counters[gid] = counters.get(gid, 0) + 1
                else:
                    counters[gid] = 0

                if counters[gid] >= NEEDED:
                    stalled_now = True
                    # Open a stall_event if cooldown elapsed and none open
                    if open_stall_id is None and (now_t - last_stall_ts) > COOLDOWN:
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
                            "request_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (now_iso(), gid, g["vram"], round(serve_cpu, 1), rss,
                             model, str(path) if path else None, active)
                        )
                        open_stall_id = cur.lastrowid
                        last_stall_ts = now_t
                        print(f"stall_detect: STALL DETECTED gpu={gid} vram={g['vram']}MiB "
                              f"util={g['util']}% serve_cpu={serve_cpu:.0f}% "
                              f"stack={path}", file=sys.stderr)

            # Close stall when no GPU still meets threshold
            if not stalled_now and open_stall_id is not None:
                conn.execute(
                    "UPDATE stall_events SET end_ts=? WHERE id=?",
                    (now_iso(), open_stall_id)
                )
                print(f"stall_detect: stall #{open_stall_id} ended", file=sys.stderr)
                open_stall_id = None

        except Exception as e:
            print(f"stall_detect error: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
