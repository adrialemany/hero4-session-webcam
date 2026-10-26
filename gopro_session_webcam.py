import argparse
import collections
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

log = logging.getLogger("gopro")

KEEPALIVE = b"_GPHD_:0:0:2:0.000000\n"
UDP_PORT = 8554
FRESH = 1.5
COOL_DOWN = 120
CACHE_DIR = os.path.expanduser("~/.cache/gopro-session")
FFMPEG = os.environ.get("GOPRO_FFMPEG", "ffmpeg")


class Camera:
    def __init__(self, host, mac=None):
        self.host = host
        self.addr = host.partition(":")[0]
        self.mac = (mac or self._load_mac() or "").lower() or None

    def get(self, path, timeout=2.0):
        url = f"http://{self.host}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()

    def alive(self):
        try:
            self.get("/gp/gpControl/status", 1.5)
            return True
        except Exception:
            return False

    def status(self):
        try:
            return json.loads(self.get("/gp/gpControl/status", 2.0))
        except Exception:
            return None

    def start_stream(self):
        return self.get("/gp/gpControl/execute?p1=gpStream&a1=proto_v2&c1=restart", 3.0)

    def stop_stream(self):
        self.get("/gp/gpControl/execute?p1=gpStream&c1=stop", 1.5)

    def set_stream_bitrate(self, bps):
        return self.get(f"/gp/gpControl/setting/62/{int(bps)}", 2.0)

    def set_autooff_never(self):
        self.get("/gp/gpControl/setting/59/0", 2.0)

    def delete_all_media(self):
        self.get("/gp/gpControl/command/storage/delete/all", 15.0)

    def _mac_file(self):
        return os.path.join(CACHE_DIR, "mac")

    def _load_mac(self):
        try:
            with open(self._mac_file()) as f:
                m = f.read().strip()
            return m if re.fullmatch(r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", m) else None
        except OSError:
            return None

    def remember_mac(self):
        if self.mac:
            return
        try:
            out = subprocess.run(["ip", "neigh", "show", self.addr],
                                 capture_output=True, text=True, timeout=2).stdout
            m = re.search(r"lladdr ([0-9a-fA-F:]{17})", out)
            if m:
                self.mac = m.group(1).lower()
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(self._mac_file(), "w") as f:
                    f.write(self.mac)
                log.info("MAC de la camara guardada: %s", self.mac)
        except Exception:
            pass

    def wake(self):
        if not self.mac:
            return
        try:
            pkt = b"\xff" * 6 + bytes.fromhex(self.mac.replace(":", "")) * 16
            bcast = self.addr.rsplit(".", 1)[0] + ".255"
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(pkt, (bcast, 9))
            s.close()
        except OSError:
            pass


class KeepAlive(threading.Thread):
    def __init__(self, addr, port=UDP_PORT, interval=1.0):
        super().__init__(daemon=True)
        self.addr, self.port, self.interval = addr, port, interval
        self._stop_evt = threading.Event()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not self._stop_evt.is_set():
            try:
                sock.sendto(KEEPALIVE, (self.addr, self.port))
            except OSError:
                pass
            self._stop_evt.wait(self.interval)
        sock.close()

    def stop(self):
        self._stop_evt.set()


class Proc:
    def __init__(self, name, cmd, stdin=None, stdout=None):
        self.name = name
        self.p = subprocess.Popen(cmd, stdin=stdin, stdout=stdout,
                                  stderr=subprocess.PIPE)
        self.tail = collections.deque(maxlen=6)
        self._drainer = threading.Thread(target=self._drain, daemon=True)
        self._drainer.start()

    def _drain(self):
        for raw in self.p.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self.tail.append(line)
                log.debug("[%s] %s", self.name, line)

    def alive(self):
        return self.p.poll() is None

    def explain(self):
        if not self.alive():
            self._drainer.join(1.0)
        return " | ".join(self.tail) or "sin salida de error"

    def stop(self):
        try:
            if self.p.stdin:
                self.p.stdin.close()
        except OSError:
            pass
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(2)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()


_CAPS = {}


def passthrough_args():
    if "pt" not in _CAPS:
        _CAPS["pt"] = []
        for opt in (["-fps_mode", "passthrough"], ["-vsync", "passthrough"]):
            try:
                r = subprocess.run(
                    [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
                     "-i", "color=c=black:s=16x16:d=0.1"] + opt + ["-f", "null", "-"],
                    capture_output=True, timeout=10)
                if r.returncode == 0:
                    _CAPS["pt"] = opt
                    break
            except Exception:
                pass
        log.debug("Opcion de sincronizacion de ffmpeg: %s", " ".join(_CAPS["pt"]) or "ninguna")
    return _CAPS["pt"]


def have_filter(name):
    key = "f_" + name
    if key not in _CAPS:
        try:
            out = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True,
                                 text=True, timeout=10).stdout
            _CAPS[key] = any(len(l.split()) > 1 and l.split()[1] == name for l in out.splitlines())
        except Exception:
            _CAPS[key] = False
    return _CAPS[key]


def video_filters(cfg):
    w, h = cfg.size
    q = cfg.quality
    vf = []
    if cfg.rotate180:
        vf += ["hflip", "vflip"]
    elif cfg.mirror:
        vf += ["hflip"]
    if q == "high":
        if have_filter("pp"):
            vf.append("pp=hb/vb/dr")
        if have_filter("hqdn3d"):
            vf.append("hqdn3d=2:1.5:3:3")
    scaler = cfg.scaler or ("fast_bilinear" if q == "fast" else "lanczos")
    flags = f"flags={scaler}"
    if cfg.fit == "crop":
        vf += [f"scale={w}:{h}:force_original_aspect_ratio=increase:{flags}", f"crop={w}:{h}"]
    elif cfg.fit == "stretch":
        vf += [f"scale={w}:{h}:{flags}"]
    else:
        vf += [f"scale={w}:{h}:force_original_aspect_ratio=decrease:{flags}",
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"]
    amount = cfg.sharpen if cfg.sharpen is not None else (0.0 if q == "fast" else 0.6)
    if amount > 0 and have_filter("unsharp"):
        vf.append(f"unsharp=5:5:{amount}:5:5:0.0")
    vf.append("format=yuv420p")
    return vf


def decoder_cmd(cfg):
    vf = video_filters(cfg)
    src = f"udp://0.0.0.0:{UDP_PORT}?fifo_size=50000&overrun_nonfatal=1&buffer_size=262144"
    if cfg.low_latency:
        inp = ["-fflags", "nobuffer+discardcorrupt", "-flags", "low_delay",
               "-probesize", "8192", "-analyzeduration", "0",
               "-max_delay", "0", "-threads", "1"]
        out = passthrough_args()
    else:
        inp = ["-fflags", "nobuffer+discardcorrupt", "-flags", "low_delay",
               "-probesize", "65536", "-analyzeduration", "1000000"]
        out = []
    return ([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"] + inp +
            ["-f", "mpegts", "-i", src, "-an", "-vf", ",".join(vf)] + out +
            ["-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"])


def writer_cmd(cfg):
    w, h = cfg.size
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if cfg.low_latency:
        cmd += ["-fflags", "nobuffer", "-flags", "low_delay"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
            "-framerate", str(cfg.fps), "-i", "pipe:0"]
    if cfg.device.startswith("/dev/video"):
        cmd += ["-pix_fmt", cfg.pixfmt]
        if cfg.low_latency:
            cmd += ["-flush_packets", "1"]
        cmd += ["-f", "v4l2", cfg.device]
    else:
        cmd += ["-y", "-f", "rawvideo", cfg.device]
    return cmd


def ffmpeg_version():
    try:
        out = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, timeout=5).stdout
        return out.splitlines()[0] if out else "desconocida"
    except Exception:
        return "desconocida"


def device_holders(device):
    try:
        target = os.path.realpath(device)
    except OSError:
        return ""
    me = os.getpid()
    mine = {me}
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) in mine:
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                if int(f.read().rsplit(")", 1)[1].split()[1]) == me:
                    continue
        except (OSError, ValueError, IndexError):
            continue
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == target:
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
                        found.append(f"PID {pid}: {cmd[:120] or '?'}")
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return "\n".join(found)


def single_instance(device):
    import fcntl
    run_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    name = "gopro_session_webcam-" + re.sub(r"[^A-Za-z0-9]+", "_", device) + ".lock"
    path = os.path.join(run_dir, name)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        other = fh.read().strip() or "?"
        sys.exit(f"Ya hay otra copia del script usando {device} (PID {other}).\n"
                 f"Parala antes con:  ./start_gopro_cam.sh --stop")
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STANDBY_NAMES = ("conlost.png", "lost.png")


def standby_image_path(cfg):
    if cfg.standby_image:
        return cfg.standby_image
    for name in STANDBY_NAMES:
        path = os.path.join(SCRIPT_DIR, name)
        if os.path.isfile(path):
            return path
    return None


def _render_frame(cfg, inp, vf):
    w, h = cfg.size
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"] + inp +
            ["-vf", vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", "pipe:1"],
            capture_output=True, timeout=15)
        if out.returncode == 0 and len(out.stdout) == w * h * 3 // 2:
            return out.stdout
        log.debug("No se pudo generar el fotograma de espera: %s",
                  out.stderr.decode(errors="replace").strip()[-300:])
    except Exception as e:
        log.debug("No se pudo generar el fotograma de espera: %s", e)
    return None


def make_standby(cfg):
    w, h = cfg.size
    img = standby_image_path(cfg)
    if img:
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p")
        frame = _render_frame(cfg, ["-i", img], vf)
        if frame:
            log.debug("Imagen de espera: %s", img)
            return frame
        log.warning("No pude usar la imagen de espera %s (ver -v); uso el texto", img)
    vf = (f"drawtext=text='GoPro Session - sin senal':fontcolor=white:"
          f"fontsize={max(h // 20, 12)}:x=(w-text_w)/2:y=(h-text_h)/2,format=yuv420p")
    frame = _render_frame(cfg, ["-f", "lavfi", "-i", f"color=c=0x151515:s={w}x{h}"], vf)
    if frame:
        return frame
    log.debug("drawtext no disponible: imagen 'sin senal' lisa")
    return bytes([21]) * (w * h) + bytes([128]) * (w * h // 2)


def _mtime(path):
    try:
        return os.path.getmtime(path) if path else None
    except OSError:
        return None


class Bridge:
    def __init__(self, cfg, cam):
        self.cfg, self.cam = cfg, cam
        w, h = cfg.size
        self.fsize = w * h * 3 // 2
        self.standby = make_standby(cfg)
        self.standby_src = standby_image_path(cfg)
        self.standby_mtime = _mtime(self.standby_src)
        self.standby_check = time.monotonic()
        self.lock = threading.Condition()
        self.seq = 0
        self.latest, self.latest_t, self.gen = None, 0.0, 0
        self.nframes = 0
        self.stop = threading.Event()
        self.writer = None
        self.writer_t0 = 0.0
        self.writer_failures = 0
        self.last_writer_msg = None
        self.hinted = False

    def start_writer(self):
        self.writer = Proc("writer", writer_cmd(self.cfg), stdin=subprocess.PIPE)
        self.writer_t0 = time.monotonic()

    def _pick(self):
        with self.lock:
            frame, t = self.latest, self.latest_t
        if frame is None or time.time() - t > FRESH:
            self._refresh_standby()
            return self.standby
        return frame

    def _refresh_standby(self):
        now = time.monotonic()
        if now - self.standby_check < 3:
            return
        self.standby_check = now
        src = standby_image_path(self.cfg)
        mt = _mtime(src)
        if src != self.standby_src or mt != self.standby_mtime:
            self.standby_src, self.standby_mtime = src, mt
            threading.Thread(target=self._reload_standby, daemon=True).start()

    def _reload_standby(self):
        self.standby = make_standby(self.cfg)
        log.info("Imagen de espera actualizada (%s)", self.standby_src or "texto")

    def _emit(self, frame):
        try:
            if not self.writer.alive():
                raise BrokenPipeError
            self.writer.p.stdin.write(frame)
            self.writer.p.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            if self.stop.is_set():
                return False
            self.writer_failures += 1
            msg = self.writer.explain()
            if msg != self.last_writer_msg:
                log.error("El escritor de %s ha terminado: %s", self.cfg.device, msg)
                self.last_writer_msg = msg
            else:
                log.error("El escritor de %s sigue sin poder abrirse (intento %d)",
                          self.cfg.device, self.writer_failures)
            if "VIDIOC_G_FMT" in msg and not self.hinted:
                self.hinted = True
                held = device_holders(self.cfg.device)
                if held:
                    log.error("PISTA: %s esta abierto por otro programa y, con exclusive_caps=1, "
                              "no admite un escritor nuevo. Cierra esa app (o su videollamada) "
                              "y esto se recupera solo:\n  %s",
                              self.cfg.device, held.replace("\n", "\n  "))
                else:
                    log.error("PISTA: nadie de tu usuario tiene abierto %s, asi que v4l2loopback "
                              "se ha quedado atascado (pasa si una app lo estaba leyendo cuando se "
                              "corto el escritor anterior). Arreglo:  ./start_gopro_cam.sh --reset",
                              self.cfg.device)
            self.writer.stop()
            if self.stop.wait(min(2 * self.writer_failures, 10)):
                return False
            try:
                self.start_writer()
            except OSError as e:
                log.error("No se pudo relanzar ffmpeg: %s", e)
            return True
        if self.writer_failures and time.monotonic() - self.writer_t0 > 10:
            log.info("El escritor de %s funciona de nuevo", self.cfg.device)
            self.writer_failures, self.last_writer_msg, self.hinted = 0, None, False
        return True

    def pump(self):
        period = 1.0 / self.cfg.fps
        nxt = time.monotonic()
        while not self.stop.is_set():
            nxt += period
            if not self._emit(self._pick()):
                return
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -1:
                nxt = time.monotonic()

    def pump_event(self):
        period = 1.0 / self.cfg.fps
        seen = -1
        fresh = False
        while not self.stop.is_set():
            with self.lock:
                if self.seq == seen:
                    self.lock.wait(timeout=2 * period if fresh else period)
                seen = self.seq
                frame, t = self.latest, self.latest_t
            fresh = frame is not None and time.time() - t <= FRESH
            if not fresh:
                self._refresh_standby()
            if not self._emit(frame if fresh else self.standby):
                return

    def start_decoder(self):
        self.gen += 1
        gen = self.gen
        dec = Proc("decoder", decoder_cmd(self.cfg), stdout=subprocess.PIPE)

        def reader():
            out = dec.p.stdout
            while True:
                data = out.read(self.fsize)
                if len(data) != self.fsize:
                    return
                with self.lock:
                    if gen != self.gen:
                        return
                    self.latest, self.latest_t = data, time.time()
                    self.nframes += 1
                    self.seq += 1
                    self.lock.notify_all()

        threading.Thread(target=reader, daemon=True).start()
        return dec

    def camera_responding(self):
        for _ in range(2):
            if self.cam.alive():
                return True
            time.sleep(1)
        return False

    def session(self):
        cfg, cam = self.cfg, self.cam
        cam.remember_mac()
        if cfg.no_autooff:
            try:
                cam.set_autooff_never()
                log.info("Peticion 'sin apagado automatico' enviada (verifica con --dump)")
            except Exception as e:
                log.warning("No se pudo cambiar el apagado automatico: %s", e)
        if cfg.clean_sd:
            try:
                cam.delete_all_media()
                log.info("Contenido de la SD borrado (--clean-sd)")
            except Exception as e:
                log.warning("No se pudo borrar la SD: %s", e)

        ka = KeepAlive(cam.addr, cfg.keepalive_port)
        ka.start()
        failures = 0
        nostream_hint = False
        try:
            while not self.stop.is_set():
                dec = self.start_decoder()
                try:
                    resp = cam.start_stream()
                    log.debug("Respuesta de la camara al pedir el stream: %s",
                              resp.decode(errors="replace").strip()[:150])
                except Exception as e:
                    log.warning("Al pedir el stream: %s", e)
                if cfg.stream_bitrate:
                    try:
                        cam.set_stream_bitrate(cfg.stream_bitrate)
                        log.debug("Bitrate de stream pedido: %d bps", cfg.stream_bitrate)
                    except Exception as e:
                        log.warning("La camara no acepta el bitrate de stream (%s)", e)
                t0 = time.time()
                last_31 = None
                got, hot, last_thermal, last_report = False, False, 0.0, t0
                last_poll = 0.0
                dec_died = False
                frames_at_report = self.nframes
                reason = "parada solicitada"
                while not self.stop.is_set():
                    time.sleep(0.5)
                    now = time.time()
                    if not dec.alive():
                        dec_died = True
                        reason = "decodificador terminado: " + dec.explain()
                        break
                    with self.lock:
                        lt = self.latest_t
                    if lt >= t0 and not got:
                        got, failures = True, 0
                        log.info("Video recibido: la webcam virtual esta activa")
                    if not got:
                        st = cam.status()
                        n31 = (st or {}).get("status", {}).get("31")
                        if n31 != last_31 and n31 is not None:
                            log.info("Estado 31 de la camara (clientes registrados) = %s%s", n31,
                                     "" if str(n31) not in ("0", "") else "  -> aun no emite; sigo consultando")
                            last_31 = n31
                    ref = lt if got else t0
                    limit = cfg.stall_timeout if got else cfg.first_frame_timeout
                    if now - ref > limit:
                        reason = ("sin fotogramas durante %.0f s" % (now - ref))
                        break
                    if got and cfg.app_poll > 0 and now - last_poll >= cfg.app_poll:
                        last_poll = now
                        st = cam.status()
                        if st is None:
                            log.debug("Latido: la camara no contesto a /status")
                        else:
                            n31 = st.get("status", {}).get("31")
                            if n31 != last_31 and n31 is not None:
                                (log.warning if str(n31) == "0" else log.debug)(
                                    "Estado 31 (clientes registrados) = %s durante el stream", n31)
                                last_31 = n31
                            if cfg.thermal_guard and str(st.get("status", {}).get("6")) == "1":
                                reason, hot = "camara demasiado caliente", True
                                break
                    elif cfg.thermal_guard and now - last_thermal >= 10 and (
                            not got or cfg.app_poll <= 0):
                        last_thermal = now
                        st = cam.status()
                        if st and str(st.get("status", {}).get("6")) == "1":
                            reason, hot = "camara demasiado caliente", True
                            break
                    if cfg.verbose and now - last_report >= 30:
                        n = self.nframes
                        log.debug("fps medio ultimos 30 s: %.1f",
                                  (n - frames_at_report) / (now - last_report))
                        last_report, frames_at_report = now, n
                dec.stop()
                if self.stop.is_set():
                    break
                log.warning("Stream interrumpido (%s)", reason)
                if dec_died and cfg.low_latency and ("Unrecognized option" in reason
                                                      or "Option not found" in reason):
                    cfg.low_latency = False
                    log.warning("Tu ffmpeg no soporta alguna opcion del modo de baja latencia. "
                                "Paso al modo conservador (equivale a --no-low-latency). "
                                "Version: %s", ffmpeg_version())
                    continue
                if not got and not nostream_hint and not dec_died and self.cam.alive():
                    nostream_hint = True
                    log.warning("La camara responde por HTTP pero no emite video. Prueba: apagarla "
                                "del todo (boton superior >10 s) y encenderla; comprueba que su SD "
                                "no tenga cal.txt/no_shutdown.txt; y que no este caliente. "
                                "Con -v veras que contesta a la orden de stream.")
                if hot:
                    try:
                        cam.stop_stream()
                    except Exception:
                        pass
                    log.warning("Descanso de %d s para que la camara se enfrie", COOL_DOWN)
                    self.stop.wait(COOL_DOWN)
                    continue
                if not self.camera_responding():
                    log.info("La camara ya no responde (apagada o fuera de alcance)")
                    return
                failures += 1
                wait = min(2 * (failures - 1), 10)
                log.info("Reintentando el stream%s (intento %d)", " en %d s" % wait if wait else " ahora", failures)
                self.stop.wait(wait)
        finally:
            ka.stop()
            try:
                cam.stop_stream()
            except Exception:
                pass

    def run(self):
        self.start_writer()
        threading.Thread(target=self.pump_event if self.cfg.low_latency else self.pump,
                         daemon=True).start()
        announced, last_wake = False, 0.0
        while not self.stop.is_set():
            if self.cam.alive():
                log.info("Camara detectada en %s", self.cam.host)
                announced = False
                self.session()
                log.info("Sesion terminada, vuelvo a esperar a la camara")
            else:
                if not announced:
                    log.info("Esperando a la camara (enciende su WiFi y conecta el PC a ella)...")
                    announced = True
                if time.time() - last_wake >= 10:
                    self.cam.wake()
                    last_wake = time.time()
                self.stop.wait(2)
        self.writer.stop()


def iface_for(addr):
    try:
        out = subprocess.run(["ip", "-o", "route", "get", addr], capture_output=True,
                             text=True, timeout=3).stdout
        m = re.search(r"\bdev (\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    if addr.startswith("127."):
        return "lo"
    try:
        ipn = struct.unpack("<L", socket.inet_aton(addr))[0]
        best, best_key = None, (-1, 0)
        with open("/proc/net/route") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                dest, mask, metric = int(p[1], 16), int(p[7], 16), int(p[6])
                if ipn & mask == dest:
                    key = (bin(mask).count("1"), -metric)
                    if key > best_key:
                        best, best_key = p[0], key
        return best
    except (OSError, ValueError, IndexError):
        return None


def rx_bytes(iface):
    try:
        with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
            return int(f.read())
    except (OSError, ValueError, TypeError):
        return None


def probe_stream(cfg, cam, secs=6):
    if not cam.alive():
        sys.exit(f"La camara no responde en {cfg.ip}: enciende su WiFi y conecta el PC a ella.")
    ka = KeepAlive(cam.addr, cfg.keepalive_port)
    ka.start()
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "info", "-fflags", "nobuffer",
           "-f", "mpegts", "-i",
           f"udp://0.0.0.0:{UDP_PORT}?fifo_size=50000&overrun_nonfatal=1&timeout=15000000",
           "-t", str(secs), "-map", "0:v:0", "-f", "null", "-"]
    iface = iface_for(cam.addr)
    samples = []
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
    out = []
    drain = threading.Thread(target=lambda: out.append(proc.stderr.read()), daemon=True)
    drain.start()
    try:
        cam.start_stream()
        if cfg.stream_bitrate:
            try:
                cam.set_stream_bitrate(cfg.stream_bitrate)
                print(f"Bitrate pedido a la camara: {cfg.stream_bitrate} bps")
            except Exception as e:
                print(f"La camara no acepta el bitrate de stream ({e})")
        t0 = time.time()
        print("Escuchando el stream (consultando /status para despertar a la camara)...")
        while proc.poll() is None and time.time() - t0 < 30:
            time.sleep(0.5)
            cam.status()
            b = rx_bytes(iface) if iface else None
            if b is not None:
                samples.append((time.time(), b))
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait()
        drain.join(2)
        ka.stop()
        try:
            cam.stop_stream()
        except Exception:
            pass
    err = out[0] if out else ""
    m = re.search(r"Stream #0:\d+.*?: Video: (\w+)(?: \(([^)]*)\))?[^\n]*?, (\d+)x(\d+)", err)
    if not m:
        print("No llego video de la camara. Revisa el estado 31 con --dump y la pista de -v.")
        return
    codec, prof, w, h = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    fps = re.search(r"([\d.]+) (?:fps|tbr)", err.split("Stream #0:", 1)[1])
    rates = []
    for i, (t1, b1) in enumerate(samples):
        for t2, b2 in samples[i + 1:]:
            if t2 - t1 >= 1.0:
                rates.append((b2 - b1) * 8 / (t2 - t1) / 1000)
                break
    rates = sorted(r for r in rates if r > 50)
    kbps = rates[len(rates) // 2] if rates else None
    print()
    print(f"Lo que emite la camara: {codec}{' (' + prof + ')' if prof else ''} {w}x{h}"
          f"{', ' + fps.group(1) + ' fps' if fps else ''}"
          f"{', ~%d kb/s' % kbps if kbps else ''}")
    if kbps:
        print(f"(medido en la interfaz {iface}: incluye cabeceras TS/UDP/IP, asi que el bitrate de "
              f"video real es ~10-15% menor; si esa interfaz lleva otro trafico saldra inflado)")
    ow, oh = cfg.size
    print(f"Tu salida es {ow}x{oh}: ampliacion x{max(ow / w, oh / h):.2f} "
          f"(la imagen no gana detalle al ampliarla; --size {w}x{h} ahorra CPU)")
    if abs(w / h - ow / oh) / (ow / oh) > 0.03:
        print(f"Formato de la camara {w}:{h} distinto del de salida {ow}:{oh}: "
              f"ahora se aplica --fit {cfg.fit} (pad=barras, crop=recortar, stretch=deformar).")


def parse_args():
    p = argparse.ArgumentParser(
        description="GoPro HERO4 Session como webcam virtual (v4l2loopback)")
    p.add_argument("--ip", default="10.5.5.9", help="IP[:puerto] de la camara")
    p.add_argument("--device", default="/dev/video10", help="dispositivo v4l2loopback")
    p.add_argument("--size", default="1280x720", help="resolucion de salida (fija), ej. 1280x720")
    p.add_argument("--fps", type=int, default=30, help="FPS de salida (constantes)")
    p.add_argument("--pixfmt", default="yuv420p", choices=["yuv420p", "yuyv422"],
                   help="formato de pixel del dispositivo (yuyv422 = mas compatible)")
    p.add_argument("--quality", default="high", choices=["fast", "balanced", "high"],
                   help="calidad de imagen: fast (escalado rapido), balanced (lanczos + realce suave) "
                        "o high (por defecto: + quita bloques y ruido)")
    p.add_argument("--sharpen", type=float, default=None, metavar="X",
                   help="intensidad del realce de nitidez (0 = sin realce; por defecto 0.6)")
    p.add_argument("--no-low-latency", dest="low_latency", action="store_false",
                   help="desactiva las optimizaciones de latencia (modo mas conservador)")
    p.add_argument("--scaler", default=None,
                   choices=["fast_bilinear", "bilinear", "bicubic", "lanczos"],
                   help="algoritmo de escalado (por defecto segun --quality)")
    p.add_argument("--fit", default="stretch", choices=["pad", "crop", "stretch"],
                   help="si el formato de la camara no coincide con el de salida: pad=barras, crop=recortar, "
                        "stretch=deformar (por defecto: la Session emite 432x240, casi 16:9, y no se nota)")
    p.add_argument("--probe", "--info", dest="probe", action="store_true",
                   help="mide lo que emite la camara (resolucion, fps, bitrate) y sale")
    p.add_argument("--stream-bitrate", type=int, default=0, metavar="BPS",
                   help="EXPERIMENTAL: bitrate del stream de la camara (ej. 1000000)")
    p.add_argument("--standby-image", default=None, metavar="PNG",
                   help="imagen a mostrar sin camara (por defecto conlost.png o lost.png "
                        "junto al script; si no hay, texto 'sin senal')")
    p.add_argument("--mirror", action="store_true", help="espejo horizontal")
    p.add_argument("--rotate180", action="store_true", help="girar 180 grados")
    p.add_argument("--stall-timeout", type=float, default=6.0,
                   help="s sin video antes de reiniciar el stream")
    p.add_argument("--first-frame-timeout", type=float, default=25.0,
                   help="s de espera al primer fotograma tras pedir el stream")
    p.add_argument("--keepalive-port", type=int, default=UDP_PORT,
                   help=argparse.SUPPRESS)
    p.add_argument("--app-poll", type=float, default=2.5, metavar="S",
                   help="consulta /status cada S s durante el stream, como la app oficial; "
                        "evita el 'CONNECTION LOST' periodico de la camara (0 = desactivado)")
    p.add_argument("--mac", help="MAC de la camara para Wake-on-LAN (se aprende sola)")
    p.add_argument("--no-autooff", action="store_true",
                   help="intenta desactivar el apagado automatico (sin verificar)")
    p.add_argument("--thermal-guard", action="store_true",
                   help="para el stream si la camara informa de calor (sin verificar)")
    p.add_argument("--clean-sd", action="store_true",
                   help="DESTRUCTIVO: borra TODO el contenido de la SD al conectar")
    p.add_argument("--dump", action="store_true", help="muestra estado/ajustes de la camara y sale")
    p.add_argument("-v", "--verbose", action="store_true")
    cfg = p.parse_args()
    m = re.fullmatch(r"(\d+)x(\d+)", cfg.size)
    if not m or int(m[1]) % 2 or int(m[2]) % 2:
        p.error("--size debe ser AnchoxAlto con valores pares, ej. 1280x720")
    cfg.size = (int(m[1]), int(m[2]))
    return cfg


def main():
    cfg = parse_args()
    logging.basicConfig(level=logging.DEBUG if cfg.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    cam = Camera(cfg.ip, cfg.mac)

    if cfg.dump:
        try:
            print(json.dumps(json.loads(cam.get("/gp/gpControl/status")), indent=1))
        except Exception as e:
            sys.exit(f"No se pudo leer el estado de {cfg.ip}: {e}")
        return

    if not shutil.which(FFMPEG):
        sys.exit("No encuentro ffmpeg (sudo apt install ffmpeg)")
    if cfg.probe:
        probe_stream(cfg, cam)
        return
    if cfg.device.startswith("/dev/video") and not os.path.exists(cfg.device):
        sys.exit(f"No existe {cfg.device}. Usa ./start_gopro_cam.sh, que carga v4l2loopback.")

    lock = single_instance(cfg.device)
    bridge = Bridge(cfg, cam)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: bridge.stop.set())
    log.info("GoPro Session -> %s (%dx%d @ %d fps, %s)",
             cfg.device, cfg.size[0], cfg.size[1], cfg.fps, cfg.pixfmt)
    log.debug("%s", ffmpeg_version())
    try:
        bridge.run()
    finally:
        bridge.stop.set()
        if bridge.writer:
            bridge.writer.stop()
        log.info("Detenido")


if __name__ == "__main__":
    main()
