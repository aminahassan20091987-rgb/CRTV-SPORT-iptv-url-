import os
import re
import time
import shutil
import threading
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, unquote

# ════════════════════════════════════════════════════════════════════
# CONFIGURATION — TOUTES surchargeables via variables d'environnement
# ════════════════════════════════════════════════════════════════════
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

BASE         = Path("/tmp/novabladeiptv")
SOURCE_FILE  = Path("/app/source.txt")
EPG_DIR      = Path("/app/epg")
FFMPEG_LOG   = Path("/tmp/ffmpeg.log")
CONFIG_FILE  = Path("/app/config.json")   # overrides persistants

# ── Latence / stabilité (env > fichier > défaut) ────────────────────
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[config] {name}={raw!r} invalide, défaut {default}",
              flush=True)
        return default

SEGMENT_SECONDS     = _env_int("SEGMENT_SECONDS", 4)
PLAYLIST_SIZE       = _env_int("PLAYLIST_SIZE", 30)
DELETE_THRESHOLD    = _env_int("DELETE_THRESHOLD", 10)
START_DELAY_SECONDS = _env_int("START_DELAY_SECONDS", 8)
FPS_ESTIMATE        = 25
X264_TUNE           = os.environ.get("X264_TUNE", "zerolatency")

# Watchdog borné (jamais < 20s, jamais > 5× segment)
FFMPEG_STALL_SECONDS = max(20, 5 * SEGMENT_SECONDS)
FFMPEG_STARTUP_GRACE = _env_int("FFMPEG_STARTUP_GRACE", 60)

# KBS -500 kbps
KBS_REDUCTION_KBPS = 500
KBS_MIN_KBPS       = 150

# EPG
MAX_EPG_BYTES = 50 * 1024 * 1024
EPG_CACHE_TTL = 300

# Debug
MAX_LOG_TAIL_BYTES = 65536   # 64 Ko exposés via /debug/ffmpeg.log

# ── Qualités : heuristique prudente, MAIS priorité à QUALITY_COUNT ─
_ALL_QUALITIES = [
    ("240p", 240, 300),
    ("360p", 360, 500),
    ("480p", 480, 850),
    ("720p", 720, 1600),
]


def _resolve_quality_count() -> int:
    """Priorité absolue : QUALITY_COUNT (env).
    Fallback : 2 qualités (safe par défaut sur Render free).
    Ne JAMAIS retourner > 2 sans confirmation explicite."""
    raw = os.environ.get("QUALITY_COUNT")
    if raw is not None and raw.strip() != "":
        try:
            n = int(raw.strip())
            n = max(1, min(len(_ALL_QUALITIES), n))
            print(f"[config] QUALITY_COUNT={n} (via env)", flush=True)
            return n
        except ValueError:
            print(f"[config] QUALITY_COUNT={raw!r} invalide "
                  f"→ fallback 2", flush=True)
    else:
        print("[config] QUALITY_COUNT absent → fallback 2 "
              "(protection CPU Render free)", flush=True)

    # Détection CPU : au mieux une indication, jamais une garantie
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count() or 1
    print(f"[config] CPU autorisés détectés: {cores} "
          f"(indication seulement)", flush=True)

    return 2  # prudence : jamais plus de 2 sans QUALITY_COUNT explicite


QUALITY_COUNT = _resolve_quality_count()
QUALITIES = _ALL_QUALITIES[:QUALITY_COUNT]

# ── État global ─────────────────────────────────────────────────────
process = None
process_lock = threading.Lock()
stop_event = threading.Event()

_epg_lock = threading.Lock()
_epg_cache = None
_epg_cache_time = 0.0

_master_cache = {"body": b"", "mtime": 0.0}
_master_lock = threading.Lock()


# ════════════════════════════════════════════════════════════════════
# Source / KBS
# ════════════════════════════════════════════════════════════════════
def read_source() -> str:
    if not SOURCE_FILE.is_file():
        raise RuntimeError("Fichier /app/source.txt manquant.")
    source = SOURCE_FILE.read_text(
        encoding="utf-8-sig"
    ).strip().strip('"').strip("'").strip()
    parsed = urlsplit(source)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("URL HTTP/HTTPS invalide dans source.txt.")
    return source


def is_kbs(source: str) -> bool:
    return "kbs" in source.lower()


def effective_bitrate_kbps(base_kbps: int, kbs: bool) -> int:
    if not kbs:
        return base_kbps
    return max(int(base_kbps * 0.3),
               base_kbps - KBS_REDUCTION_KBPS,
               KBS_MIN_KBPS)


def looks_like_hls(source: str) -> bool:
    try:
        p = urlsplit(source)
    except ValueError:
        return False
    path = p.path.lower()
    return ".m3u8" in path or "/hls/" in path


# ════════════════════════════════════════════════════════════════════
# EPG
# ════════════════════════════════════════════════════════════════════
_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def build_epg_xml() -> bytes:
    global _epg_cache, _epg_cache_time
    with _epg_lock:
        now = time.time()
        if _epg_cache is not None and (now - _epg_cache_time) < EPG_CACHE_TTL:
            return _epg_cache

        root = ET.Element("tv")
        if EPG_DIR.is_dir():
            for epg_file in sorted(EPG_DIR.glob("*.xml")):
                try:
                    size = epg_file.stat().st_size
                    if size == 0 or size > MAX_EPG_BYTES:
                        print(f"EPG ignoré (taille {size}): {epg_file}",
                              flush=True)
                        continue
                    if _DOCTYPE_RE.search(epg_file.read_bytes()[:4096]):
                        print(f"EPG ignoré (DOCTYPE): {epg_file}", flush=True)
                        continue
                    file_root = ET.parse(epg_file).getroot()
                    for channel in file_root.findall("channel"):
                        root.append(channel)
                    for programme in file_root.findall("programme"):
                        root.append(programme)
                    print(f"EPG fusionné : {epg_file.name}", flush=True)
                except Exception as exc:
                    print(f"EPG erreur {epg_file}: {exc}", flush=True)

        if len(root) == 0:
            ch = ET.SubElement(root, "channel", id="novablade.live")
            ET.SubElement(ch, "display-name").text = "NovaBlade Live"

        body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        _epg_cache = body
        _epg_cache_time = now
        return body


# ════════════════════════════════════════════════════════════════════
# FFmpeg — génération dynamique (respecte QUALITY_COUNT)
# ════════════════════════════════════════════════════════════════════
def make_command(source: str):
    kbs = is_kbs(source)
    count = len(QUALITIES)
    if count < 1:
        raise RuntimeError("Aucune qualité vidéo configurée.")

    gop = max(1, SEGMENT_SECONDS * FPS_ESTIMATE)

    shutil.rmtree(BASE, ignore_errors=True)
    for name, _, _ in QUALITIES:
        (BASE / name).mkdir(parents=True, exist_ok=True)

    # Filtres dynamiques
    splits = "".join(f"[v{i}]" for i in range(count))
    filters = [f"[0:v]split={count}{splits}"]
    for i, (_, height, _) in enumerate(QUALITIES):
        filters.append(
            f"[v{i}]scale=-2:'min({height},ih)':flags=fast_bilinear[vout{i}]"
        )
    filter_complex = ";".join(filters)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-nostdin", "-y",
        "-fflags", "+genpts+discardcorrupt",
        "-rw_timeout", "15000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "2",
        "-probesize", "1000000",
        "-analyzeduration", "1000000",
    ]

    if looks_like_hls(source):
        cmd += ["-live_start_index", "-2"]
        print("Source HLS → -live_start_index -2", flush=True)
    else:
        print("Source non-HLS → -live_start_index omis", flush=True)

    cmd += ["-i", source, "-filter_complex", filter_complex]

    for i in range(count):
        cmd += ["-map", f"[vout{i}]", "-map", "0:a:0?"]

    for i, (_, _, base_kbps) in enumerate(QUALITIES):
        kbps = effective_bitrate_kbps(base_kbps, kbs)
        rate = f"{kbps}k"
        cmd += [
            f"-c:v:{i}", "libx264",
            f"-preset:v:{i}", "ultrafast",
            f"-tune:v:{i}", X264_TUNE,
            f"-threads:v:{i}", "1",
            f"-b:v:{i}", rate,
            f"-maxrate:v:{i}", rate,
            f"-bufsize:v:{i}", f"{kbps * 2}k",
            f"-g:v:{i}", str(gop),
            f"-keyint_min:v:{i}", "1",
            f"-sc_threshold:v:{i}", "0",
            f"-force_key_frames:v:{i}",
            f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
            f"-c:a:{i}", "aac",
            f"-b:a:{i}", "64k",
            f"-ar:a:{i}", "44100",
            f"-ac:a:{i}", "2",
        ]

    variant_map = " ".join(
        f"v:{i},a:{i},name:{name}"
        for i, (name, _, _) in enumerate(QUALITIES)
    )

    cmd += [
        "-map_metadata", "-1",
        "-max_muxing_queue_size", "1024",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", str(PLAYLIST_SIZE),
        "-hls_delete_threshold", str(DELETE_THRESHOLD),
        "-hls_flags",
        "delete_segments+independent_segments+program_date_time+temp_file",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", variant_map,
        "-hls_segment_filename",
        str(BASE / "%v" / "segment_%08d.ts"),
        str(BASE / "%v" / "index.m3u8"),
    ]
    return cmd


# ════════════════════════════════════════════════════════════════════
# Watchdog + logger
# ════════════════════════════════════════════════════════════════════
def newest_segment_mtime() -> float:
    newest = 0.0
    try:
        for p in BASE.glob("*/segment_*.ts"):
            try:
                m = p.stat().st_mtime
                if m > newest:
                    newest = m
            except OSError:
                pass
    except OSError:
        pass
    return newest


def _segment_logger(current):
    """Toutes les 10 s : âge + taille du segment le plus récent.
    Permet de détecter une dégradation même sans stall."""
    while current.poll() is None and not stop_event.is_set():
        time.sleep(10)
        try:
            segs = [
                (p, p.stat())
                for p in BASE.glob("*/segment_*.ts")
                if p.is_file()
            ]
            if not segs:
                print("[seg] aucun segment encore produit", flush=True)
                continue
            newest, st = max(segs, key=lambda t: t[1].st_mtime)
            age = time.time() - st.st_mtime
            size_kb = st.st_size // 1024
            print(
                f"[seg] newest={newest.parent.name}/{newest.name} "
                f"age={age:.1f}s size={size_kb}KB "
                f"(total_segments={len(segs)})",
                flush=True,
            )
        except OSError:
            pass


def supervise():
    global process
    consecutive_failures = 0

    while not stop_event.is_set():
        current = None
        try:
            source = read_source()
            command = make_command(source)
            print(
                f"FFmpeg démarrage (KBS={is_kbs(source)}, "
                f"{len(QUALITIES)} qualités {[q[0] for q in QUALITIES]}, "
                f"seg={SEGMENT_SECONDS}s, stall={FFMPEG_STALL_SECONDS}s)",
                flush=True,
            )

            log_fp = open(FFMPEG_LOG, "ab")
            with process_lock:
                current = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stderr=log_fp,
                )
                process = current

            threading.Thread(
                target=_segment_logger, args=(current,), daemon=True,
            ).start()

            start = time.monotonic()
            last_progress = start
            last_mtime = 0.0

            while current.poll() is None and not stop_event.is_set():
                time.sleep(1)
                mtime = newest_segment_mtime()
                now = time.monotonic()

                if mtime > last_mtime:
                    last_mtime = mtime
                    last_progress = now

                if last_mtime == 0.0:
                    stalled = (now - start) > FFMPEG_STARTUP_GRACE
                else:
                    stalled = (now - last_progress) > FFMPEG_STALL_SECONDS

                if stalled:
                    print(
                        f"Watchdog : stall (mtime={last_mtime:.0f}, "
                        f"elapsed={now - start:.0f}s) → restart",
                        flush=True,
                    )
                    current.terminate()
                    try:
                        current.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        current.kill()
                    break

            exit_code = current.wait()
            log_fp.close()
            print(f"FFmpeg terminé : code {exit_code}", flush=True)
            consecutive_failures = 0 if exit_code == 0 else consecutive_failures + 1

        except Exception as exc:
            consecutive_failures += 1
            print(f"Erreur FFmpeg/source : {exc}", flush=True)
        finally:
            with process_lock:
                if process is current:
                    process = None

        if not stop_event.is_set():
            backoff = min(30, 5 * max(1, consecutive_failures))
            stop_event.wait(backoff)


# ════════════════════════════════════════════════════════════════════
# HTTP
# ════════════════════════════════════════════════════════════════════
def _tail(path: Path, max_bytes: int) -> bytes:
    """Lit les derniers max_bytes d'un fichier (sans tout charger)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read()


class Handler(BaseHTTPRequestHandler):
    timeout = 30
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def respond(self, status, body, content_type,
                cache="no-store", head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if not head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def do_OPTIONS(self):
        self.respond(204, b"", "text/plain")

    def do_GET(self):
        self.handle_request(False)

    def do_HEAD(self):
        self.handle_request(True)

    def handle_request(self, head_only):
        path = unquote(urlsplit(self.path).path)

        # ── Santé ───────────────────────────────────────────────────
        if path == "/healthz":
            return self.respond(200, b"ok", "text/plain; charset=utf-8",
                                head_only=head_only)

        # ── Diagnostic racine ───────────────────────────────────────
        if path == "/":
            info = (
                f"NovaBlade IPTV\n"
                f"Qualités     : {', '.join(q[0] for q in QUALITIES)}\n"
                f"QUALITY_COUNT: {QUALITY_COUNT}\n"
                f"Segment      : {SEGMENT_SECONDS}s × {PLAYLIST_SIZE}\n"
                f"Stall        : {FFMPEG_STALL_SECONDS}s "
                f"(grâce {FFMPEG_STARTUP_GRACE}s)\n"
                f"Tune         : {X264_TUNE}\n"
                f"EPG          : /epg.xml\n"
                f"Live         : /live/master.m3u8\n"
                f"Debug log    : /debug/ffmpeg.log\n"
            ).encode("utf-8")
            return self.respond(200, info, "text/plain; charset=utf-8",
                                head_only=head_only)

        # ── Log FFmpeg (indispensable sur Render free) ─────────────
        if path == "/debug/ffmpeg.log":
            if not FFMPEG_LOG.is_file():
                return self.respond(404, b"No log yet", "text/plain",
                                    head_only=head_only)
            try:
                body = _tail(FFMPEG_LOG, MAX_LOG_TAIL_BYTES)
                return self.respond(200, body, "text/plain; charset=utf-8",
                                    cache="no-store", head_only=head_only)
            except OSError as exc:
                return self.respond(500, str(exc).encode(), "text/plain",
                                    head_only=head_only)

        # ── Statut des segments ────────────────────────────────────
        if path == "/debug/segments":
            try:
                segs = sorted(
                    BASE.glob("*/segment_*.ts"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                lines = [f"Total segments: {len(segs)}"]
                now = time.time()
                for p in segs[:20]:
                    st = p.stat()
                    age = now - st.st_mtime
                    lines.append(
                        f"{p.parent.name}/{p.name}  "
                        f"age={age:.1f}s  size={st.st_size // 1024}KB"
                    )
                body = ("\n".join(lines) + "\n").encode("utf-8")
                return self.respond(200, body, "text/plain; charset=utf-8",
                                    head_only=head_only)
            except OSError as exc:
                return self.respond(500, str(exc).encode(), "text/plain",
                                    head_only=head_only)

        # ── EPG ────────────────────────────────────────────────────
        if path == "/epg.xml":
            try:
                body = build_epg_xml()
                return self.respond(200, body, "application/xml; charset=utf-8",
                                    cache="public, max-age=300",
                                    head_only=head_only)
            except Exception as exc:
                print(f"Erreur EPG : {exc}", flush=True)
                return self.respond(500, b"EPG error", "text/plain",
                                    head_only=head_only)

        # ── Live HLS ───────────────────────────────────────────────
        if not path.startswith("/live/"):
            return self.respond(404, b"Not found", "text/plain",
                                head_only=head_only)

        relative = path[len("/live/"):]

        if relative == "master.m3u8":
            return self._serve_master(BASE / "master.m3u8", head_only)

        parts = Path(relative).parts
        if (
            not relative
            or relative.startswith("/")
            or any(p in ("", ".", "..") for p in parts)
            or Path(relative).suffix not in (".m3u8", ".ts")
        ):
            return self.respond(404, b"Not found", "text/plain",
                                head_only=head_only)

        base = BASE.resolve()
        target = (BASE / relative).resolve()
        try:
            if not target.is_relative_to(base):
                raise ValueError
        except (AttributeError, ValueError):
            if base not in target.parents:
                return self.respond(403, b"Forbidden", "text/plain",
                                    head_only=head_only)

        if not target.is_file():
            return self.respond(503, b"Segment not ready; retry shortly",
                                "text/plain; charset=utf-8",
                                head_only=head_only)

        if target.suffix == ".m3u8":
            return self._serve_playlist(target, head_only)

        try:
            body = target.read_bytes()
            self.respond(200, body, "video/mp2t",
                         "public, max-age=60", head_only=head_only)
        except OSError as exc:
            print(f"Erreur segment HLS : {exc}", flush=True)
            self.respond(503, b"Temporary file error", "text/plain",
                         head_only=head_only)

    def _serve_master(self, target: Path, head_only: bool):
        try:
            mtime = target.stat().st_mtime
        except OSError:
            return self.respond(503, b"Master not ready; retry shortly",
                                "text/plain; charset=utf-8",
                                head_only=head_only)

        with _master_lock:
            if mtime != _master_cache["mtime"]:
                try:
                    _master_cache["body"] = target.read_bytes()
                    _master_cache["mtime"] = mtime
                except OSError:
                    return self.respond(503, b"Master unreadable",
                                        "text/plain", head_only=head_only)
            body = _master_cache["body"]

        self.respond(200, body, "application/vnd.apple.mpegurl",
                     "no-cache, no-store, must-revalidate",
                     head_only=head_only)

    def _serve_playlist(self, target: Path, head_only: bool):
        try:
            body = target.read_bytes()
        except OSError as exc:
            print(f"Erreur playlist HLS : {exc}", flush=True)
            return self.respond(503, b"Temporary file error", "text/plain",
                                head_only=head_only)

        # #EXT-X-START est un HINT, pas une contrainte
        if target.name == "index.m3u8" and body.startswith(b"#EXTM3U"):
            text = body.decode("utf-8", errors="replace")
            segment_count = text.count("#EXTINF:")
            start_offset = min(
                START_DELAY_SECONDS,
                max(2, (PLAYLIST_SIZE - 2) * SEGMENT_SECONDS),
            )
            min_segments = max(3, start_offset // SEGMENT_SECONDS + 1)
            if segment_count >= min_segments and "#EXT-X-START:" not in text:
                lines = text.splitlines()
                lines.insert(
                    1,
                    f"#EXT-X-START:TIME-OFFSET=-{start_offset},PRECISE=YES",
                )
                body = ("\n".join(lines) + "\n").encode("utf-8")

        self.respond(200, body, "application/vnd.apple.mpegurl",
                     "no-cache, no-store, must-revalidate",
                     head_only=head_only)


# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg n'est pas installé.")

    EPG_DIR.mkdir(parents=True, exist_ok=True)

    # Bandeau diagnostic au démarrage
    print("═" * 60, flush=True)
    print("NovaBlade IPTV — démarrage", flush=True)
    print(f"  QUALITY_COUNT (effectif) : {QUALITY_COUNT}", flush=True)
    print(f"  Qualités actives         : {[q[0] for q in QUALITIES]}",
          flush=True)
    print(f"  Segment                  : {SEGMENT_SECONDS}s × {PLAYLIST_SIZE} "
          f"(≈{SEGMENT_SECONDS * PLAYLIST_SIZE}s buffer)", flush=True)
    print(f"  Stall / grâce            : {FFMPEG_STALL_SECONDS}s / "
          f"{FFMPEG_STARTUP_GRACE}s", flush=True)
    print(f"  Tune                     : {X264_TUNE}", flush=True)
    print(f"  NOTE: #EXT-X-START=-{START_DELAY_SECONDS}s = hint "
          f"(VLC l'ignore)", flush=True)
    print("  Endpoints: /, /healthz, /epg.xml, /live/master.m3u8, "
          "/debug/ffmpeg.log, /debug/segments", flush=True)
    print("═" * 60, flush=True)

    threading.Thread(target=supervise, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    server.allow_reuse_address = True

    print(f"Serveur HTTP écoute sur {PORT}", flush=True)

    try:
        server.serve_forever()
    finally:
        stop_event.set()
        server.server_close()
        with process_lock:
            current = process
        if current and current.poll() is None:
            current.terminate()
            try:
                current.wait(timeout=5)
            except subprocess.TimeoutExpired:
                current.kill()
