import time
import shutil
import threading
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP_NAME = "NovaBlade IPTV"
HOST = "0.0.0.0"
PORT = 8080

BASE = Path("/tmp/novabladeiptv")
SOURCE_FILE = Path("/app/source.txt")

QUALITIES = {
    "240p": {"height": 240, "bitrate": "350k"},
    "360p": {"height": 360, "bitrate": "600k"},
    "480p": {"height": 480, "bitrate": "1000k"},
    "720p": {"height": 720, "bitrate": "2200k"},
}

process = None
process_lock = threading.Lock()


def read_source():
    if not SOURCE_FILE.exists():
        raise RuntimeError(
            "Fichier source.txt manquant. Ajoute la source HLS autorisée."
        )

    source = SOURCE_FILE.read_text(encoding="utf-8").strip()

    if not source.startswith(("http://", "https://")):
        raise RuntimeError("URL HTTP/HTTPS invalide dans source.txt.")

    return source


def build_command(source):
    BASE.mkdir(parents=True, exist_ok=True)

    for name in QUALITIES:
        (BASE / name).mkdir(parents=True, exist_ok=True)

    filters = (
        "[0:v]split=4[v0][v1][v2][v3];"
        "[v0]scale=-2:240[v240];"
        "[v1]scale=-2:360[v360];"
        "[v2]scale=-2:480[v480];"
        "[v3]scale=-2:720[v720]"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",
        "-i", source,
        "-filter_complex", filters,
    ]

    for name, label in [
        ("240p", "v240"),
        ("360p", "v360"),
        ("480p", "v480"),
        ("720p", "v720"),
    ]:
        command.extend(["-map", f"[{label}]", "-map", "0:a:0?"])

    for index, (name, config) in enumerate(QUALITIES.items()):
        command.extend([
            f"-c:v:{index}", "libx264",
            f"-b:v:{index}", config["bitrate"],
            f"-preset:v:{index}", "ultrafast",
            f"-g:v:{index}", "48",
            f"-sc_threshold:v:{index}", "0",
            f"-c:a:{index}", "aac",
            f"-b:a:{index}", "96k",
        ])

    command.extend([
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
    ])

    return command


def supervise_stream():
    global process

    while True:
        try:
            source = read_source()
            command = build_command(source)

            print("NovaBlade : démarrage FFmpeg", flush=True)

            with process_lock:
                process = subprocess.Popen(command)

            code = process.wait()
            print(
                f"FFmpeg arrêté (code {code}). Nouvelle tentative dans 10 s.",
                flush=True,
            )

        except Exception as exc:
            print(f"Erreur du flux : {exc}", flush=True)

        time.sleep(10)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            return self.reply(200, b"ok", "text/plain")

        if path == "/":
            body = (
                "NovaBlade IPTV\n"
                "HLS service\n"
                "Use /live/master.m3u8\n"
            ).encode()
            return self.reply(200, body, "text/plain")

        if not path.startswith("/live/"):
            return self.reply(404, b"Not found", "text/plain")

        relative = path[len("/live/"):]
        target = (BASE / relative).resolve()

        if not str(target).startswith(str(BASE.resolve()) + "/"):
            return self.reply(403, b"Forbidden", "text/plain")

        if not target.is_file():
            return self.reply(
                503,
                b"Playlist or segment not ready",
                "text/plain",
            )

        content_type = (
            "application/vnd.apple.mpegurl"
            if target.suffix == ".m3u8"
            else "video/mp2t"
        )

        try:
            self.reply(200, target.read_bytes(), content_type)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reply(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg n'est pas installé.")

    threading.Thread(
        target=supervise_stream,
        daemon=True,
    ).start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"{APP_NAME} prêt sur le port {PORT}", flush=True)

    try:
        server.serve_forever()
    finally:
        server.server_close()
