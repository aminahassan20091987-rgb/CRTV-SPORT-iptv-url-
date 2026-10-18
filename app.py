
import os
import time
import shutil
import threading
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, unquote

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

BASE = Path("/tmp/novabladeiptv")
SOURCE_FILE = Path("/app/source.txt")

SEGMENT_SECONDS = 4
PLAYLIST_SIZE = 30            # Environ 120 secondes
DELETE_THRESHOLD = 30         # Conserve les anciens segments
START_DELAY_SECONDS = 60      # Cible de lecture derrière le direct

QUALITIES = [
    ("240p", 240, "300k"),
    ("360p", 360, "500k"),
    ("480p", 480, "850k"),
    ("720p", 720, "1600k"),
]

process = None
process_lock = threading.Lock()
stop_event = threading.Event()


def read_source():
    if not SOURCE_FILE.is_file():
        raise RuntimeError("Fichier /app/source.txt manquant.")

    source = SOURCE_FILE.read_text(
        encoding="utf-8-sig"
    ).strip().strip('"').strip("'").strip()

    parsed = urlsplit(source)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("URL HTTP/HTTPS invalide dans source.txt.")

    return source


def make_command(source):
    shutil.rmtree(BASE, ignore_errors=True)

    for name, _, _ in QUALITIES:
        (BASE / name).mkdir(parents=True, exist_ok=True)

    filters = (
        "[0:v]split=4[v0][v1][v2][v3];"
        "[v0]scale=-2:240:flags=fast_bilinear[v240];"
        "[v1]scale=-2:360:flags=fast_bilinear[v360];"
        "[v2]scale=-2:480:flags=fast_bilinear[v480];"
        "[v3]scale=-2:720:flags=fast_bilinear[v720]"
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",
        "-fflags", "+genpts",
        "-rw_timeout", "15000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", source,
        "-filter_complex", filters,
    ]

    for label in ("v240", "v360", "v480", "v720"):
        cmd += ["-map", f"[{label}]", "-map", "0:a:0?"]

    for i, (_, _, bitrate) in enumerate(QUALITIES):
        rate = int(bitrate[:-1])
        cmd += [
            f"-c:v:{i}", "libx264",
            f"-preset:v:{i}", "ultrafast",
            f"-threads:v:{i}", "1",
            f"-b:v:{i}", bitrate,
            f"-maxrate:v:{i}", bitrate,
            f"-bufsize:v:{i}", f"{rate * 2}k",
            f"-g:v:{i}", "120",
            f"-keyint_min:v:{i}", "1",
            f"-sc_threshold:v:{i}", "0",
            f"-force_key_frames:v:{i}",
            f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
            f"-c:a:{i}", "aac",
            f"-b:a:{i}", "96k",
            f"-ar:a:{i}", "44100",
        ]

    cmd += [
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", str(PLAYLIST_SIZE),
        "-hls_delete_threshold", str(DELETE_THRESHOLD),
        "-hls_flags",
        "delete_segments+independent_segments+program_date_time",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map",
        "v:0,a:0,name:240p "
        "v:1,a:1,name:360p "
        "v:2,a:2,name:480p "
        "v:3,a:3,name:720p",
        "-hls_segment_filename",
        str(BASE / "%v" / "segment_%06d.ts"),
        str(BASE / "%v" / "index.m3u8"),
    ]

    return cmd


def supervise():
    global process

    while not stop_event.is_set():
        current = None

        try:
            source = read_source()
            command = make_command(source)
            print("Démarrage FFmpeg...", flush=True)

            with process_lock:
                current = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                )
                process = current

            exit_code = current.wait()
            print(f"FFmpeg terminé : code {exit_code}", flush=True)

        except Exception as exc:
            print(f"Erreur FFmpeg/source : {exc}", flush=True)

        finally:
            with process_lock:
                if process is current:
                    process = None

        if not stop_event.is_set():
            stop_event.wait(5)


class Handler(BaseHTTPRequestHandler):
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

        if path == "/healthz":
            return self.respond(
                200, b"ok", "text/plain; charset=utf-8",
                head_only=head_only,
            )

        if path == "/":
            return self.respond(
                200, b"NovaBlade IPTV is running",
                "text/plain; charset=utf-8",
                head_only=head_only,
            )

        if not path.startswith("/live/"):
            return self.respond(
                404, b"Not found", "text/plain",
                head_only=head_only,
            )

        relative = path[len("/live/"):]
        parts = Path(relative).parts

        if (
            not relative
            or relative.startswith("/")
            or any(p in ("", ".", "..") for p in parts)
            or Path(relative).suffix not in (".m3u8", ".ts")
        ):
            return self.respond(
                404, b"Not found", "text/plain",
                head_only=head_only,
            )

        base = BASE.resolve()
        target = (BASE / relative).resolve()

        if base not in target.parents:
            return self.respond(
                403, b"Forbidden", "text/plain",
                head_only=head_only,
            )

        if not target.is_file():
            return self.respond(
                503, b"Segment not ready; retry shortly",
                "text/plain; charset=utf-8",
                head_only=head_only,
            )

        is_playlist = target.suffix == ".m3u8"
        content_type = (
            "application/vnd.apple.mpegurl"
            if is_playlist else "video/mp2t"
        )

        try:
            body = target.read_bytes()

            # Demande aux lecteurs compatibles de démarrer environ
            # 60 secondes derrière le direct, une fois le tampon prêt.
            if (
                is_playlist
                and target.name == "index.m3u8"
                and body.startswith(b"#EXTM3U")
            ):
                text = body.decode("utf-8", errors="replace")
                segment_count = text.count("#EXTINF:")

                if (
                    segment_count * SEGMENT_SECONDS
                    >= START_DELAY_SECONDS + SEGMENT_SECONDS
                    and "#EXT-X-START:" not in text
                ):
                    lines = text.splitlines()
                    lines.insert(
                        1,
                        f"#EXT-X-START:TIME-OFFSET=-{START_DELAY_SECONDS},PRECISE=YES",
                    )
                    body = ("\n".join(lines) + "\n").encode("utf-8")

            cache = (
                "no-cache, no-store, must-revalidate"
                if is_playlist else "public, max-age=60"
            )

            self.respond(
                200, body, content_type, cache,
                head_only=head_only,
            )

        except OSError as exc:
            print(f"Erreur fichier HLS : {exc}", flush=True)
            self.respond(
                503, b"Temporary file error", "text/plain",
                head_only=head_only,
            )


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg n'est pas installé.")

    threading.Thread(target=supervise, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True

    print(f"NovaBlade IPTV écoute sur {PORT}", flush=True)

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
