import asyncio
import base64
import io
import json
import math
import mimetypes
import os
import queue
import re
import secrets
import sys
import threading
import time
from pathlib import Path

import mss
import numpy as np
import sounddevice as sd
import websockets
from PIL import Image

from PySide6.QtCore import (
    Qt, QObject, Signal, QTimer, QPropertyAnimation, QEasingCurve,
    QSize, QUrl
)
from PySide6.QtGui import (
    QIcon, QPixmap, QColor, QDesktopServices, QAction, QPainter,
    QPainterPath, QFont
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QScrollArea, QFrame, QFileDialog, QMenu, QTabWidget,
    QComboBox, QSlider, QCheckBox, QColorDialog, QSystemTrayIcon, QStyle,
    QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QSizePolicy
)

SERVER_URL = "wss://sikkord-jrbh.onrender.com"
AUDIO_PROTOCOL = 3
SCREEN_PROTOCOL = 3

APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "Sikkord"
APP_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = APP_DIR / "session.json"
SETTINGS_FILE = APP_DIR / "settings.json"

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ASSETS = BASE / "assets"
LOGO = ASSETS / "sikkord_logo.png"
ICON = ASSETS / "sikkord.ico"
QSS = ASSETS / "theme.qss"

NETWORK_RATE = 16000
NETWORK_SAMPLES = 320   # 20 ms
PCM_BYTES = NETWORK_SAMPLES * 2

DEFAULT_SETTINGS = {
    "noise_suppression": True,
    "noise_strength": 48,
    "output_volume": 75,
    "input_device": None,
    "output_device": None,
    "screen_profile": "Eco 540p",
    "message_sound": True,
    "call_sound": True,
    "reduce_motion": False,
    "compact_mode": False,
    "accent": "#F0384F",
}

def resource(path: Path) -> str:
    return str(path)

def canonical_dm_room(a, b):
    x, y = sorted((a or "", b or ""), key=str.lower)
    return "dm:" + x.lower() + ":" + y.lower()

def fmt_seen(ts):
    if not ts:
        return "Bilinmiyor"
    d = max(0, int(time.time()) - int(ts))
    if d < 60: return "Az önce"
    if d < 3600: return f"{d//60} dk önce"
    if d < 86400: return f"{d//3600} sa önce"
    return f"{d//86400} gün önce"

def resample_linear(x, out_n):
    x = np.asarray(x, dtype=np.float32)
    if len(x) == out_n:
        return x
    if len(x) < 2:
        return np.zeros(out_n, dtype=np.float32)
    xp = np.linspace(0.0, 1.0, len(x), dtype=np.float32)
    fp = np.linspace(0.0, 1.0, out_n, dtype=np.float32)
    return np.interp(fp, xp, x).astype(np.float32)

class SignalBus(QObject):
    event = Signal(dict)
    connection = Signal(bool)
    voice_packet = Signal(str, bytes)
    screen_packet = Signal(str, bytes)
    media_status = Signal(str, bool, str)
    toast = Signal(str, str, str)

class LocalSettings:
    def __init__(self):
        self.data = dict(DEFAULT_SETTINGS)
        try:
            if SETTINGS_FILE.exists():
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.data.update(saved)
        except Exception:
            pass

    def save(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

class NetworkCore:
    def __init__(self, bus: SignalBus):
        self.bus = bus
        self.loop = None
        self.thread = None
        self.running = True
        self.connected = False
        self.token = ""
        self.username = ""

        self._main_send = None
        self._voice_send = None
        self._screen_send = None

        self.voice_room = None
        self.screen_room = None
        self.voice_joined = threading.Event()
        self.screen_joined = threading.Event()

        self._ensure_voice = False
        self._ensure_screen = False

    def start(self):
        self.thread = threading.Thread(target=self._thread_main, daemon=True, name="SikkordNet")
        self.thread.start()

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._main_send = asyncio.Queue(maxsize=300)
        self._voice_send = asyncio.Queue(maxsize=6)
        self._screen_send = asyncio.Queue(maxsize=2)
        self.loop.run_until_complete(self._runner())

    async def _runner(self):
        voice_task = asyncio.create_task(self._media_supervisor("voice"))
        screen_task = asyncio.create_task(self._media_supervisor("screen"))
        try:
            await self._main_supervisor()
        finally:
            self.running = False
            voice_task.cancel()
            screen_task.cancel()

    def _put_latest(self, q, item):
        if not q:
            return
        try:
            while q.full():
                q.get_nowait()
            q.put_nowait(item)
        except Exception:
            pass

    def send(self, payload: dict):
        if self.loop and self._main_send:
            self.loop.call_soon_threadsafe(self._put_latest, self._main_send, payload)

    def send_voice(self, data: bytes):
        if self.loop and self._voice_send:
            self.loop.call_soon_threadsafe(self._put_latest, self._voice_send, data)

    def send_screen(self, data: bytes):
        if self.loop and self._screen_send:
            self.loop.call_soon_threadsafe(self._put_latest, self._screen_send, data)

    def ensure_media(self, kind):
        if kind == "voice":
            self._ensure_voice = True
        else:
            self._ensure_screen = True

    def set_room(self, kind, room):
        if kind == "voice":
            self.voice_room = room
            self.voice_joined.clear()
            self._ensure_voice = bool(room)
        else:
            self.screen_room = room
            self.screen_joined.clear()
            self._ensure_screen = bool(room)

    def close(self):
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: None)

    async def _main_supervisor(self):
        while self.running:
            try:
                async with websockets.connect(
                    SERVER_URL, max_size=12_000_000,
                    ping_interval=12, ping_timeout=12,
                    open_timeout=12, close_timeout=2,
                    compression=None
                ) as ws:
                    self.connected = True
                    self.bus.connection.emit(True)

                    # Automatic login if a token exists.
                    try:
                        if SESSION_FILE.exists():
                            token = json.loads(SESSION_FILE.read_text(encoding="utf-8")).get("token", "")
                            if token:
                                await ws.send(json.dumps({"action":"token_login","token":token}, separators=(",",":")))
                    except Exception:
                        pass

                    sender = asyncio.create_task(self._main_sender(ws))
                    try:
                        async for raw in ws:
                            if isinstance(raw, bytes):
                                continue
                            try:
                                obj = json.loads(raw)
                            except Exception:
                                continue
                            if obj.get("type") == "login" and obj.get("ok"):
                                self.token = obj.get("token", "") or self.token
                                self.username = obj.get("username", "")
                            self.bus.event.emit(obj)
                    finally:
                        sender.cancel()
            except Exception as e:
                self.connected = False
                self.bus.connection.emit(False)
                await asyncio.sleep(2.0)

    async def _main_sender(self, ws):
        while self.running:
            payload = await self._main_send.get()
            try:
                await ws.send(json.dumps(payload, ensure_ascii=False, separators=(",",":")))
            except Exception:
                return

    async def _media_supervisor(self, kind):
        while self.running:
            ensure = self._ensure_voice if kind == "voice" else self._ensure_screen
            room = self.voice_room if kind == "voice" else self.screen_room
            if not ensure or not room or not self.token:
                await asyncio.sleep(.15)
                continue

            joined_event = self.voice_joined if kind == "voice" else self.screen_joined
            joined_event.clear()
            try:
                async with websockets.connect(
                    SERVER_URL, max_size=12_000_000,
                    ping_interval=10, ping_timeout=10,
                    open_timeout=10, close_timeout=2,
                    compression=None
                ) as ws:
                    proto = AUDIO_PROTOCOL if kind == "voice" else SCREEN_PROTOCOL
                    codec = "pcm16" if kind == "voice" else "jpeg"
                    await ws.send(json.dumps({
                        "action":"media_auth","token":self.token,
                        "kind":kind,"proto":proto,"codec":codec
                    }, separators=(",",":")))
                    auth = await asyncio.wait_for(ws.recv(), timeout=8)
                    if isinstance(auth, bytes):
                        continue
                    auth = json.loads(auth)
                    if not auth.get("ok"):
                        await asyncio.sleep(1)
                        continue

                    current_room = None
                    tx_task = asyncio.create_task(self._media_sender(ws, kind))
                    try:
                        while self.running:
                            target = self.voice_room if kind == "voice" else self.screen_room
                            if target != current_room:
                                joined_event.clear()
                                await ws.send(json.dumps({"action":"media_join","room":target}, separators=(",",":")))
                                current_room = target

                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=.20)
                            except asyncio.TimeoutError:
                                continue

                            if isinstance(raw, str):
                                try:
                                    ctrl = json.loads(raw)
                                except Exception:
                                    continue
                                if ctrl.get("type") == "media_joined":
                                    if ctrl.get("ok") and ctrl.get("room") == current_room:
                                        joined_event.set()
                                        self.bus.media_status.emit(kind, True, current_room or "")
                                    else:
                                        joined_event.clear()
                                continue

                            if kind == "voice" and raw[:1] == b"P" and len(raw) >= 2:
                                ln = raw[1]
                                if len(raw) >= 2 + ln + PCM_BYTES:
                                    speaker = raw[2:2+ln].decode("utf-8","ignore") or "?"
                                    pcm = raw[2+ln:2+ln+PCM_BYTES]
                                    self.bus.voice_packet.emit(speaker, pcm)
                            elif kind == "screen" and raw[:1] == b"S" and len(raw) >= 2:
                                ln = raw[1]
                                if len(raw) >= 2 + ln:
                                    speaker = raw[2:2+ln].decode("utf-8","ignore") or "?"
                                    self.bus.screen_packet.emit(speaker, raw[2+ln:])
                    finally:
                        tx_task.cancel()
            except Exception:
                joined_event.clear()
                self.bus.media_status.emit(kind, False, room or "")
                await asyncio.sleep(1.0)

    async def _media_sender(self, ws, kind):
        q = self._voice_send if kind == "voice" else self._screen_send
        while self.running:
            data = await q.get()
            try:
                await ws.send(data)
            except Exception:
                return

class NoiseSuppressor:
    def __init__(self):
        self.noise = 0.007
        self.prev = 0.0
        self.gain = 1.0

    def process(self, x, enabled, strength):
        x = np.asarray(x, dtype=np.float32)
        if not enabled or x.size == 0:
            return x
        # Gentle DC/high-pass filter, no aggressive AGC.
        y = np.empty_like(x)
        y[0] = x[0] - 0.94*self.prev
        if len(x) > 1:
            y[1:] = x[1:] - 0.94*x[:-1]
        self.prev = float(x[-1])
        rms = float(np.sqrt(np.mean(y*y)+1e-9))
        if rms < max(.025, self.noise*2.5):
            self.noise = .992*self.noise + .008*rms
        s = max(0.0, min(1.0, strength/100.0))
        threshold = max(.003, self.noise*(1.25 + s*.9))
        if rms < threshold:
            target = .15 + (1-s)*.20
        else:
            target = 1.0
        self.gain = .92*self.gain + .08*target
        return np.clip(y*self.gain, -.95, .95)

class AudioEngine:
    def __init__(self, net: NetworkCore, bus: SignalBus, settings: LocalSettings):
        self.net = net
        self.bus = bus
        self.settings = settings
        self.active = False
        self.muted = False
        self.room = None
        self.mode = None
        self.peer = None
        self.input_stream = None
        self.output_stream = None
        self.stop_evt = threading.Event()
        self.rx = {}
        self.rx_lock = threading.Lock()
        self.levels = {}
        self.fx = NoiseSuppressor()
        self.start_lock = threading.Lock()

        bus.voice_packet.connect(self.receive)

    def receive(self, speaker, pcm):
        try:
            a = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            self.levels[speaker] = (min(1.0, float(np.sqrt(np.mean(a*a)+1.0))/8500.0), time.monotonic())
        except Exception:
            pass
        with self.rx_lock:
            q = self.rx.get(speaker)
            if q is None:
                q = queue.Queue(maxsize=3)
                self.rx[speaker] = q
            try:
                while q.full():
                    q.get_nowait()
                q.put_nowait(pcm)
            except Exception:
                pass

    def start(self, room, mode="server", peer=None):
        with self.start_lock:
            if self.active:
                return
            self.active = True
        self.room = room
        self.mode = mode
        self.peer = peer
        self.stop_evt.clear()
        self.net.set_room("voice", room)
        self.net.ensure_media("voice")
        threading.Thread(target=self._worker, daemon=True, name="SikkordAudio").start()

    def _worker(self):
        if not self.net.voice_joined.wait(timeout=8):
            self.active = False
            self.bus.toast.emit("Ses bağlantısı", "Ses odasına bağlanılamadı.", "error")
            return
        try:
            din, dout = sd.default.device
            indev = self.settings.data.get("input_device")
            outdev = self.settings.data.get("output_device")
            indev = din if indev is None else int(indev)
            outdev = dout if outdev is None else int(outdev)
            if indev is None or int(indev) < 0:
                raise RuntimeError("Mikrofon bulunamadı.")
            if outdev is None or int(outdev) < 0:
                raise RuntimeError("Hoparlör/kulaklık bulunamadı.")

            in_rate = int(float(sd.query_devices(indev, "input").get("default_samplerate", 48000)))
            out_rate = int(float(sd.query_devices(outdev, "output").get("default_samplerate", 48000)))
            in_n = max(160, int(in_rate*.02))
            out_n = max(160, int(out_rate*.02))

            self.input_stream = sd.RawInputStream(
                device=indev, samplerate=in_rate, channels=1,
                dtype="int16", blocksize=in_n, latency="low"
            )
            self.output_stream = sd.RawOutputStream(
                device=outdev, samplerate=out_rate, channels=1,
                dtype="int16", blocksize=out_n, latency="low"
            )
            self.input_stream.start()
            self.output_stream.start()

            cap = threading.Thread(target=self._capture_loop, args=(in_n,), daemon=True)
            play = threading.Thread(target=self._playback_loop, args=(out_n,), daemon=True)
            cap.start(); play.start()
            while self.active and not self.stop_evt.wait(.15):
                pass
        except Exception as e:
            self.bus.toast.emit("Ses cihazı", str(e), "error")
        finally:
            self._close_streams()
            self.active = False

    def _capture_loop(self, in_n):
        while self.active and not self.stop_evt.is_set():
            try:
                raw, overflow = self.input_stream.read(in_n)
                if self.muted:
                    continue
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)/32768.0
                x = self.fx.process(
                    x,
                    bool(self.settings.data.get("noise_suppression", True)),
                    int(self.settings.data.get("noise_strength", 48))
                )
                net = resample_linear(x, NETWORK_SAMPLES)
                self.levels[self.net.username or "Ben"] = (
                    min(1.0, float(np.sqrt(np.mean(net*net)+1e-10))*4.0),
                    time.monotonic()
                )
                pcm = (np.clip(net, -.92, .92)*32767).astype("<i2").tobytes()
                self.net.send_voice(b"P"+pcm)
            except Exception:
                time.sleep(.004)

    def _playback_loop(self, out_n):
        silence = np.zeros(out_n, dtype=np.int16).tobytes()
        while self.active and not self.stop_evt.is_set():
            mixes = []
            with self.rx_lock:
                items = list(self.rx.items())
            for speaker, q in items:
                try:
                    raw = q.get_nowait()
                except queue.Empty:
                    continue
                try:
                    mixes.append(np.frombuffer(raw, dtype="<i2").astype(np.float32)/32768.0)
                except Exception:
                    pass
            try:
                if mixes:
                    y = np.sum(mixes, axis=0)
                    if len(mixes) > 1:
                        y /= math.sqrt(len(mixes))
                    peak = float(np.max(np.abs(y))+1e-9)
                    if peak > .92:
                        y *= .92/peak
                    y *= max(0.0, min(1.0, self.settings.data.get("output_volume",75)/100.0))
                    y = resample_linear(y, out_n)
                    self.output_stream.write((np.clip(y,-.94,.94)*32767).astype(np.int16).tobytes())
                else:
                    self.output_stream.write(silence)
            except Exception:
                time.sleep(.004)

    def _close_streams(self):
        for s in (self.input_stream, self.output_stream):
            try:
                if s:
                    s.stop(); s.close()
            except Exception:
                pass
        self.input_stream = None
        self.output_stream = None
        with self.rx_lock:
            self.rx.clear()

    def stop(self):
        self.active = False
        self.stop_evt.set()
        self.net.set_room("voice", None)
        self._close_streams()

class ScreenEngine:
    def __init__(self, net: NetworkCore, bus: SignalBus, settings: LocalSettings):
        self.net = net
        self.bus = bus
        self.settings = settings
        self.active = False
        self.room = None
        self.stop_evt = threading.Event()
        self.last_frame = None
        self.last_sender = None
        self.frame_signal = SignalBus()
        bus.screen_packet.connect(self.receive)

    def receive(self, speaker, data):
        self.last_sender = speaker
        self.last_frame = data
        self.bus.event.emit({"type":"qt_screen_frame","username":speaker,"data":data})

    def start(self, room):
        if self.active:
            return
        self.active = True
        self.room = room
        self.stop_evt.clear()
        self.net.set_room("screen", room)
        self.net.ensure_media("screen")
        threading.Thread(target=self._worker, daemon=True, name="SikkordScreen").start()

    def _worker(self):
        if not self.net.screen_joined.wait(timeout=8):
            self.active = False
            self.bus.toast.emit("Ekran paylaşımı", "Ekran medya odasına bağlanılamadı.", "error")
            return
        profile = str(self.settings.data.get("screen_profile","Eco 540p"))
        if profile.startswith("Dengeli"):
            max_size=(1280,720); fps=6; quality=62
        elif profile.startswith("Net"):
            max_size=(1600,900); fps=6; quality=68
        else:
            max_size=(960,540); fps=5; quality=54

        try:
            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors)>1 else sct.monitors[0]
                interval = 1.0/fps
                while self.active and not self.stop_evt.is_set():
                    st=time.perf_counter()
                    shot=sct.grab(mon)
                    im=Image.frombytes("RGB",shot.size,shot.bgra,"raw","BGRX")
                    im.thumbnail(max_size,Image.Resampling.BILINEAR)
                    b=io.BytesIO()
                    im.save(b,"JPEG",quality=quality,optimize=False)
                    data=b.getvalue()
                    self.last_frame=data
                    self.last_sender=self.net.username or "Ben"
                    self.bus.event.emit({"type":"qt_screen_frame","username":self.last_sender,"data":data})
                    self.net.send_screen(b"S"+data)
                    time.sleep(max(0.0,interval-(time.perf_counter()-st)))
        except Exception as e:
            self.bus.toast.emit("Ekran paylaşımı", str(e), "error")
        finally:
            self.active=False

    def stop(self):
        self.active=False
        self.stop_evt.set()
        self.net.set_room("screen", None)

def circular_pixmap(data, size=44):
    pm = QPixmap(size,size)
    pm.fill(Qt.transparent)
    if data:
        try:
            raw = base64.b64decode(data)
            src = QPixmap()
            src.loadFromData(raw)
            src = src.scaled(size,size,Qt.KeepAspectRatioByExpanding,Qt.SmoothTransformation)
            p=QPainter(pm);p.setRenderHint(QPainter.Antialiasing)
            path=QPainterPath();path.addEllipse(0,0,size,size);p.setClipPath(path)
            p.drawPixmap(0,0,src);p.end()
            return pm
        except Exception:
            pass
    p=QPainter(pm);p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#451821"));p.setPen(Qt.NoPen);p.drawEllipse(0,0,size,size);p.end()
    return pm

class PremiumDialog(QDialog):
    def __init__(self, parent, title, text, kind="info", buttons=("Tamam",)):
        super().__init__(parent)
        self.setObjectName("PremiumDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.result_name = None
        lay=QVBoxLayout(self); lay.setContentsMargins(26,24,26,24); lay.setSpacing(15)
        icon=QLabel("◆" if kind=="info" else ("!" if kind=="warning" else "×"))
        icon.setStyleSheet("font-size:28px;color:#F0384F;font-weight:800;")
        lay.addWidget(icon)
        t=QLabel(title);t.setStyleSheet("font-size:20px;font-weight:800;");lay.addWidget(t)
        body=QLabel(text);body.setWordWrap(True);body.setStyleSheet("color:#AAB3C1;line-height:1.4;");lay.addWidget(body)
        row=QHBoxLayout();row.addStretch()
        for i,b in enumerate(buttons):
            btn=QPushButton(b)
            if i==0: btn.setProperty("accent",True)
            btn.clicked.connect(lambda _, name=b:self._done(name))
            row.addWidget(btn)
        lay.addLayout(row)

    def _done(self,name):
        self.result_name=name
        self.accept()

class Toast(QFrame):
    def __init__(self,parent,title,text,kind="info"):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setFixedWidth(350)
        lay=QHBoxLayout(self);lay.setContentsMargins(14,12,14,12)
        dot=QLabel("●");dot.setStyleSheet("color:#F0384F;font-size:18px;");lay.addWidget(dot)
        col=QVBoxLayout()
        a=QLabel(title);a.setStyleSheet("font-weight:800;")
        b=QLabel(text);b.setWordWrap(True);b.setStyleSheet("color:#9DA7B6;")
        col.addWidget(a);col.addWidget(b);lay.addLayout(col,1)
        self.effect=QGraphicsOpacityEffect(self);self.setGraphicsEffect(self.effect)
        self.anim=QPropertyAnimation(self.effect,b"opacity",self)
        self.anim.setDuration(180);self.anim.setStartValue(0);self.anim.setEndValue(1);self.anim.start()
        QTimer.singleShot(3500,self.fade_out)

    def fade_out(self):
        self.anim=QPropertyAnimation(self.effect,b"opacity",self)
        self.anim.setDuration(220);self.anim.setStartValue(1);self.anim.setEndValue(0)
        self.anim.finished.connect(self.deleteLater);self.anim.start()

class MessageBubble(QFrame):
    def __init__(self, app, msg):
        super().__init__()
        self.app=app;self.msg=msg
        who=msg.get("username") or msg.get("sender") or "?"
        mine=who==app.user
        self.setObjectName("MessageBubbleSelf" if mine else "MessageBubbleOther")
        lay=QVBoxLayout(self);lay.setContentsMargins(12,9,12,9);lay.setSpacing(4)

        top=QHBoxLayout()
        author=QLabel(who);author.setObjectName("MessageAuthor")
        if msg.get("color"): author.setStyleSheet(f"font-weight:800;color:{msg['color']};")
        top.addWidget(author);top.addStretch()
        meta=QLabel(time.strftime("%H:%M",time.localtime(msg.get("created_at",time.time()))));meta.setObjectName("MessageMeta")
        top.addWidget(meta);lay.addLayout(top)

        if msg.get("reply_to"):
            r=QLabel(f"↪ #{msg['reply_to']} mesajına yanıt");r.setObjectName("MessageMeta");lay.addWidget(r)

        text=msg.get("text")
        if text is None:text=msg.get("message","")
        if msg.get("deleted"):
            body=QLabel("[mesaj silindi]");body.setStyleSheet("color:#687487;font-style:italic;");lay.addWidget(body)
        elif text:
            body=QLabel(str(text));body.setWordWrap(True);body.setTextInteractionFlags(Qt.TextSelectableByMouse);body.setObjectName("MessageBody");lay.addWidget(body)
            match=re.search(r"sikkord://join/([A-Z0-9-]+)",str(text),re.I)
            if match:
                btn=QPushButton("🔗  SUNUCUYA KATIL")
                btn.setProperty("accent",True)
                btn.clicked.connect(lambda:self.app.net.send({"action":"join","code":match.group(1).upper()}))
                lay.addWidget(btn,0,Qt.AlignLeft)

        if msg.get("attachment_name"):
            att=QPushButton(f"📎  {msg.get('attachment_name')}  •  AÇ")
            att.clicked.connect(lambda:self.app.net.send({
                "action":"attachment_get","id":msg.get("id"),
                "scope":"dm" if app.mode=="dm" else "server"
            }))
            lay.addWidget(att,0,Qt.AlignLeft)

        if app.mode=="dm" and msg.get("sender")==app.user:
            mark="✓ Gönderildi"
            if msg.get("delivered_at"):mark="✓✓ Teslim"
            if msg.get("read_at"):mark="✓✓ Okundu"
            rr=QLabel(mark);rr.setObjectName("MessageMeta");lay.addWidget(rr,0,Qt.AlignRight)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.menu)

    def menu(self,pos):
        m=QMenu(self)
        reply=m.addAction("↩ Yanıtla")
        info=m.addAction("ⓘ Mesaj bilgisi") if self.app.mode=="server" else None
        delete=None
        who=self.msg.get("username") or self.msg.get("sender")
        if who==self.app.user:
            delete=m.addAction("🗑 Mesajı sil")
        act=m.exec(self.mapToGlobal(pos))
        if act==reply:
            self.app.reply_to=self.msg.get("id")
            self.app.reply_label.setText(f"↪ #{self.app.reply_to} mesajına yanıt veriyorsun")
        elif info and act==info:
            self.app.net.send({"action":"message_info","id":self.msg.get("id")})
        elif delete and act==delete:
            self.app.net.send({"action":"delete_dm" if self.app.mode=="dm" else "delete_server_message","id":self.msg.get("id")})

class ParticipantCard(QFrame):
    def __init__(self,name,avatar=None):
        super().__init__()
        self.name=name;self.setObjectName("ParticipantCard");self.setMinimumSize(175,135)
        lay=QVBoxLayout(self);lay.setAlignment(Qt.AlignCenter)
        av=QLabel();av.setFixedSize(64,64);av.setPixmap(circular_pixmap(avatar,64));av.setScaledContents(True)
        av.setAlignment(Qt.AlignCenter);lay.addWidget(av,0,Qt.AlignCenter)
        self.label=QLabel(name);self.label.setAlignment(Qt.AlignCenter);self.label.setStyleSheet("font-weight:800;");lay.addWidget(self.label)
        self.state=QLabel("Aramada");self.state.setAlignment(Qt.AlignCenter);self.state.setStyleSheet("color:#42D88B;font-size:11px;");lay.addWidget(self.state)

    def set_speaking(self,on):
        self.setObjectName("ParticipantSpeaking" if on else "ParticipantCard")
        self.style().unpolish(self);self.style().polish(self)

class CallWindow(QDialog):
    hangup = Signal()
    mute = Signal()
    share = Signal()

    def __init__(self,app):
        super().__init__(app)
        self.app=app
        self.setObjectName("CallWindow")
        self.setWindowTitle("SIKKORD • Arama")
        self.resize(1180,760)
        self.cards={}
        lay=QVBoxLayout(self);lay.setContentsMargins(12,12,12,12);lay.setSpacing(10)

        top=QHBoxLayout()
        title=QLabel("SIKKORD ARAMA");title.setStyleSheet("font-size:16px;font-weight:800;");top.addWidget(title)
        self.state=QLabel("Bağlanıyor...");self.state.setObjectName("WarningText");top.addWidget(self.state)
        top.addStretch()
        self.mute_btn=QPushButton("🎙 MİKROFON");self.mute_btn.clicked.connect(self.mute.emit);top.addWidget(self.mute_btn)
        self.share_btn=QPushButton("🖥 EKRAN PAYLAŞ");self.share_btn.clicked.connect(self.share.emit);top.addWidget(self.share_btn)
        end=QPushButton("📵 ARAMAYI BİTİR");end.setProperty("danger",True);end.clicked.connect(self.hangup.emit);top.addWidget(end)
        lay.addLayout(top)

        self.people=QHBoxLayout();self.people.setSpacing(10);lay.addLayout(self.people)

        self.screen=QLabel("Ekran paylaşımı yok")
        self.screen.setObjectName("ScreenFrame")
        self.screen.setAlignment(Qt.AlignCenter)
        self.screen.setMinimumHeight(480)
        self.screen.setStyleSheet("color:#536071;font-size:15px;")
        lay.addWidget(self.screen,1)

        self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(120)

    def set_participants(self,users):
        names={u.get("username") for u in users}
        for n in list(self.cards):
            if n not in names:
                w=self.cards.pop(n);self.people.removeWidget(w);w.deleteLater()
        for u in users:
            n=u.get("username","?")
            if n not in self.cards:
                c=ParticipantCard(n,u.get("avatar"));self.cards[n]=c;self.people.addWidget(c)
            self.cards[n].state.setText("Mikrofon kapalı" if u.get("muted") else "Aramada")

    def tick(self):
        now=time.monotonic()
        for name,card in self.cards.items():
            lvl=self.app.audio.levels.get(name,(0,0))
            card.set_speaking(lvl[1]>now-.35 and lvl[0]>.035)

    def show_frame(self,data,who):
        pm=QPixmap();pm.loadFromData(data)
        if pm.isNull():return
        pm=pm.scaled(self.screen.size(),Qt.KeepAspectRatio,Qt.SmoothTransformation)
        self.screen.setPixmap(pm)
        self.screen.setToolTip(f"{who} ekran paylaşıyor")

    def clear_frame(self):
        self.screen.setPixmap(QPixmap());self.screen.setText("Ekran paylaşımı yok")

    def closeEvent(self,e):
        # Hide only. Call stays active and main UI's GÖSTER opens it again.
        e.ignore();self.hide()

class SettingsDialog(QDialog):
    def __init__(self,app):
        super().__init__(app)
        self.app=app
        self.setWindowTitle("SIKKORD • Ayarlar")
        self.resize(820,690)
        root=QVBoxLayout(self);root.setContentsMargins(22,20,22,20)
        h=QLabel("AYARLAR");h.setObjectName("PageTitle");root.addWidget(h)
        tabs=QTabWidget();root.addWidget(tabs,1)

        # Profile
        p=QWidget();pl=QVBoxLayout(p);pl.setContentsMargins(20,20,20,20);pl.setSpacing(12)
        pl.addWidget(QLabel("Profil"))
        self.name=QLineEdit(app.user);self.name.setPlaceholderText("Kullanıcı adı");pl.addWidget(self.name)
        color_row=QHBoxLayout()
        self.color=QLineEdit(app.user_color);self.color.setReadOnly(True);color_row.addWidget(self.color)
        choose=QPushButton("🎨 İsim rengini seç");choose.clicked.connect(self.pick_color);color_row.addWidget(choose);pl.addLayout(color_row)
        avatar=QPushButton("📷 Profil fotoğrafı değiştir");avatar.clicked.connect(app.choose_avatar);pl.addWidget(avatar)
        save=QPushButton("PROFİLİ KAYDET");save.setProperty("accent",True);save.clicked.connect(self.save_profile);pl.addWidget(save)
        pl.addStretch();tabs.addTab(p,"👤 Profil")

        # Audio
        a=QWidget();al=QVBoxLayout(a);al.setContentsMargins(20,20,20,20);al.setSpacing(12)
        self.inputs=QComboBox();self.outputs=QComboBox()
        self.input_map=[];self.output_map=[]
        try:
            for i,d in enumerate(sd.query_devices()):
                if d["max_input_channels"]>0:
                    self.input_map.append(i);self.inputs.addItem(f"{i} • {d['name']}")
                if d["max_output_channels"]>0:
                    self.output_map.append(i);self.outputs.addItem(f"{i} • {d['name']}")
        except Exception:pass
        al.addWidget(QLabel("Mikrofon"));al.addWidget(self.inputs)
        al.addWidget(QLabel("Hoparlör / Kulaklık"));al.addWidget(self.outputs)
        self.noise=QCheckBox("Gürültü engelleme");self.noise.setChecked(bool(app.settings.data["noise_suppression"]));al.addWidget(self.noise)
        al.addWidget(QLabel("Gürültü engelleme gücü"))
        self.noise_strength=QSlider(Qt.Horizontal);self.noise_strength.setRange(0,100);self.noise_strength.setValue(int(app.settings.data["noise_strength"]));al.addWidget(self.noise_strength)
        al.addWidget(QLabel("Çıkış ses seviyesi"))
        self.vol=QSlider(Qt.Horizontal);self.vol.setRange(0,100);self.vol.setValue(int(app.settings.data["output_volume"]));al.addWidget(self.vol)
        test=QPushButton("🔊 Hoparlörü test et");test.clicked.connect(self.test_speaker);al.addWidget(test)
        savea=QPushButton("SES AYARLARINI KAYDET");savea.setProperty("accent",True);savea.clicked.connect(self.save_audio);al.addWidget(savea)
        al.addStretch();tabs.addTab(a,"🎙 Ses")

        # Appearance
        ap=QWidget();apl=QVBoxLayout(ap);apl.setContentsMargins(20,20,20,20);apl.setSpacing(12)
        self.motion=QCheckBox("Animasyonları azalt");self.motion.setChecked(bool(app.settings.data["reduce_motion"]));apl.addWidget(self.motion)
        self.compact=QCheckBox("Kompakt mesaj görünümü");self.compact.setChecked(bool(app.settings.data["compact_mode"]));apl.addWidget(self.compact)
        apl.addWidget(QLabel("Tema: Premium Siyah / Kırmızı"))
        savep=QPushButton("GÖRÜNÜMÜ KAYDET");savep.setProperty("accent",True);savep.clicked.connect(self.save_appearance);apl.addWidget(savep);apl.addStretch()
        tabs.addTab(ap,"✨ Görünüm")

        # Notifications
        n=QWidget();nl=QVBoxLayout(n);nl.setContentsMargins(20,20,20,20);nl.setSpacing(12)
        self.msgsound=QCheckBox("Mesaj bildirim sesi");self.msgsound.setChecked(bool(app.settings.data["message_sound"]));nl.addWidget(self.msgsound)
        self.callsound=QCheckBox("Arama zil sesi");self.callsound.setChecked(bool(app.settings.data["call_sound"]));nl.addWidget(self.callsound)
        sn=QPushButton("BİLDİRİMLERİ KAYDET");sn.setProperty("accent",True);sn.clicked.connect(self.save_notifications);nl.addWidget(sn);nl.addStretch()
        tabs.addTab(n,"🔔 Bildirim")

        # Performance
        perf=QWidget();pf=QVBoxLayout(perf);pf.setContentsMargins(20,20,20,20);pf.setSpacing(12)
        self.screenq=QComboBox();self.screenq.addItems(["Eco 540p","Dengeli 720p","Net 900p"])
        idx=self.screenq.findText(app.settings.data.get("screen_profile","Eco 540p"))
        if idx>=0:self.screenq.setCurrentIndex(idx)
        pf.addWidget(QLabel("Ekran paylaşım kalitesi"));pf.addWidget(self.screenq)
        info=QLabel("Öneri: Eco 540p en akıcı ve en düşük CPU kullanımıdır. Arayüz ağı ve ses işleme ana UI thread'inden ayrıdır.")
        info.setWordWrap(True);info.setObjectName("SubtleText");pf.addWidget(info)
        sp=QPushButton("PERFORMANS AYARLARINI KAYDET");sp.setProperty("accent",True);sp.clicked.connect(self.save_performance);pf.addWidget(sp);pf.addStretch()
        tabs.addTab(perf,"⚡ Performans")

    def pick_color(self):
        c=QColorDialog.getColor(QColor(self.color.text()),self,"İsim rengi")
        if c.isValid():self.color.setText(c.name())

    def save_profile(self):
        self.app.net.send({"action":"profile_update","username":self.name.text().strip(),"color":self.color.text().strip()})
        self.app.toast("Profil","Profil güncellemesi gönderildi.","info")

    def save_audio(self):
        if self.inputs.currentIndex()>=0:self.app.settings.data["input_device"]=self.input_map[self.inputs.currentIndex()]
        if self.outputs.currentIndex()>=0:self.app.settings.data["output_device"]=self.output_map[self.outputs.currentIndex()]
        self.app.settings.data["noise_suppression"]=self.noise.isChecked()
        self.app.settings.data["noise_strength"]=self.noise_strength.value()
        self.app.settings.data["output_volume"]=self.vol.value()
        self.app.settings.save();self.app.toast("Ses","Ses ayarları kaydedildi.","info")

    def save_appearance(self):
        self.app.settings.data["reduce_motion"]=self.motion.isChecked()
        self.app.settings.data["compact_mode"]=self.compact.isChecked()
        self.app.settings.save();self.app.toast("Görünüm","Görünüm ayarları kaydedildi.","info")

    def save_notifications(self):
        self.app.settings.data["message_sound"]=self.msgsound.isChecked()
        self.app.settings.data["call_sound"]=self.callsound.isChecked()
        self.app.settings.save();self.app.toast("Bildirimler","Bildirim ayarları kaydedildi.","info")

    def save_performance(self):
        self.app.settings.data["screen_profile"]=self.screenq.currentText()
        self.app.settings.save();self.app.toast("Performans","Performans ayarları kaydedildi.","info")

    def test_speaker(self):
        def worker():
            try:
                rate=44100
                t=np.linspace(0,.3,int(rate*.3),False)
                x=(np.sin(2*np.pi*660*t)*.12).astype(np.float32)
                sd.play(x,rate);sd.wait()
            except Exception as e:self.app.bus.toast.emit("Hoparlör testi",str(e),"error")
        threading.Thread(target=worker,daemon=True).start()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SIKKORD")
        self.resize(1480,900)
        self.setMinimumSize(1120,720)
        if ICON.exists():self.setWindowIcon(QIcon(resource(ICON)))
        if QSS.exists():self.setStyleSheet(QSS.read_text(encoding="utf-8"))

        self.bus=SignalBus()
        self.settings=LocalSettings()
        self.net=NetworkCore(self.bus)
        self.audio=AudioEngine(self.net,self.bus,self.settings)
        self.screen_engine=ScreenEngine(self.net,self.bus,self.settings)

        self.user=""
        self.user_color="#F4F5F7"
        self.avatar=None
        self.servers=[]
        self.friends=[]
        self.friend_requests=[]
        self.members=[]
        self.server_code=""
        self.server_name=""
        self.mode="server"
        self.dm_peer=None
        self.current_history=[]
        self.reply_to=None
        self.call_window=None
        self.call_state="idle"
        self.call_peer=None
        self.call_started_at=None
        self.remote_screen_user=None
        self.pending_attachment=None

        self.bus.event.connect(self.handle_event)
        self.bus.connection.connect(self.connection_changed)
        self.bus.toast.connect(self.toast)
        self.net.start()

        self.build_login()
        self.call_ui_timer=QTimer(self);self.call_ui_timer.timeout.connect(self.update_call_banner);self.call_ui_timer.start(500)

    # ---------- generic UI ----------
    def clear_central(self):
        old=self.centralWidget()
        if old:old.deleteLater()

    def fade(self,w):
        if self.settings.data.get("reduce_motion"):return
        fx=QGraphicsOpacityEffect(w);w.setGraphicsEffect(fx)
        a=QPropertyAnimation(fx,b"opacity",w);a.setDuration(150);a.setStartValue(.35);a.setEndValue(1);a.setEasingCurve(QEasingCurve.OutCubic)
        a.finished.connect(lambda:w.setGraphicsEffect(None));a.start()
        w._fade_anim=a

    def modal(self,title,text,kind="info",buttons=("Tamam",)):
        d=PremiumDialog(self,title,text,kind,buttons);d.exec();return d.result_name

    def toast(self,title,text,kind="info"):
        t=Toast(self,title,text,kind);t.show()
        t.adjustSize()
        t.move(self.width()-t.width()-28,self.height()-t.height()-34)
        t.raise_()

    def resizeEvent(self,e):
        super().resizeEvent(e)
        for w in self.findChildren(Toast):
            if w.isVisible():w.move(self.width()-w.width()-28,self.height()-w.height()-34)

    def connection_changed(self,on):
        if hasattr(self,"status"):
            self.status.setText("● BAĞLI" if on else "● YENİDEN BAĞLANIYOR")
            self.status.setObjectName("OnlineText" if on else "WarningText")
            self.status.style().unpolish(self.status);self.status.style().polish(self.status)

    # ---------- login ----------
    def build_login(self):
        self.clear_central()
        root=QWidget();self.setCentralWidget(root)
        layout=QHBoxLayout(root);layout.setContentsMargins(0,0,0,0)
        visual=QFrame();visual.setStyleSheet("background:#05070A;border-right:1px solid #1D2430;")
        vl=QVBoxLayout(visual);vl.setContentsMargins(50,50,50,50);vl.addStretch()
        if LOGO.exists():
            logo=QLabel();pm=QPixmap(resource(LOGO)).scaled(420,420,Qt.KeepAspectRatio,Qt.SmoothTransformation)
            logo.setPixmap(pm);logo.setAlignment(Qt.AlignCenter)
            shadow=QGraphicsDropShadowEffect(logo);shadow.setBlurRadius(55);shadow.setColor(QColor("#D81F35"));shadow.setOffset(0,0);logo.setGraphicsEffect(shadow)
            vl.addWidget(logo)
        brand=QLabel("SIKKORD");brand.setAlignment(Qt.AlignCenter);brand.setStyleSheet("font-size:38px;font-weight:900;color:#FF4055;");vl.addWidget(brand)
        sub=QLabel("Konuş • Yazış • Paylaş\nPremium siyah / kırmızı iletişim deneyimi")
        sub.setAlignment(Qt.AlignCenter);sub.setStyleSheet("color:#7F8999;font-size:14px;");vl.addWidget(sub);vl.addStretch()
        layout.addWidget(visual,3)

        right=QFrame();right.setStyleSheet("background:#0B0E13;")
        rl=QVBoxLayout(right);rl.setContentsMargins(75,75,75,75);rl.addStretch()
        h=QLabel("Tekrar hoş geldin.");h.setStyleSheet("font-size:28px;font-weight:900;");rl.addWidget(h)
        s=QLabel("Arkadaşlarına bağlanmak için giriş yap.");s.setObjectName("SubtleText");rl.addWidget(s)
        rl.addSpacing(20)
        self.login_user=QLineEdit();self.login_user.setPlaceholderText("Kullanıcı adı");rl.addWidget(self.login_user)
        self.login_pass=QLineEdit();self.login_pass.setEchoMode(QLineEdit.Password);self.login_pass.setPlaceholderText("Şifre");rl.addWidget(self.login_pass)
        enter=QPushButton("GİRİŞ YAP");enter.setProperty("accent",True);enter.clicked.connect(self.do_login);rl.addWidget(enter)
        reg=QPushButton("KAYIT OL");reg.clicked.connect(self.do_register);rl.addWidget(reg)
        self.status=QLabel("● SUNUCUYA BAĞLANIYOR");self.status.setObjectName("WarningText");rl.addWidget(self.status)
        rl.addStretch();layout.addWidget(right,2)
        self.fade(root)

    def do_login(self):
        self.net.send({"action":"login","username":self.login_user.text().strip(),"password":self.login_pass.text()})

    def do_register(self):
        self.net.send({"action":"register","username":self.login_user.text().strip(),"password":self.login_pass.text()})

    # ---------- main UI ----------
    def build_main(self):
        self.clear_central()
        root=QWidget();self.setCentralWidget(root)
        all=QVBoxLayout(root);all.setContentsMargins(0,0,0,0);all.setSpacing(0)

        top=QFrame();top.setObjectName("TopBar");top.setFixedHeight(62)
        tl=QHBoxLayout(top);tl.setContentsMargins(14,8,14,8)
        if LOGO.exists():
            l=QLabel();l.setPixmap(QPixmap(resource(LOGO)).scaled(42,42,Qt.KeepAspectRatio,Qt.SmoothTransformation));tl.addWidget(l)
        brand=QLabel("SIKKORD");brand.setObjectName("BrandText");tl.addWidget(brand)
        self.status=QLabel("● BAĞLI");self.status.setObjectName("OnlineText");tl.addWidget(self.status)
        tl.addStretch()
        self.call_top=QPushButton("📞  ARAMA");self.call_top.setProperty("accent",True);self.call_top.clicked.connect(self.call_clicked);tl.addWidget(self.call_top)
        sett=QPushButton("⚙  AYARLAR");sett.clicked.connect(lambda:SettingsDialog(self).exec());tl.addWidget(sett)
        all.addWidget(top)

        body=QHBoxLayout();body.setContentsMargins(0,0,0,0);body.setSpacing(0)
        holder=QWidget();holder.setLayout(body);all.addWidget(holder,1)

        rail=QFrame();rail.setObjectName("Rail");rail.setFixedWidth(112);rr=QVBoxLayout(rail);rr.setContentsMargins(8,14,8,10)
        self.nav_servers=QPushButton("🏠  SUNUCU");self.nav_servers.setProperty("nav",True);self.nav_servers.clicked.connect(self.show_servers)
        self.nav_friends=QPushButton("👥  ARKADAŞ");self.nav_friends.setProperty("nav",True);self.nav_friends.clicked.connect(self.show_friends)
        add=QPushButton("＋  YENİ");add.setProperty("accent",True);add.clicked.connect(self.create_server)
        rr.addWidget(self.nav_servers);rr.addWidget(self.nav_friends);rr.addWidget(add);rr.addStretch()
        body.addWidget(rail)

        side=QFrame();side.setObjectName("SidePanel");side.setFixedWidth(265);sl=QVBoxLayout(side);sl.setContentsMargins(12,14,12,12)
        self.side_title=QLabel("SUNUCULAR");self.side_title.setObjectName("SectionTitle");sl.addWidget(self.side_title)
        self.side_list=QListWidget();self.side_list.itemClicked.connect(self.side_clicked);self.side_list.setContextMenuPolicy(Qt.CustomContextMenu);self.side_list.customContextMenuRequested.connect(self.side_menu);sl.addWidget(self.side_list,1)
        self.profile_card=QFrame();self.profile_card.setObjectName("Card");pc=QHBoxLayout(self.profile_card);pc.setContentsMargins(8,8,8,8)
        self.profile_avatar=QLabel();self.profile_avatar.setFixedSize(38,38);pc.addWidget(self.profile_avatar)
        self.profile_name=QLabel(self.user);self.profile_name.setStyleSheet("font-weight:800;");pc.addWidget(self.profile_name);pc.addStretch()
        sl.addWidget(self.profile_card)
        body.addWidget(side)

        content=QFrame();content.setObjectName("ContentPanel");cl=QVBoxLayout(content);cl.setContentsMargins(0,0,0,0);cl.setSpacing(0)
        head=QFrame();head.setFixedHeight(62);hl=QHBoxLayout(head);hl.setContentsMargins(18,9,14,9)
        self.page_title=QLabel("Bir sunucu seç");self.page_title.setObjectName("PageTitle");hl.addWidget(self.page_title)
        hl.addStretch()
        self.copy_invite=QPushButton("🔗 DAVET");self.copy_invite.clicked.connect(self.copy_invite_link);hl.addWidget(self.copy_invite)
        self.context_btn=QPushButton("•••");self.context_btn.clicked.connect(self.server_context_menu_button);hl.addWidget(self.context_btn)
        cl.addWidget(head)

        self.call_banner=QFrame();self.call_banner.setObjectName("CallBanner");self.call_banner.setVisible(False)
        cb=QHBoxLayout(self.call_banner);cb.setContentsMargins(14,8,10,8)
        self.call_banner_text=QLabel("SES: Bağlı değil");cb.addWidget(self.call_banner_text);cb.addStretch()
        show=QPushButton("GÖSTER");show.clicked.connect(self.show_call);cb.addWidget(show)
        leave=QPushButton("AYRIL");leave.clicked.connect(self.leave_voice);cb.addWidget(leave)
        cl.addWidget(self.call_banner)

        self.chat_scroll=QScrollArea();self.chat_scroll.setWidgetResizable(True);self.chat_scroll.setFrameShape(QFrame.NoFrame)
        self.chat_host=QWidget();self.chat_layout=QVBoxLayout(self.chat_host);self.chat_layout.setContentsMargins(18,16,18,16);self.chat_layout.setSpacing(8);self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_host);cl.addWidget(self.chat_scroll,1)

        self.typing=QLabel("");self.typing.setStyleSheet("color:#747F90;padding:0 18px;");cl.addWidget(self.typing)
        composer=QFrame();composer.setStyleSheet("background:#0A0E14;border-top:1px solid #1D2530;");co=QVBoxLayout(composer);co.setContentsMargins(14,9,14,11);co.setSpacing(5)
        self.reply_label=QLabel("");self.reply_label.setStyleSheet("color:#FF7786;font-size:11px;");co.addWidget(self.reply_label)
        row=QHBoxLayout()
        fileb=QPushButton("📎");fileb.clicked.connect(self.attach);row.addWidget(fileb)
        self.message=QLineEdit();self.message.setPlaceholderText("Mesajını yaz...");self.message.returnPressed.connect(self.send_message);self.message.textEdited.connect(self.typing_send);row.addWidget(self.message,1)
        send=QPushButton("➤  GÖNDER");send.setProperty("accent",True);send.clicked.connect(self.send_message);row.addWidget(send)
        voice=QPushButton("🎧  SESE KATIL");voice.clicked.connect(self.join_voice);row.addWidget(voice)
        screen=QPushButton("🖥  EKRAN");screen.clicked.connect(self.toggle_screen);row.addWidget(screen)
        co.addLayout(row);cl.addWidget(composer)
        body.addWidget(content,1)

        members=QFrame();members.setObjectName("MembersPanel");members.setFixedWidth(265);ml=QVBoxLayout(members);ml.setContentsMargins(12,14,12,12)
        mt=QLabel("ÜYELER");mt.setObjectName("SectionTitle");ml.addWidget(mt)
        self.member_list=QListWidget();ml.addWidget(self.member_list,1)
        body.addWidget(members)

        self.update_profile_ui()
        self.show_servers()
        self.net.send({"action":"servers"})
        self.net.send({"action":"friends"})
        self.fade(root)

    def update_profile_ui(self):
        if not hasattr(self,"profile_avatar"):return
        self.profile_avatar.setPixmap(circular_pixmap(self.avatar,38))
        self.profile_name.setText(self.user)
        self.profile_name.setStyleSheet(f"font-weight:800;color:{self.user_color};")

    def animate_page(self):
        self.fade(self.chat_host)

    # ---------- lists/navigation ----------
    def show_servers(self):
        self.mode="server";self.dm_peer=None
        self.nav_servers.setProperty("navActive",True);self.nav_friends.setProperty("navActive",False)
        for b in (self.nav_servers,self.nav_friends):b.style().unpolish(b);b.style().polish(b)
        self.side_title.setText("SUNUCULAR")
        self.side_list.clear()
        for s in self.servers:
            it=QListWidgetItem(f"◆  {s.get('name','Sunucu')}")
            it.setData(Qt.UserRole,("server",s))
            self.side_list.addItem(it)
        self.page_title.setText(self.server_name or "Sunucular")
        if self.server_code:
            self.net.send({"action":"enter","code":self.server_code})

    def show_friends(self):
        self.mode="friends";self.dm_peer=None
        self.nav_servers.setProperty("navActive",False);self.nav_friends.setProperty("navActive",True)
        for b in (self.nav_servers,self.nav_friends):b.style().unpolish(b);b.style().polish(b)
        self.side_title.setText("ARKADAŞLAR")
        self.side_list.clear()
        add=QListWidgetItem("＋  Arkadaş ekle");add.setData(Qt.UserRole,("friend_add",None));self.side_list.addItem(add)
        for r in self.friend_requests:
            it=QListWidgetItem(f"📨  {r.get('sender')} • İstek")
            it.setData(Qt.UserRole,("request",r));self.side_list.addItem(it)
        for f in self.friends:
            st="●" if f.get("online") else "○"
            it=QListWidgetItem(f"{st}  {f.get('username')}")
            it.setData(Qt.UserRole,("friend",f));self.side_list.addItem(it)
        self.page_title.setText("Arkadaşlar")
        self.clear_chat()

    def side_clicked(self,it):
        kind,data=it.data(Qt.UserRole) or (None,None)
        if kind=="server":
            self.server_code=data.get("code","");self.server_name=data.get("name","")
            self.mode="server";self.dm_peer=None
            self.net.send({"action":"enter","code":self.server_code})
        elif kind=="friend":
            self.open_dm(data.get("username"))
        elif kind=="friend_add":
            self.add_friend_dialog()
        elif kind=="request":
            self.friend_request_dialog(data)

    def side_menu(self,pos):
        it=self.side_list.itemAt(pos)
        if not it:return
        kind,data=it.data(Qt.UserRole) or (None,None)
        if kind!="server":return
        m=QMenu(self)
        enter=m.addAction("➡ Sunucuya git")
        invite=m.addAction("🔗 Davet bağlantısını kopyala")
        m.addSeparator()
        owner=data.get("owner")==self.user
        danger=m.addAction("🗑 Sunucuyu sil" if owner else "🚪 Sunucudan ayrıl")
        act=m.exec(self.side_list.mapToGlobal(pos))
        if act==enter:
            self.server_code=data.get("code","");self.server_name=data.get("name","");self.net.send({"action":"enter","code":self.server_code})
        elif act==invite:
            QApplication.clipboard().setText(f"sikkord://join/{data.get('code','')}")
            self.toast("Davet","Davet bağlantısı kopyalandı.","info")
        elif act==danger:
            text="Sunucu ve tüm mesajlar kalıcı olarak silinsin mi?" if owner else "Sunucudan ayrılmak istiyor musun?"
            if self.modal("Emin misin?",text,"warning",("EVET","VAZGEÇ"))=="EVET":
                self.net.send({"action":"delete_server" if owner else "leave_server","code":data.get("code")})

    def server_context_menu_button(self):
        if not self.server_code:return
        s=next((x for x in self.servers if x.get("code")==self.server_code),None)
        if not s:return
        m=QMenu(self)
        inv=m.addAction("🔗 Davet bağlantısını kopyala")
        m.addSeparator()
        owner=s.get("owner")==self.user
        danger=m.addAction("🗑 Sunucuyu sil" if owner else "🚪 Sunucudan ayrıl")
        act=m.exec(self.context_btn.mapToGlobal(self.context_btn.rect().bottomRight()))
        if act==inv:self.copy_invite_link()
        elif act==danger:
            if self.modal("Emin misin?","Bu işlem geri alınamaz." if owner else "Sunucudan ayrılacaksın.","warning",("DEVAM","VAZGEÇ"))=="DEVAM":
                self.net.send({"action":"delete_server" if owner else "leave_server","code":self.server_code})

    # ---------- chat ----------
    def clear_chat(self):
        while self.chat_layout.count()>1:
            item=self.chat_layout.takeAt(0)
            w=item.widget()
            if w:w.deleteLater()
        self.current_history=[]

    def load_history(self,msgs):
        self.clear_chat()
        self.current_history=list(msgs or [])
        for i,m in enumerate(self.current_history):
            b=MessageBubble(self,m);self.chat_layout.insertWidget(self.chat_layout.count()-1,b)
        QTimer.singleShot(30,lambda:self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))
        self.animate_page()

    def add_message(self,m):
        if any(x.get("id")==m.get("id") for x in self.current_history if m.get("id") is not None):return
        self.current_history.append(m)
        self.chat_layout.insertWidget(self.chat_layout.count()-1,MessageBubble(self,m))
        QTimer.singleShot(10,lambda:self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))

    def send_message(self):
        text=self.message.text().strip()
        if not text and not self.pending_attachment:return
        payload={"text":text,"reply_to":self.reply_to}
        if self.pending_attachment:payload.update(self.pending_attachment)
        if self.mode=="dm" and self.dm_peer:
            self.net.send({"action":"dm_send","to":self.dm_peer,**payload})
        elif self.mode=="server" and self.server_code:
            self.net.send({"action":"server_chat","message":text,**payload})
        self.message.clear();self.reply_to=None;self.reply_label.clear();self.pending_attachment=None

    def typing_send(self,_):
        if self.mode=="dm" and self.dm_peer:self.net.send({"action":"typing","scope":"dm","peer":self.dm_peer})
        elif self.mode=="server" and self.server_code:self.net.send({"action":"typing","scope":"server"})

    def attach(self):
        path,_=QFileDialog.getOpenFileName(self,"Dosya gönder")
        if not path:return
        try:
            raw=Path(path).read_bytes()
            if len(raw)>6_000_000:
                self.modal("Dosya çok büyük","Dosya en fazla 6 MB olabilir.","warning");return
            self.pending_attachment={
                "attachment_name":Path(path).name,
                "attachment_mime":mimetypes.guess_type(path)[0] or "application/octet-stream",
                "attachment_data":base64.b64encode(raw).decode("ascii")
            }
            self.reply_label.setText(f"📎 {Path(path).name} gönderilecek")
        except Exception as e:self.modal("Dosya",str(e),"error")

    # ---------- server/friends ----------
    def create_server(self):
        d=QDialog(self);d.setWindowTitle("Yeni sunucu");l=QVBoxLayout(d);e=QLineEdit();e.setPlaceholderText("Sunucu adı");l.addWidget(QLabel("Yeni bir alan oluştur"));l.addWidget(e)
        b=QPushButton("SUNUCUYU OLUŞTUR");b.setProperty("accent",True);l.addWidget(b);b.clicked.connect(d.accept)
        if d.exec() and e.text().strip():self.net.send({"action":"create","name":e.text().strip()})

    def add_friend_dialog(self):
        d=QDialog(self);d.setWindowTitle("Arkadaş ekle");l=QVBoxLayout(d);e=QLineEdit();e.setPlaceholderText("Kullanıcı adı");l.addWidget(QLabel("Kullanıcı adını yaz"));l.addWidget(e)
        b=QPushButton("İSTEK GÖNDER");b.setProperty("accent",True);l.addWidget(b);b.clicked.connect(d.accept)
        if d.exec() and e.text().strip():self.net.send({"action":"friend_request","username":e.text().strip()})

    def friend_request_dialog(self,r):
        x=self.modal("Arkadaşlık isteği",f"{r.get('sender')} seni arkadaş eklemek istiyor.", "info", ("KABUL ET","REDDET"))
        self.net.send({"action":"friend_respond","id":r.get("id"),"accept":x=="KABUL ET"})

    def open_dm(self,username):
        self.mode="dm";self.dm_peer=username
        self.page_title.setText("@"+username)
        self.clear_chat()
        self.net.send({"action":"dm_open","username":username})
        self.call_top.setText("📞  ÖZELDEN ARA")

    def copy_invite_link(self):
        if not self.server_code:return
        QApplication.clipboard().setText(f"sikkord://join/{self.server_code}")
        self.toast("Davet","sikkord://join bağlantısı kopyalandı.","info")

    # ---------- call/voice ----------
    def room_for_current(self):
        if self.mode=="dm" and self.dm_peer:return canonical_dm_room(self.user,self.dm_peer)
        if self.server_code:return "srv:"+self.server_code
        return None

    def join_voice(self):
        if not self.server_code:
            self.toast("Ses","Önce bir sunucu seç.","warning");return
        if self.audio.active:
            self.leave_voice();return
        room="srv:"+self.server_code
        self.net.send({"action":"voice","on":True})
        self.audio.start(room,"server")
        self.call_state="connected";self.call_peer=self.server_name;self.call_started_at=time.monotonic()
        self.show_call()

    def call_clicked(self):
        if self.mode=="dm" and self.dm_peer:
            if self.audio.active and self.audio.mode=="dm":
                self.end_call();return
            room=canonical_dm_room(self.user,self.dm_peer)
            self.call_state="ringing";self.call_peer=self.dm_peer;self.call_started_at=None
            self.net.send({"action":"dm_call_start","peer":self.dm_peer})
            self.audio.start(room,"dm",self.dm_peer)
            self.show_call()
        elif self.server_code:
            self.call_state="ringing";self.call_peer=self.server_name;self.call_started_at=None
            self.net.send({"action":"call_start"})
            self.audio.start("srv:"+self.server_code,"server")
            self.show_call()
        else:self.toast("Arama","Bir sunucu veya özel sohbet seç.","warning")

    def show_call(self):
        if self.call_window is None:
            self.call_window=CallWindow(self)
            self.call_window.hangup.connect(self.end_call)
            self.call_window.mute.connect(self.toggle_mute)
            self.call_window.share.connect(self.toggle_screen)
        self.refresh_call_people()
        self.call_window.show();self.call_window.raise_();self.call_window.activateWindow()
        self.update_call_banner()

    def refresh_call_people(self):
        if not self.call_window:return
        if self.audio.mode=="dm" and self.audio.peer:
            f=next((x for x in self.friends if x.get("username")==self.audio.peer),{})
            users=[
                {"username":self.user,"avatar":self.avatar,"muted":self.audio.muted},
                {"username":self.audio.peer,"avatar":f.get("avatar"),"muted":False},
            ]
        else:
            users=[m for m in self.members if m.get("voice")]
            if self.audio.active and not any(m.get("username")==self.user for m in users):
                users=[{"username":self.user,"avatar":self.avatar,"muted":self.audio.muted}]+users
        self.call_window.set_participants(users)

    def update_call_banner(self):
        if not hasattr(self,"call_banner"):return
        active=self.audio.active or self.call_state in ("ringing","incoming","connected")
        self.call_banner.setVisible(active)
        if not active:return
        elapsed=""
        if self.call_started_at:
            sec=int(time.monotonic()-self.call_started_at);elapsed=f" • {sec//60:02d}:{sec%60:02d}"
        if self.call_state=="ringing":
            self.call_banner.setObjectName("CallBannerRinging");text=f"📞 ARANIYOR • {self.call_peer or ''}"
        elif self.call_state=="incoming":
            self.call_banner.setObjectName("CallBannerRinging");text=f"🔔 GELEN ARAMA • {self.call_peer or ''}"
        else:
            self.call_banner.setObjectName("CallBannerConnected");text=f"🟢 ARAMA BAĞLI{elapsed}"
        self.call_banner.style().unpolish(self.call_banner);self.call_banner.style().polish(self.call_banner)
        self.call_banner_text.setText(text)
        if self.call_window:
            self.call_window.state.setText(text)
            self.call_window.mute_btn.setText("🔇 MİKROFON KAPALI" if self.audio.muted else "🎙 MİKROFON AÇIK")
            self.call_window.share_btn.setText("🟥 PAYLAŞIMI DURDUR" if self.screen_engine.active else "🖥 EKRAN PAYLAŞ")
            self.refresh_call_people()

    def toggle_mute(self):
        if not self.audio.active:return
        self.audio.muted=not self.audio.muted
        self.net.send({"action":"mic_mute","muted":self.audio.muted})
        self.update_call_banner()

    def leave_voice(self):
        if self.audio.mode=="dm":
            self.end_call();return
        self.net.send({"action":"voice","on":False})
        self.audio.stop();self.call_state="idle";self.call_peer=None;self.call_started_at=None
        if self.call_window:self.call_window.hide()
        self.update_call_banner()

    def end_call(self):
        # This is intentionally hard and deterministic: local media stops first.
        mode=self.audio.mode
        peer=self.audio.peer
        self.audio.stop()
        if self.screen_engine.active:
            self.screen_engine.stop()
            if self.server_code:self.net.send({"action":"screen","on":False})
        if mode=="dm" and peer:
            self.net.send({"action":"dm_call_end","peer":peer})
        elif self.server_code:
            self.net.send({"action":"group_call_end"})
        self.call_state="idle";self.call_peer=None;self.call_started_at=None
        if self.call_window:
            self.call_window.clear_frame();self.call_window.hide()
        self.update_call_banner()
        self.toast("Arama","Arama kapatıldı.","info")

    def incoming_group_call(self,from_user):
        self.call_state="incoming";self.call_peer=from_user;self.update_call_banner()
        d=PremiumDialog(self,"Gelen grup araması",f"{from_user} sunucuyu arıyor.","info",("KABUL ET","REDDET"))
        if d.exec() and d.result_name=="KABUL ET":
            self.net.send({"action":"call_answer"})
            self.audio.start("srv:"+self.server_code,"server")
            self.call_state="connected";self.call_started_at=time.monotonic();self.show_call()
        else:
            self.net.send({"action":"call_reject"});self.call_state="idle";self.call_peer=None;self.update_call_banner()

    def incoming_dm_call(self,from_user):
        self.call_state="incoming";self.call_peer=from_user;self.update_call_banner()
        d=PremiumDialog(self,"Özel arama",f"{from_user} seni arıyor.","info",("KABUL ET","REDDET"))
        if d.exec() and d.result_name=="KABUL ET":
            self.net.send({"action":"dm_call_answer","peer":from_user})
            self.audio.start(canonical_dm_room(self.user,from_user),"dm",from_user)
            self.call_state="connected";self.call_started_at=time.monotonic();self.show_call()
        else:
            self.net.send({"action":"dm_call_reject","peer":from_user});self.call_state="idle";self.call_peer=None;self.update_call_banner()

    # ---------- screen ----------
    def toggle_screen(self):
        if not self.server_code:
            self.toast("Ekran","Ekran paylaşımı için bir sunucuda olmalısın.","warning");return
        if self.screen_engine.active:
            self.screen_engine.stop();self.net.send({"action":"screen","on":False})
            if self.call_window:self.call_window.clear_frame()
            self.toast("Ekran paylaşımı","Paylaşım durduruldu.","info")
        else:
            self.net.send({"action":"screen","on":True})
            self.screen_engine.start("srv:"+self.server_code)
            self.show_call()
            self.toast("Ekran paylaşımı","Şu anda ekranını paylaşıyorsun.","info")
        self.update_call_banner()

    # ---------- profile ----------
    def choose_avatar(self):
        path,_=QFileDialog.getOpenFileName(self,"Profil fotoğrafı seç","","Resim (*.png *.jpg *.jpeg *.webp)")
        if not path:return
        try:
            im=Image.open(path).convert("RGB");im.thumbnail((512,512))
            b=io.BytesIO();im.save(b,"JPEG",quality=84,optimize=True)
            data=base64.b64encode(b.getvalue()).decode("ascii")
            self.net.send({"action":"profile","avatar":data})
        except Exception as e:self.modal("Profil",str(e),"error")

    # ---------- events ----------
    def handle_event(self,m):
        t=m.get("type")
        if t=="login":
            if m.get("ok"):
                self.user=m.get("username","");self.user_color=m.get("color","#F4F5F7");self.avatar=m.get("avatar")
                self.net.username=self.user;self.net.token=m.get("token","") or self.net.token
                if self.net.token:
                    try:SESSION_FILE.write_text(json.dumps({"token":self.net.token}),encoding="utf-8")
                    except:pass
                if not hasattr(self,"side_list") or self.centralWidget() is None:
                    self.build_main()
                else:
                    self.update_profile_ui()
                self.net.send({"action":"servers"});self.net.send({"action":"friends"})
            else:self.modal("Giriş yapılamadı",m.get("message","Kullanıcı adı veya şifre yanlış."),"error")
        elif t=="token_login":
            if not m.get("ok"):
                try:SESSION_FILE.unlink(missing_ok=True)
                except:pass
        elif t=="register":
            self.modal("Kayıt",m.get("message",""),"info" if m.get("ok") else "warning")
        elif t=="servers":
            self.servers=m.get("items",[])
            if hasattr(self,"side_list") and self.mode=="server":self.show_servers()
        elif t=="created":
            self.server_code=m.get("code","");self.server_name=m.get("name","");self.net.send({"action":"enter","code":self.server_code});self.net.send({"action":"servers"})
        elif t=="joined":
            if m.get("ok"):
                self.server_code=m.get("code","");self.server_name=m.get("name","");self.net.send({"action":"enter","code":self.server_code});self.net.send({"action":"servers"})
            else:self.modal("Davet","Davet kodu geçersiz.","warning")
        elif t=="entered":
            self.mode="server";self.dm_peer=None
            self.server_code=m.get("code","");self.server_name=m.get("name","");self.members=m.get("members",[])
            self.page_title.setText(self.server_name);self.call_top.setText("📞  GRUP ARAMASI")
            self.load_history(m.get("history",[]));self.render_members();self.net.send({"action":"server_read"})
        elif t=="members":
            self.members=m.get("items",[]);self.render_members()
            if any(x.get("screen") and x.get("username")!=self.user for x in self.members):
                self.net.set_room("screen","srv:"+self.server_code);self.net.ensure_media("screen")
        elif t in ("server_chat","chat"):
            if "text" not in m and "message" in m:m["text"]=m.get("message")
            if self.mode=="server" and self.server_code:self.add_message(m)
            if m.get("username")!=self.user:self.toast("Yeni mesaj",f"{m.get('username','?')}: {m.get('text','')[:60]}","info")
            self.net.send({"action":"server_read"})
        elif t=="server_message_deleted":
            self.net.send({"action":"enter","code":self.server_code})
        elif t=="friends":
            self.friends=m.get("items",[]);self.friend_requests=m.get("requests",[])
            if self.mode=="friends":self.show_friends()
        elif t=="friend_request_result":
            self.toast("Arkadaş",m.get("message",""),"info");self.net.send({"action":"friends"})
        elif t=="friend_request_notice":
            self.toast("Arkadaşlık isteği",f"{m.get('from')} seni arkadaş eklemek istiyor.","info");self.net.send({"action":"friends"})
        elif t=="dm_history":
            if self.dm_peer==m.get("peer"):self.load_history(m.get("messages",[]));self.net.send({"action":"dm_read","peer":self.dm_peer})
        elif t=="dm_message":
            if self.mode=="dm" and self.dm_peer in (m.get("sender"),m.get("receiver")):
                self.add_message(m)
                if m.get("sender")==self.dm_peer:self.net.send({"action":"dm_read","peer":self.dm_peer})
            if m.get("sender")!=self.user:self.toast("Özel mesaj",f"{m.get('sender')}: {m.get('text','')[:60]}","info")
        elif t=="dm_read":
            # Update data without rebuilding whole chat.
            ids=set(m.get("ids",[]))
            for x in self.current_history:
                if x.get("id") in ids:x["read_at"]=m.get("read_at")
        elif t=="typing":
            if m.get("username")!=self.user:
                self.typing.setText(f"✍ {m.get('username')} yazıyor...")
                QTimer.singleShot(1800,lambda:self.typing.setText(""))
        elif t=="incoming_call":
            self.incoming_group_call(m.get("from","?"))
        elif t=="incoming_dm_call":
            self.incoming_dm_call(m.get("from","?"))
        elif t=="group_call_state":
            st=m.get("state")
            if st=="started":
                self.call_state="connected";self.call_started_at=time.monotonic();self.show_call()
            elif st=="accepted":
                self.call_state="connected";self.call_started_at=self.call_started_at or time.monotonic();self.show_call()
            elif st=="ended":
                self.audio.stop()
                if self.screen_engine.active:self.screen_engine.stop()
                self.call_state="idle";self.call_peer=None;self.call_started_at=None
                if self.call_window:self.call_window.hide()
                self.update_call_banner()
                self.toast("Grup araması",f"Arama {m.get('by','bir kullanıcı')} tarafından kapatıldı.","info")
        elif t=="dm_call_state":
            st=m.get("state")
            if st=="accepted":
                self.call_state="connected";self.call_started_at=time.monotonic();self.show_call()
            elif st in ("rejected","ended"):
                self.audio.stop();self.call_state="idle";self.call_peer=None;self.call_started_at=None
                if self.call_window:self.call_window.hide()
                self.update_call_banner();self.toast("Arama","Arama reddedildi." if st=="rejected" else "Arama sona erdi.","info")
        elif t=="voice":
            self.render_members()
        elif t=="screen":
            user=m.get("username");on=bool(m.get("on"))
            for mm in self.members:
                if mm.get("username")==user:mm["screen"]=on
            if user!=self.user and on:
                self.net.set_room("screen","srv:"+self.server_code);self.net.ensure_media("screen");self.show_call()
            elif user!=self.user and not on and self.call_window:
                self.call_window.clear_frame()
            self.render_members()
        elif t=="qt_screen_frame":
            if self.call_window:
                self.call_window.show_frame(m.get("data"),m.get("username","?"))
        elif t=="profile":
            if m.get("ok"):
                self.avatar=m.get("avatar");self.update_profile_ui()
        elif t=="profile_update":
            if m.get("ok"):
                self.user=m.get("username",self.user);self.user_color=m.get("color",self.user_color);self.net.username=self.user;self.update_profile_ui();self.toast("Profil",m.get("message","Profil güncellendi."),"info")
            else:self.modal("Profil",m.get("message","Profil güncellenemedi."),"warning")
        elif t=="profile_changed":
            old=m.get("old_username");new=m.get("username")
            if old==self.user:self.user=new;self.user_color=m.get("color",self.user_color)
            self.net.send({"action":"friends"})
            if self.server_code:self.net.send({"action":"enter","code":self.server_code})
        elif t=="message_info":
            info=m.get("info")
            readers=info.get("read_by",[]) if info else []
            txt="Henüz kimse okumadı." if not readers else "\n".join(f"• {x.get('username')} • {time.strftime('%H:%M',time.localtime(x.get('read_at')))}" for x in readers)
            self.modal("Mesaj bilgisi","Okuyanlar:\n"+txt,"info")
        elif t=="attachment_data":
            data=m.get("data")
            if not data:self.modal("Dosya","Dosya alınamadı.","warning");return
            name=m.get("name") or "dosya"
            path,_=QFileDialog.getSaveFileName(self,"Dosyayı kaydet",name)
            if path:
                try:Path(path).write_bytes(base64.b64decode(data));self.toast("Dosya","Dosya kaydedildi.","info")
                except Exception as e:self.modal("Dosya",str(e),"error")
        elif t=="server_manage":
            self.toast("Sunucu",m.get("message",""),"info" if m.get("ok") else "warning")
            if m.get("ok"):
                self.server_code="";self.server_name="";self.members=[];self.net.send({"action":"servers"});self.show_servers()
        elif t=="error":
            self.modal("SIKKORD",m.get("message","Bir hata oluştu."),"warning")

    def render_members(self):
        if not hasattr(self,"member_list"):return
        self.member_list.clear()
        for m in self.members:
            state="🎙 Seste" if m.get("voice") else ("● Çevrimiçi" if m.get("online") else f"○ {fmt_seen(m.get('last_seen'))}")
            if m.get("screen"):state+=" • 🖥 Ekran"
            it=QListWidgetItem(f"{m.get('username')}\n{state}")
            if not m.get("online"):it.setForeground(QColor("#687385"))
            self.member_list.addItem(it)
        self.refresh_call_people()

    def closeEvent(self,e):
        # Keep it deterministic: hide to tray if possible.
        if not hasattr(self,"tray"):
            self.tray=QSystemTrayIcon(QIcon(resource(ICON)) if ICON.exists() else self.style().standardIcon(QStyle.SP_ComputerIcon),self)
            menu=QMenu()
            show=menu.addAction("SIKKORD'u Aç");show.triggered.connect(self.showNormal)
            quit_action=menu.addAction("Tamamen Çık")
            quit_action.triggered.connect(self.real_quit)
            self.tray.setContextMenu(menu);self.tray.activated.connect(lambda reason:self.showNormal() if reason==QSystemTrayIcon.DoubleClick else None);self.tray.show()
        e.ignore();self.hide();self.toast("SIKKORD","Uygulama arka planda çalışmaya devam ediyor.","info")

    def real_quit(self):
        self.audio.stop();self.screen_engine.stop();self.net.close()
        QApplication.quit()

def main():
    app=QApplication(sys.argv)
    app.setApplicationName("SIKKORD")
    app.setQuitOnLastWindowClosed(False)
    win=MainWindow();win.show()
    sys.exit(app.exec())

if __name__=="__main__":
    main()
