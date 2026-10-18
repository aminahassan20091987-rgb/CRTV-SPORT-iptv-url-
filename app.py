import os
import time
import shutil
import threading
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))

BASE = Path("/tmp/novabladeiptv")
SOURCE_FILE = Path("/app/source.txt")

QUALITIES = [
    ("240p", 240, "350k"),
    ("360p", 360, "600k"),
    ("480p", 480, "1000k"),
    ("720p", 720, "2200k"),
]

process = None
process_lock = threading.Lock()


def read_source():
    if not SOURCE_FILE.exists():
        raise RuntimeError("Fichier source.txt manquant.")

    source = SOURCE_FILE.read_text(
        encoding="utf-8-sig"
    ).strip()

    source = source.strip('"').strip("'").strip()

    if not source.startswith(("http://", "https://")):
        raise RuntimeError(
            "URL invalide dans source.txt. "
            f"Contenu lu : {source[:30]!r}"
        )

    return source


def make_command(source):
    shutil.rmtree(BASE, ignore_errors=True)

    for name, _, _ in QUALITIES:
        (BASE / name).mkdir(parents=True, exist_ok=True)

    filters = (
        "[0:v]split=4[v0][v1][v2][v3];"
        "[v0]scale=-2:240[v240];"
        "[v1]scale=-2:360[v360];"
        "[v2]scale=-2:480[v480];"
        "[v3]scale=-2:720[v720]"
    )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",
        "-i", source,
        "-filter_complex", filters,
    ]

    for label in ("v240", "v360", "v480", "v720"):
        cmd += ["-map", f"[{label}]", "-map", "0:a:0?"]

    for i, (_, _, bitrate) in enumerate(QUALITIES):
        cmd += [
            f"-c:v:{i}", "libx264",
            f"-b:v:{i}", bitrate,
            f"-preset:v:{i}", "ultrafast",
            f"-g:v:{i}", "48",
            f"-sc_threshold:v:{i}", "0",
            f"-c:a:{i}", "aac",
            f"-b:a:{i}", "96k",
        ]

    cmd += [
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+independent_segments",
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

    while True:
        try:
            source = read_source()
            command = make_command(source)

            print("Démarrage du flux FFmpeg...", flush=True)

            with process_lock:
                process = subprocess.Popen(command)

            exit_code = process.wait()

            print(
                f"FFmpeg arrêté avec le code {exit_code}.",
                flush=True,
            )

        except Exception as exc:
            print(f"Erreur du flux : {exc}", flush=True)

        time.sleep(10)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_body(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])

        if path == "/healthz":
            return self.send_body(
                200, b"ok", "text/plain; charset=utf-8"
            )

        if path == "/":
            return self.send_body(
                200,
                b"NovaBlade IPTV service is running",
                "text/plain; charset=utf-8",
            )

        if not path.startswith("/live/"):
            return self.send_body(
                404, b"Not found", "text/plain; charset=utf-8"
            )

        relative = path[len("/live/"):]
        target = (BASE / relative).resolve()

        if not str(target).startswith(str(BASE.resolve()) + os.sep):
            return self.send_body(
                403, b"Forbidden", "text/plain; charset=utf-8"
            )

        if not target.is_file():
            return self.send_body(
                503,
                b"Playlist or segment not ready",
                "text/plain; charset=utf-8",
            )

        content_type = (
            "application/vnd.apple.mpegurl"
            if target.suffix == ".m3u8"
            else "video/mp2t"
        )

        try:
            self.send_body(200, target.read_bytes(), content_type)
        except OSError as exc:
            print(f"Erreur de lecture du fichier HLS : {exc}", flush=True)

    def do_HEAD(self):
        self.send_body(200, b"", "text/plain; charset=utf-8")


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg n'est pas installé.")

    threading.Thread(target=supervise, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"NovaBlade IPTV écoute sur {PORT}", flush=True)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        with process_lock:
            if process and process.poll() is None:
                process.terminate()
