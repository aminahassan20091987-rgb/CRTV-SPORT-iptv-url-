import os
import time
import json
import hmac
import shutil
import secrets
import threading
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

# ============================================================
# NovaBlade IPTV - Prototype
# Aucun .env et aucune URL de source dans le code.
# Déploiement Docker avec FFmpeg installé.
# ============================================================

APP_NAME = "NovaBlade IPTV"
HOST = "0.0.0.0"
PORT = 8080

ROOT = Path("/tmp/novabladeiptv")
ROOT.mkdir(parents=True, exist_ok=True)

QUALITIES = {
    "240p": {"height": 240, "bitrate": "350k", "maxrate": "450k", "bufsize": "700k"},
    "360p": {"height": 360, "bitrate": "600k", "maxrate": "750k", "bufsize": "1200k"},
    "480p": {"height": 480, "bitrate": "1000k", "maxrate": "1250k", "bufsize": "2000k"},
    "720p": {"height": 720, "bitrate": "2200k", "maxrate": "2750k", "bufsize": "4400k"},
}

state = {
    "source": "",
    "password": "",
    "process": None,
    "error": "",
    "started_at": None,
}

lock = threading.RLock()


def html_escape(value):
    value = str(value)
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def is_running():
    process = state["process"]
    return process is not None and process.poll() is None


def stop_stream():
    with lock:
        process = state["process"]
        state["process"] = None

    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def start_stream(source):
    if not source.startswith(("https://", "http://")):
        raise ValueError("La source doit être une URL HTTP ou HTTPS.")

    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg n'est pas installé sur le serveur.")

    stop_stream()

    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True, exist_ok=True)

    for quality in QUALITIES:
        (ROOT / quality).mkdir(parents=True, exist_ok=True)

    filters = []
    maps = []
    var_map = []

    for index, (name, config) in enumerate(QUALITIES.items()):
        filters.append(
            f"[0:v]scale=-2:{config['height']}:"
            f"force_original_aspect_ratio=decrease[v{index}]"
        )
        maps.extend(["-map", f"[v{index}]", "-map", "0:a:0?"])

        var_map.append(
            f"v:{index},a:{index},name:{name}"
        )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-y",
        "-i", source,
        "-filter_complex", ";".join(filters),
    ]

    command.extend(maps)

    for index, (name, config) in enumerate(QUALITIES.items()):
        command.extend([
            f"-c:v:{index}", "libx264",
            f"-preset", "veryfast",
            f"-b:v:{index}", config["bitrate"],
            f"-maxrate:v:{index}", config["maxrate"],
            f"-bufsize:v:{index}", config["bufsize"],
        ])

    command.extend([
        "-c:a", "aac",
        "-b:a", "96k",
        "-ac", "2",
        "-ar", "44100",
        "-f", "hls",
        "-hls_time", "4",
        "-hls_list_size", "6",
        "-hls_flags", "delete_segments+append_list+independent_segments",
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", " ".join(var_map),
        "-hls_segment_filename", str(ROOT / "%v" / "segment_%06d.ts"),
        str(ROOT / "%v" / "index.m3u8"),
    ])

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(1)

    if process.poll() is not None:
        raise RuntimeError(
            "FFmpeg n'a pas démarré. Vérifie la source, les codecs et les pistes audio/vidéo."
        )

    with lock:
        state["source"] = source
        state["process"] = process
        state["started_at"] = time.time()
        state["error"] = ""


def page(title, content):
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_escape(title)} - NovaBlade IPTV</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0b1020;color:#edf2ff;font-family:Arial,sans-serif}}
header{{padding:24px 16px;text-align:center;background:#121a30;border-bottom:1px solid #293654}}
h1{{margin:0;color:#65e6c4;font-size:25px}}
main{{max-width:760px;margin:25px auto;padding:0 15px}}
.card{{background:#141e35;border:1px solid #293654;border-radius:14px;padding:20px;margin-bottom:16px}}
input,button{{width:100%;padding:13px;border-radius:8px;margin-top:8px;font-size:15px}}
input{{background:#0b1020;border:1px solid #3a4b70;color:white}}
button{{border:0;background:#65e6c4;color:#07141b;font-weight:bold;cursor:pointer}}
a{{color:#65e6c4;overflow-wrap:anywhere}}
.small{{color:#a9b6d2;font-size:13px;line-height:1.5}}
.status{{padding:10px;background:#0b1020;border-radius:8px;overflow-wrap:anywhere}}
</style>
</head>
<body>
<header><h1>NovaBlade IPTV</h1><p>Gestion et diffusion HLS</p></header>
<main>{content}</main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "NovaBlade/1.0"

    def log_message(self, fmt, *args):
        # Ne pas enregistrer les requêtes dans les journaux standards.
        pass

    def send_text(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16384:
            raise ValueError("Formulaire trop volumineux.")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in parse_qs(raw).items() if v}

    def authorized(self, form=None):
        if not state["password"]:
            return False

        cookie = self.headers.get("Cookie", "")
        token = ""
        for item in cookie.split(";"):
            if item.strip().startswith("nb_token="):
                token = item.strip().split("=", 1)[1]

        if not token:
            return False

        # Le jeton est créé à la connexion et conservé en mémoire.
        return hmac.compare_digest(token, self.server.session_token)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/healthz":
            return self.send_text(200, "ok", "text/plain; charset=utf-8")

        if path == "/":
            if not state["password"]:
                content = """
                <section class="card">
                <h2>Créer le mot de passe administrateur</h2>
                <p class="small">Choisis un mot de passe long. Il sera conservé uniquement en mémoire jusqu'au redémarrage.</p>
                <form method="post" action="/setup">
                <input type="password" name="password" minlength="12" required placeholder="12 caractères minimum">
                <button>Créer le mot de passe</button></form></section>"""
            elif not self.authorized():
                content = """
                <section class="card"><h2>Connexion</h2>
                <form method="post" action="/login">
                <input type="password" name="password" required placeholder="Mot de passe administrateur">
                <button>Se connecter</button></form></section>"""
            else:
                running = is_running()
                status = "En diffusion" if running else "Arrêté"
                source_field = """
                <form method="post" action="/source">
                <label>URL de la source HLS autorisée</label>
                <input type="url" name="source" required placeholder="https://serveur.example/playlist.m3u8">
                <button>Démarrer / remplacer la source</button>
                </form>"""
                links = ""
                if running:
                    links = '<div class="card"><h3>Liens HLS</h3>'
                    for quality in QUALITIES:
                        links += (
                            f'<p><b>{quality}</b> — <a href="/live/{quality}/index.m3u8">'
                            f'/live/{quality}/index.m3u8</a></p>'
                        )
                    links += '<p><b>Master playlist</b> — <a href="/live/master.m3u8">/live/master.m3u8</a></p></div>'

                content = f"""
                <section class="card"><h2>Tableau de bord</h2>
                <p class="status">État : <b>{status}</b></p>
                {source_field}
                <form method="post" action="/stop"><button>Arrêter le flux</button></form>
                <p class="small">La source n'est pas affichée sur cette page. Les liens de sortie restent publics si le service est accessible publiquement.</p>
                <p><a href="/logout">Se déconnecter</a></p></section>
                {links}"""
            return self.send_text(200, page("Accueil", content))

        if path.startswith("/live/"):
            if not is_running():
                return self.send_text(503, "Flux indisponible", "text/plain; charset=utf-8")

            relative = path[len("/live/"):]
            requested = (ROOT / relative).resolve()

            if not str(requested).startswith(str(ROOT.resolve()) + os.sep):
                return self.send_text(403, "Forbidden", "text/plain; charset=utf-8")

            if not requested.is_file():
                return self.send_text(404, "Playlist ou segment pas encore disponible",
                                      "text/plain; charset=utf-8")

            content_type = "application/vnd.apple.mpegurl" if requested.suffix == ".m3u8" else "video/mp2t"
            try:
                data = requested.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_text(404, "Page introuvable")

    def do_POST(self):
        path = urlparse(self.path).path

        try:
            form = self.read_form()
        except Exception:
            return self.send_text(400, "Formulaire invalide", "text/plain; charset=utf-8")

        if path == "/setup":
            password = form.get("password", "")
            if state["password"]:
                return self.send_text(409, "Mot de passe déjà créé.")
            if len(password) < 12:
                return self.send_text(400, "Utilise au moins 12 caractères.")
            state["password"] = password
            self.server.session_token = secrets.token_urlsafe(32)
            self.send_response(303)
            self.send_header("Set-Cookie", f"nb_token={self.server.session_token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/login":
            if not state["password"] or not hmac.compare_digest(form.get("password", ""), state["password"]):
                return self.send_text(401, "Mot de passe incorrect.")
            self.server.session_token = secrets.token_urlsafe(32)
            self.send_response(303)
            self.send_header("Set-Cookie", f"nb_token={self.server.session_token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/")
            self.end_headers()
            return

        if not self.authorized():
            return self.send_text(401, "Connexion administrateur requise.")

        if path == "/source":
            source = form.get("source", "").strip()
            try:
                start_stream(source)
            except Exception as exc:
                state["error"] = str(exc)
                return self.send_text(400, "Impossible de démarrer le flux. Vérifie la source et la configuration FFmpeg.")
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/stop":
            stop_stream()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        self.send_text(404, "Page introuvable")

    def do_HEAD(self):
        self.send_text(200, "", "text/plain; charset=utf-8")


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.session_token = ""
    print(f"{APP_NAME} démarré sur le port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_stream()
        server.server_close()
