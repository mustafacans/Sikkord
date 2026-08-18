
import asyncio
import base64
import io
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
import mss
import sounddevice as sd
import websockets
from PIL import Image, ImageDraw, ImageGrab, ImageOps, ImageTk

try:
    import pystray
except Exception:
    pystray = None

try:
    import winsound
except Exception:
    winsound = None

SERVER_URL = "wss://sikkord-jrbh.onrender.com"

APP_DIR = Path(os.getenv("APPDATA", Path.home())) / "Sikkord"
APP_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = APP_DIR / "session.json"
SETTINGS_FILE = APP_DIR / "settings.json"
NETWORK_RATE = 16000
NETWORK_SAMPLES = 320  # 20 ms, PCM16 mono
AUDIO_PROTOCOL = 2
SCREEN_PROTOCOL = 3

RATE = 16000
BG = "#07090c"
PANEL = "#0e1116"
PANEL2 = "#151922"
PANEL3 = "#202631"
RED = "#e2293f"
RED2 = "#ff5264"
TEXT = "#f4f5f7"
MUTED = "#8d95a3"
GREEN = "#35d07f"
GRAY = "#646b76"


def fmt_seen(ts):
    if not ts:
        return "Bilinmiyor"
    d = max(0, int(time.time()) - int(ts))
    if d < 60: return "Az önce"
    if d < 3600: return f"{d//60} dk önce"
    if d < 86400: return f"{d//3600} sa önce"
    return f"{d//86400} gün önce"


def load_json(path, default):
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else dict(default)
    except Exception:
        return dict(default)


def save_json(path, data):
    try:path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception:pass


def resample_linear(x, target_len):
    x=np.asarray(x,dtype=np.float32)
    if len(x)==target_len:return x
    if len(x)<2:return np.zeros(target_len,dtype=np.float32)
    src=np.linspace(0.0,1.0,len(x),endpoint=False)
    dst=np.linspace(0.0,1.0,target_len,endpoint=False)
    return np.interp(dst,src,x).astype(np.float32)


def pcm16_encode(x):
    x=np.clip(np.asarray(x,dtype=np.float32),-0.98,0.98)
    return (x*32767.0).astype("<i2",copy=False).tobytes()


def pcm16_decode(data):
    if len(data)!=(NETWORK_SAMPLES*2):
        raise ValueError("Geçersiz PCM16 paket uzunluğu")
    return np.frombuffer(data,dtype="<i2").astype(np.float32)/32768.0


class NoiseSuppressor:
    """Lightweight speech filter: correct DC blocker + adaptive soft expander.

    It never amplifies the microphone. This avoids the harsh / screaming sound
    the previous differentiator + AGC chain could produce.
    """
    def __init__(self):
        self.noise_rms=0.006
        self.prev_x=0.0
        self.prev_y=0.0
        self.gain=1.0

    def process(self,x,enabled=True,strength=58):
        x=np.asarray(x,dtype=np.float32)
        if x.size==0:return x
        # Correct first-order DC blocker / gentle high-pass (~80 Hz-ish).
        y=np.empty_like(x)
        px=float(self.prev_x); py=float(self.prev_y); alpha=0.985
        for i,v in enumerate(x):
            nv=alpha*(py+float(v)-px)
            y[i]=nv; px=float(v); py=nv
        self.prev_x=px; self.prev_y=py
        if not enabled:
            return np.clip(y,-0.95,0.95)
        rms=float(np.sqrt(np.mean(y*y)+1e-10))
        # Learn stationary room noise only while the frame is quiet.
        if rms < max(0.025,self.noise_rms*2.2):
            self.noise_rms=0.995*self.noise_rms+0.005*rms
        s=max(0.0,min(1.0,float(strength)/100.0))
        threshold=max(0.0025,self.noise_rms*(1.25+0.85*s))
        ratio=rms/(threshold+1e-9)
        if ratio<=0.65:
            target=0.10 + 0.15*(1.0-s)
        elif ratio<1.25:
            target=0.25 + 0.75*((ratio-0.65)/0.60)
        else:
            target=1.0
        self.gain=0.82*self.gain+0.18*target
        y*=self.gain
        # Limiter only; never boost speech.
        peak=float(np.max(np.abs(y))+1e-9)
        if peak>0.94:y*=0.94/peak
        return y.astype(np.float32,copy=False)


class Sikkord:
    def __init__(self, root):
        self.r=root
        self.r.title("SIKKORD PRO")
        self.r.geometry("1420x900")
        self.r.minsize(1120,720)
        self.r.configure(bg=BG)
        self.r.protocol("WM_DELETE_WINDOW",self.close)

        self.loop=None; self.ws=None; self.connected=False; self.reconnect=True
        self.voice_ws=None; self.screen_ws=None
        self.user=""; self.token=""; self.avatar=None; self.name_color="#f4f5f7"
        self.servers=[]; self.sc=""; self.sn=""; self.members=[]
        self.friends=[]; self.friend_requests=[]; self.current_dm=None
        self.current_mode="server"  # server | friends | dm
        self.current_history=[]
        self.reply_to=None
        self.voice=False; self.muted=False; self.screen=False
        self.audio_by_user={}; self.audio_lock=threading.Lock()
        self.voice_tx=queue.Queue(maxsize=3); self.screen_tx=queue.Queue(maxsize=1)
        self.video=queue.Queue(maxsize=1)
        self.ins=None; self.outs=None
        self.voice_ready=threading.Event(); self.screen_ready=threading.Event()
        self.chat_images={}; self.message_map={}
        self.vwin=None; self.vlabel=None; self.vcanvas=None; self.vphoto=None; self.vstatus=None
        self.last_video_frame=None; self.last_video_sender=None; self.remote_screen_until=0.0
        self.share_banner=None
        self.speaking_levels={}; self.overlay_win=None; self.overlay_name=None; self.overlay_bar=None; self.overlay_tick_started=False
        self.call_stage_rings={}; self.call_stage_photos={}; self.call_stage_screen_canvas=None; self.call_stage_screen_photo=None
        self.pending=queue.Queue()
        self.pending_attachment=None
        self.active_dm_call=None
        self.ring_stop=threading.Event()
        self.tray_icon=None
        self.force_exit=False
        self.server_read_cache={}
        self.audio_fx=NoiseSuppressor()
        self.app_settings=load_json(SETTINGS_FILE,{"noise_suppression":True,"noise_strength":58,"echo_protection":True,"output_volume":72,"screen_profile":"Eco 540p","input_device":None,"output_device":None,"overlay":True,"message_sound":True,"call_sound":True,"reduced_motion":False,"compact_chat":False,"startup":False})
        self.input_device=self.app_settings.get("input_device")
        self.output_device=self.app_settings.get("output_device")
        self.call_stage=None; self.call_stage_body=None; self.call_stage_timer=None
        self.call_stage_sig=None
        self.remote_screen_users=set()
        self.screen_render_pending=False
        self.screen_starting=False
        self.local_preview_last=0.0
        self.avatar_cache={}
        self.server_history_cache={}
        self.dm_history_cache={}
        self._render_token=0
        self._member_sig=None
        self._server_sig=None
        self._friend_sig=None
        self.voice_media_started=False
        self.screen_media_started=False
        self.mic_raw=queue.Queue(maxsize=2)
        self.playback_raw=queue.Queue(maxsize=3)
        self.audio_worker_stop=threading.Event()
        self.capture_thread=None
        self.playback_thread=None
        self.voice_starting=False
        self.voice_start_lock=threading.Lock()
        self.remote_audio_until=0.0
        self.remote_audio_level=0.0

        self.style=ttk.Style(); self.style.theme_use("clam")
        self.style.configure("TButton",background=PANEL2,foreground=TEXT,borderwidth=0,padding=(12,9),font=("Segoe UI Semibold",10))
        self.style.map("TButton",background=[("active",PANEL3)])
        self.style.configure("Red.TButton",background=RED,foreground="white")
        self.style.map("Red.TButton",background=[("active",RED2)])
        self.style.configure("TEntry",fieldbackground="#0f1115",foreground=TEXT,borderwidth=0,padding=8)
        self.style.configure("TCombobox",fieldbackground="#0f1115",foreground=TEXT,padding=8)

        self.login_ui()
        threading.Thread(target=self.net,daemon=True).start()

    # ---------- local session ----------
    def load_saved_token(self):
        try:
            d=json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            return d.get("token","")
        except Exception:return ""

    def save_token(self, token):
        try:
            SESSION_FILE.write_text(json.dumps({"token":token}),encoding="utf-8")
        except Exception:pass

    def clear_token(self):
        try: SESSION_FILE.unlink(missing_ok=True)
        except Exception: pass

    # ---------- networking ----------
    def net(self):
        self.loop=asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.net_run())

    async def net_run(self):
        while self.reconnect:
            try:
                async with websockets.connect(SERVER_URL,max_size=12_000_000,ping_interval=15,ping_timeout=15,compression=None) as w:
                    self.ws=w; self.connected=True
                    self.ui(lambda:self.set_status("BAĞLI",GREEN))
                    token=self.load_saved_token()
                    if token and not self.user:
                        await w.send(json.dumps({"action":"token_login","token":token}))
                    self.flush_pending()
                    async for raw in w:
                        if isinstance(raw,bytes): continue
                        try:self.handle(json.loads(raw))
                        except Exception as e: print("handle",e)
            except Exception:
                self.connected=False; self.ui(lambda:self.set_status("SUNUCU BEKLENİYOR",RED))
            self.ws=None; self.connected=False
            await asyncio.sleep(3)

    def send(self,payload):
        if self.loop and self.ws and self.connected:
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(payload,ensure_ascii=False)),self.loop)
        else:
            try:self.pending.put_nowait(payload)
            except:pass

    def flush_pending(self):
        while self.connected and not self.pending.empty():
            try:self.send(self.pending.get_nowait())
            except:break

    async def _media_sender(self,w,kind):
        q=self.voice_tx if kind=="voice" else self.screen_tx
        while self.reconnect:
            try:data=q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(.003 if kind=="voice" else .012);continue
            try:await w.send(data)
            except Exception:return

    async def connect_media(self,kind):
        ready=self.voice_ready if kind=="voice" else self.screen_ready
        while self.reconnect:
            if not self.token:
                ready.clear();await asyncio.sleep(1);continue
            sender_task=None
            try:
                w=await websockets.connect(SERVER_URL,max_size=12_000_000,ping_interval=10,ping_timeout=10,compression=None,open_timeout=10,close_timeout=1)
                await w.send(json.dumps({"action":"media_auth","token":self.token,"kind":kind,"proto":AUDIO_PROTOCOL if kind=="voice" else SCREEN_PROTOCOL,"codec":"pcm16" if kind=="voice" else "jpeg"}))
                auth=await asyncio.wait_for(w.recv(),timeout=8)
                auth_obj={} if isinstance(auth,bytes) else json.loads(auth)
                if isinstance(auth,bytes) or not auth_obj.get("ok") or (kind=="voice" and (auth_obj.get("proto")!=AUDIO_PROTOCOL or auth_obj.get("codec")!="pcm16")) or (kind=="screen" and auth_obj.get("proto")!=SCREEN_PROTOCOL):
                    await w.close();await asyncio.sleep(1);continue
                if kind=="voice":self.voice_ws=w
                else:self.screen_ws=w
                ready.set();sender_task=asyncio.create_task(self._media_sender(w,kind))
                async for raw in w:
                    if kind=="voice" and isinstance(raw,bytes) and raw[:1]==b"P" and len(raw)>=2:
                        ln=raw[1]
                        if len(raw)>=2+ln:
                            speaker=raw[2:2+ln].decode("utf-8","ignore") or "?"
                            self.put_audio(speaker,raw[2+ln:])
                    elif kind=="screen" and isinstance(raw,bytes) and raw[:1]==b"S" and len(raw)>=2:
                        ln=raw[1]
                        if len(raw)>=2+ln:
                            speaker=raw[2:2+ln].decode("utf-8","ignore") or "?"
                            self.put_video(speaker,raw[2+ln:])
            except Exception:pass
            finally:
                ready.clear()
                if sender_task:sender_task.cancel()
                if kind=="voice":self.voice_ws=None
                else:self.screen_ws=None
            if self.reconnect:await asyncio.sleep(1.0)

    def _queue_latest(self,q,data):
        try:
            while q.full():q.get_nowait()
            q.put_nowait(data)
        except Exception:pass

    def ensure_media(self,kind):
        if not self.loop:
            return
        if kind=="voice":
            if self.voice_media_started:return
            self.voice_media_started=True
        else:
            if self.screen_media_started:return
            self.screen_media_started=True
        asyncio.run_coroutine_threadsafe(self.connect_media(kind),self.loop)

    def raw_media(self,kind,data):
        self._queue_latest(self.voice_tx if kind=="voice" else self.screen_tx,data)

    def put_audio(self,speaker,data):
        try:
            a=np.frombuffer(data,dtype="<i2").astype(np.float32)
            level=min(1.0,float(np.sqrt(np.mean(a*a)+1.0))/9000.0)
            self.speaking_levels[speaker]=(level,time.monotonic())
        except Exception:pass
        with self.audio_lock:
            q=self.audio_by_user.get(speaker)
            if q is None:
                q=queue.Queue(maxsize=3);self.audio_by_user[speaker]=q
            self._queue_latest(q,data)

    def put_video(self,speaker,data):
        self.last_video_sender=speaker
        self.last_video_frame=data
        self.remote_screen_until=time.monotonic()+2.0
        self.schedule_screen_render()

    def ui(self,fn):
        try:self.r.after(0,fn)
        except:pass

    # ---------- avatar ----------
    def avatar_img(self,data,size=40):
        if not data:return None
        key=(hash(data),int(size))
        cached=self.avatar_cache.get(key)
        if cached is not None:
            return cached
        try:
            im=Image.open(io.BytesIO(base64.b64decode(data))).convert("RGBA")
            im=ImageOps.fit(im,(size,size),Image.Resampling.LANCZOS)
            mask=Image.new("L",(size,size),0)
            ImageDraw.Draw(mask).ellipse((0,0,size-1,size-1),fill=255)
            im.putalpha(mask)
            photo=ImageTk.PhotoImage(im)
            if len(self.avatar_cache)>180:
                self.avatar_cache.clear()
            self.avatar_cache[key]=photo
            return photo
        except:return None

    def avatar_widget(self,parent,username,data,size=38,bg=PANEL2):
        img=self.avatar_img(data,size)
        if img:
            l=tk.Label(parent,image=img,bg=parent.cget("bg")); l.image=img; return l
        c=tk.Canvas(parent,width=size,height=size,bg=parent.cget("bg"),highlightthickness=0)
        c.create_oval(1,1,size-1,size-1,fill="#3a2024",outline="")
        c.create_text(size/2,size/2,text=(username[:1] or "?").upper(),fill="white",font=("Segoe UI",max(9,size//3),"bold"))
        return c

    # ---------- sound / tray ----------
    def play_notification(self):
        if not self.app_settings.get("message_sound",True):return
        try:
            if self.tray_icon:self.tray_icon.notify("Yeni mesajın var","SIKKORD")
        except:pass
        def worker():
            if winsound:
                try:
                    winsound.Beep(740,90); winsound.Beep(988,110)
                    return
                except Exception:
                    pass
            try:self.r.bell()
            except:pass
        threading.Thread(target=worker,daemon=True).start()

    def start_ringtone(self):
        if not self.app_settings.get("call_sound",True):return
        self.ring_stop.clear()
        def worker():
            while not self.ring_stop.is_set():
                if winsound:
                    try:
                        for freq,dur in ((880,260),(1175,260),(988,260),(1318,420)):
                            if self.ring_stop.is_set(): return
                            winsound.Beep(freq,dur)
                        self.ring_stop.wait(.7)
                        continue
                    except Exception:
                        pass
                try:self.ui(self.r.bell)
                except:pass
                self.ring_stop.wait(1.0)
        threading.Thread(target=worker,daemon=True).start()

    def stop_ringtone(self):
        self.ring_stop.set()

    def tray_image(self):
        im=Image.new("RGBA",(64,64),(12,14,18,255))
        d=ImageDraw.Draw(im)
        d.rounded_rectangle((5,5,59,59),radius=16,fill=(226,41,63,255))
        d.text((26,24),"S",fill="white")
        return im

    def show_from_tray(self,icon=None,item=None):
        self.ui(lambda:[self.r.deiconify(),self.r.lift(),self.r.focus_force()])

    def exit_app(self,icon=None,item=None):
        self.force_exit=True
        self.reconnect=False
        self.stop_ringtone()
        try:self.stop_voice(notify=False)
        except:pass
        self.screen=False
        try:
            if self.tray_icon:self.tray_icon.stop()
        except:pass
        self.ui(self.r.destroy)

    def ensure_tray(self):
        if pystray is None or self.tray_icon is not None:
            return
        try:
            menu=pystray.Menu(
                pystray.MenuItem("SIKKORD'u Aç",self.show_from_tray,default=True),
                pystray.MenuItem("Tamamen Çık",self.exit_app)
            )
            self.tray_icon=pystray.Icon("Sikkord",self.tray_image(),"SIKKORD",menu)
            threading.Thread(target=self.tray_icon.run,daemon=True).start()
        except Exception:
            self.tray_icon=None

    # ---------- basic UI ----------
    def clear(self):
        for w in self.r.winfo_children():w.destroy()

    def set_status(self,text,color):
        if hasattr(self,"status_label"):self.status_label.config(text="● "+text,fg=color)

    def login_ui(self):
        self.clear()
        o=tk.Frame(self.r,bg=BG);o.pack(fill="both",expand=True)
        tk.Frame(o,bg=RED,height=5).pack(fill="x")
        card=tk.Frame(o,bg=PANEL,padx=45,pady=38);card.place(relx=.5,rely=.48,anchor="center")
        tk.Label(card,text="SIKKORD",bg=PANEL,fg=RED2,font=("Segoe UI",42,"bold")).pack()
        tk.Label(card,text="Discord tarzı arkadaş, sunucu, DM, ses ve dosya paylaşımı",bg=PANEL,fg=MUTED,font=("Segoe UI",10)).pack(pady=(2,25))
        self.eu=ttk.Entry(card,width=38);self.eu.pack(pady=7);self.eu.insert(0,"Kullanıcı adı")
        self.ep=ttk.Entry(card,width=38,show="*");self.ep.pack(pady=7)
        ttk.Button(card,text="GİRİŞ YAP",style="Red.TButton",command=self.login).pack(fill="x",pady=(12,5))
        ttk.Button(card,text="KAYIT OL",command=self.register).pack(fill="x",pady=5)
        self.status_label=tk.Label(card,text="● SUNUCU BEKLENİYOR",bg=PANEL,fg=RED,font=("Segoe UI",9,"bold"));self.status_label.pack(pady=(18,0))

    def login(self):self.send({"action":"login","username":self.eu.get().strip(),"password":self.ep.get()})
    def register(self):self.send({"action":"register","username":self.eu.get().strip(),"password":self.ep.get()})

    def main_ui(self):
        self.clear()
        top=tk.Frame(self.r,bg="#0d0f12",height=64);top.pack(fill="x");top.pack_propagate(False)
        brand=tk.Frame(top,bg="#0d0f12");brand.pack(side="left",padx=(16,22),fill="y")
        tk.Label(brand,text="SIKKORD",bg="#0d0f12",fg=RED2,font=("Segoe UI Semibold",21)).pack(anchor="w",pady=(8,0))
        tk.Label(brand,text="VOICE • CHAT • SHARE",bg="#0d0f12",fg="#596273",font=("Segoe UI",7)).pack(anchor="w")
        self.status_label=tk.Label(top,text="● BAĞLI",bg="#0d0f12",fg=GREEN,font=("Segoe UI Semibold",9));self.status_label.pack(side="left")
        ttk.Button(top,text="⚙ Ayarlar",command=self.settings).pack(side="right",padx=8,pady=12)
        ttk.Button(top,text="＋ Sunucu",command=self.create_server).pack(side="right",padx=2)
        ttk.Button(top,text="🔗 Katıl",command=self.join_server).pack(side="right",padx=2)
        self.invite_label=tk.Label(top,text="Davet: —",bg="#0d0f12",fg="#d9dce3",font=("Segoe UI",10,"bold"));self.invite_label.pack(side="right",padx=18)

        body=tk.Frame(self.r,bg=BG);body.pack(fill="both",expand=True)

        # far left navigation
        nav=tk.Frame(body,bg="#090c10",width=118);nav.pack(side="left",fill="y");nav.pack_propagate(False)
        tk.Label(nav,text="GEZİN",bg="#090c10",fg="#596273",font=("Segoe UI Semibold",8)).pack(anchor="w",padx=12,pady=(14,5))
        ttk.Button(nav,text="SUNUCULAR",command=self.open_servers_home).pack(fill="x",padx=8,pady=4)
        ttk.Button(nav,text="ARKADAŞLAR",command=self.show_friends).pack(fill="x",padx=8,pady=4)
        ttk.Button(nav,text="YENİ SUNUCU",style="Red.TButton",command=self.create_server).pack(fill="x",padx=8,pady=(14,4))

        # left pane
        self.left=tk.Frame(body,bg="#111419",width=255);self.left.pack(side="left",fill="y");self.left.pack_propagate(False)
        self.left_title=tk.Label(self.left,text="SUNUCULAR",bg="#111419",fg=MUTED,font=("Segoe UI",9,"bold"),anchor="w")
        self.left_title.pack(fill="x",padx=14,pady=(14,8))
        self.left_list=tk.Frame(self.left,bg="#111419");self.left_list.pack(fill="both",expand=True,padx=8)
        self.userbar=tk.Frame(self.left,bg="#0e1115",height=62);self.userbar.pack(fill="x");self.userbar.pack_propagate(False)
        av=self.avatar_widget(self.userbar,self.user,self.avatar,38,bg=PANEL2);av.pack(side="left",padx=9,pady=11)
        self.userbar_name=tk.Label(self.userbar,text=self.user,bg="#0e1115",fg=self.name_color,font=("Segoe UI Semibold",10));self.userbar_name.pack(side="left")
        tk.Label(self.userbar,text="  •  AKTİF",bg="#0e1115",fg=GREEN,font=("Segoe UI",8)).pack(side="left")

        center=tk.Frame(body,bg=BG);center.pack(side="left",fill="both",expand=True)
        head=tk.Frame(center,bg=PANEL,height=60);head.pack(fill="x");head.pack_propagate(False)
        self.title_label=tk.Label(head,text="Bir sunucu veya arkadaş seç",bg=PANEL,fg=TEXT,font=("Segoe UI",16,"bold"));self.title_label.pack(side="left",padx=18)
        self.call_btn=ttk.Button(head,text="ARAMA",style="Red.TButton",command=self.call);self.call_btn.pack(side="right",padx=10,pady=10)
        self.server_menu_btn=ttk.Button(head,text="SUNUCU",command=self.server_menu);self.server_menu_btn.pack(side="right",padx=4,pady=10)
        self.transition_bar=tk.Frame(head,bg=RED2,height=2);self.transition_bar.place(x=-140,y=58,width=140,height=2)

        self.voice_strip=tk.Frame(center,bg="#101821",height=42);self.voice_strip.pack(fill="x");self.voice_strip.pack_propagate(False)
        self.voice_strip_label=tk.Label(
            self.voice_strip,text="SES: Bağlı değil",bg="#101821",fg="#8b96a7",
            font=("Segoe UI Semibold",9),anchor="w"
        )
        self.voice_strip_label.pack(side="left",padx=16,fill="x",expand=True)
        self.show_call_btn=ttk.Button(self.voice_strip,text="GÖSTER",command=self.show_call_screen,state="disabled")
        self.show_call_btn.pack(side="right",padx=(4,6),pady=5)
        ttk.Button(self.voice_strip,text="BAĞLANTIYI KES",command=self.end_active_call).pack(side="right",padx=(6,2),pady=5)

        chat_wrap=tk.Frame(center,bg=BG)
        chat_wrap.pack(fill="both",expand=True)
        self.chat=tk.Text(
            chat_wrap,bg=BG,fg=TEXT,insertbackground="white",border=0,
            font=("Segoe UI",11),wrap="word",state="disabled",
            padx=22,pady=18,cursor="arrow",spacing1=2,spacing3=4
        )
        self.chat.pack(side="left",fill="both",expand=True)
        chat_scroll=ttk.Scrollbar(chat_wrap,orient="vertical",command=self.chat.yview)
        chat_scroll.pack(side="right",fill="y")
        self.chat.configure(yscrollcommand=chat_scroll.set)
        self.chat.tag_configure("user",foreground="#ff6b76",font=("Segoe UI",11,"bold"))
        self.chat.tag_configure("meta",foreground=MUTED,font=("Segoe UI",8))
        self.chat.tag_configure("deleted",foreground=GRAY,font=("Segoe UI",9,"italic"))
        self.chat.tag_configure("system",foreground=MUTED,font=("Segoe UI",9,"italic"))
        self.chat.bind("<Button-3>",self.message_menu)

        controls=tk.Frame(center,bg=PANEL,height=112);controls.pack(fill="x");controls.pack_propagate(False)
        self.reply_label=tk.Label(controls,text="",bg=PANEL,fg="#ff9aa2",font=("Segoe UI",8))
        self.reply_label.pack(anchor="w",padx=14)
        entry_border=tk.Frame(controls,bg="#7a2530",padx=2,pady=2);entry_border.pack(side="left",fill="x",expand=True,padx=(12,6),pady=12)
        self.msg=tk.Entry(entry_border,bg="#11151c",fg=TEXT,insertbackground="white",relief="flat",font=("Segoe UI",11))
        self.msg.pack(fill="x",expand=True,ipady=10,padx=1,pady=1);self.msg.insert(0,"Mesaj yaz...")
        self.msg.bind("<FocusIn>",lambda e:self.msg.delete(0,"end") if self.msg.get()=="Mesaj yaz..." else None)
        self.msg.bind("<Return>",lambda e:(self.send_message(),"break")[1]);self.msg.bind("<Control-v>",self.ctrl_v_image)
        ttk.Button(controls,text="DOSYA",command=self.attach_file).pack(side="left",padx=4)
        ttk.Button(controls,text="GÖNDER",style="Red.TButton",command=self.send_message).pack(side="left",padx=4)
        self.vb=ttk.Button(controls,text="SESE KATIL",command=self.toggle_voice);self.vb.pack(side="left",padx=4)
        self.mb=ttk.Button(controls,text="MİKROFON",command=self.toggle_mute);self.mb.pack(side="left",padx=4)
        self.sb=ttk.Button(controls,text="EKRAN",command=self.toggle_screen);self.sb.pack(side="left",padx=(4,10))

        self.right=tk.Frame(body,bg="#111419",width=270);self.right.pack(side="right",fill="y");self.right.pack_propagate(False)
        self.right_title=tk.Label(self.right,text="SUNUCUDAKİLER",bg="#111419",fg=MUTED,font=("Segoe UI",9,"bold"),anchor="w")
        self.right_title.pack(fill="x",padx=14,pady=(14,8))
        self.right_list=tk.Frame(self.right,bg="#111419");self.right_list.pack(fill="both",expand=True,padx=8)

        self.show_servers()
        self.send({"action":"servers"}); self.send({"action":"friends"})
        if not self.overlay_tick_started:
            self.overlay_tick_started=True
            self.r.after(180,self.overlay_tick)

    # ---------- premium micro-interactions / overlays ----------
    def transition_pulse(self):
        if self.app_settings.get("reduced_motion",False) or not hasattr(self,"transition_bar"):return
        try:w=max(300,self.r.winfo_width())
        except:w=900
        steps=10
        def tick(i=0):
            if not hasattr(self,"transition_bar"):return
            x=int(-140+(w+140)*(i/steps))
            self.transition_bar.place_configure(x=x)
            if i<steps:self.r.after(12,lambda:tick(i+1))
            else:self.transition_bar.place_configure(x=-140)
        tick()

    def show_share_banner(self):
        if self.share_banner and self.share_banner.winfo_exists():return
        self.share_banner=tk.Frame(self.r,bg="#12231d",highlightbackground="#2ac47a",highlightthickness=1,padx=12,pady=8)
        tk.Label(self.share_banner,text="●  Şu anda ekranınızı paylaşıyorsunuz",bg="#12231d",fg="#aef2ce",font=("Segoe UI Semibold",9)).pack(side="left",padx=(0,12))
        ttk.Button(self.share_banner,text="PAYLAŞIMI DURDUR",style="Red.TButton",command=self.stop_screen).pack(side="left")
        self.share_banner.place(relx=1.0,rely=1.0,anchor="se",x=-18,y=-18)

    def hide_share_banner(self):
        try:
            if self.share_banner:self.share_banner.destroy()
        except:pass
        self.share_banner=None

    def ensure_speaker_overlay(self):
        if not self.app_settings.get("overlay",True) or not self.voice:
            self.hide_speaker_overlay();return
        if self.overlay_win and self.overlay_win.winfo_exists():return
        w=tk.Toplevel(self.r);self.overlay_win=w;w.overrideredirect(True);w.attributes("-topmost",True);w.attributes("-alpha",0.92);w.configure(bg="#090d12")
        sw=w.winfo_screenwidth();w.geometry(f"250x58+{max(20,sw-280)}+28")
        box=tk.Frame(w,bg="#101720",highlightbackground="#253142",highlightthickness=1);box.pack(fill="both",expand=True)
        self.overlay_name=tk.Label(box,text="SIKKORD • Ses bağlı",bg="#101720",fg=TEXT,font=("Segoe UI Semibold",10),anchor="w");self.overlay_name.pack(fill="x",padx=12,pady=(8,2))
        self.overlay_bar=tk.Frame(box,bg="#2ac47a",height=3);self.overlay_bar.place(x=12,y=42,width=30,height=3)

    def hide_speaker_overlay(self):
        try:
            if self.overlay_win:self.overlay_win.destroy()
        except:pass
        self.overlay_win=None;self.overlay_name=None;self.overlay_bar=None

    def overlay_tick(self):
        if self.voice and self.app_settings.get("overlay",True):
            self.ensure_speaker_overlay()
            nowm=time.monotonic();active=[]
            for name,(level,ts) in list(self.speaking_levels.items()):
                if nowm-ts<0.35 and level>0.035:active.append((level,name))
            if self.voice and not self.muted:
                local=self.speaking_levels.get(self.user)
                if local and nowm-local[1]<0.35 and local[0]>0.035:active.append((local[0],self.user))
            if active and self.overlay_name:
                level,name=max(active)
                self.overlay_name.config(text=f"Konuşuyor • {name}",fg="#dfffee")
                self.overlay_bar.place_configure(width=max(20,int(210*min(1.0,level))))
            elif self.overlay_name:
                self.overlay_name.config(text="SIKKORD • Ses bağlı",fg=MUTED);self.overlay_bar.place_configure(width=24)
        else:self.hide_speaker_overlay()
        try:self.r.after(120,self.overlay_tick)
        except:pass

    # ---------- left modes ----------
    def clear_frame(self,frame):
        for w in frame.winfo_children():w.destroy()

    def open_servers_home(self):
        self.current_mode="server";self.current_dm=None
        self._server_sig=None;self._member_sig=None
        self.transition_pulse()
        if hasattr(self,"call_btn"):self.call_btn.config(text="GRUP ARAMASI")
        if hasattr(self,"invite_label"):self.invite_label.config(text=f"Davet: {self.sc}" if self.sc else "")
        self.show_servers()
        if self.sc:self.send({"action":"enter","code":self.sc})

    def server_menu(self):
        if self.current_mode!="server" or not self.sc:return
        s=next((x for x in self.servers if x.get("code")==self.sc),{})
        menu=tk.Menu(self.r,tearoff=0,bg=PANEL2,fg=TEXT,activebackground=RED,activeforeground="white")
        menu.add_command(label="Davet bağlantısını kopyala",command=self.copy_invite);menu.add_separator()
        if s.get("owner")==self.user:menu.add_command(label="Sunucuyu sil",command=self.delete_current_server)
        else:menu.add_command(label="Sunucudan ayrıl",command=self.leave_current_server)
        menu.tk_popup(self.r.winfo_pointerx(),self.r.winfo_pointery())

    def leave_current_server(self):
        if self.sc and messagebox.askyesno("Sunucudan ayrıl","Bu sunucudan ayrılmak istiyor musun?"):
            self.send({"action":"leave_server","code":self.sc})

    def delete_current_server(self):
        if self.sc and messagebox.askyesno("Sunucuyu sil","Sunucu ve tüm mesajları kalıcı olarak silinsin mi?"):
            self.send({"action":"delete_server","code":self.sc})

    def show_servers(self):
        self.current_mode="server"
        self.left_title.config(text="SUNUCULAR")
        sig=tuple((s.get("code"),s.get("name"),s.get("owner")) for s in self.servers)+(self.sc,)
        if sig==self._server_sig and self.left_list.winfo_children():
            self.render_members();return
        self._server_sig=sig
        self.clear_frame(self.left_list)
        for s in self.servers:
            row=tk.Frame(self.left_list,bg="#111419");row.pack(fill="x",pady=2)
            b=tk.Button(row,text=("●  " if s["code"]==self.sc else "○  ")+s["name"],bg="#111419",fg=TEXT,activebackground="#2a171a",activeforeground="white",bd=0,anchor="w",font=("Segoe UI Semibold",10),command=lambda c=s["code"]:self.enter_server(c))
            b.pack(fill="x",ipady=7)
            b.bind("<Button-3>",lambda e,sv=s:self.server_context(e,sv))
        self.render_members()

    def server_context(self,event,s):
        menu=tk.Menu(self.r,tearoff=0,bg=PANEL2,fg=TEXT,activebackground=RED,activeforeground="white")
        menu.add_command(label="Sunucuya git",command=lambda:self.enter_server(s["code"]))
        menu.add_command(label="Davet kodunu kopyala",command=lambda:self.copy_server_code(s["code"]))
        menu.add_command(label="Davet bağlantısını kopyala",command=lambda:self.copy_server_link(s["code"]))
        menu.add_separator()
        if s.get("owner")==self.user:
            menu.add_command(label="Sunucuyu sil",command=lambda:self.confirm_delete_server(s["code"]))
        else:
            menu.add_command(label="Sunucudan ayrıl",command=lambda:self.confirm_leave_server(s["code"]))
        menu.tk_popup(event.x_root,event.y_root)

    def copy_server_code(self,code):
        self.r.clipboard_clear();self.r.clipboard_append(code)

    def copy_server_link(self,code):
        self.r.clipboard_clear();self.r.clipboard_append(f"sikkord://join/{code}")

    def confirm_delete_server(self,code):
        if messagebox.askyesno("Sunucuyu sil","Sunucu ve mesajları kalıcı olarak silinsin mi?"):
            self.send({"action":"delete_server","code":code})

    def confirm_leave_server(self,code):
        if messagebox.askyesno("Sunucudan ayrıl","Bu sunucudan ayrılmak istiyor musun?"):
            self.send({"action":"leave_server","code":code})

    def show_friends(self):
        self.current_mode="friends"; self.current_dm=None
        self.transition_pulse()
        self._friend_sig=None
        self.left_title.config(text="ARKADAŞLAR")
        self.title_label.config(text="Arkadaşlar")
        self.invite_label.config(text="")
        self.clear_frame(self.left_list); self.clear_chat(); self.clear_frame(self.right_list)
        ttk.Button(self.left_list,text="← SUNUCULARA DÖN",command=self.open_servers_home).pack(fill="x",pady=(0,6))
        ttk.Button(self.left_list,text="ARKADAŞ EKLE",style="Red.TButton",command=self.add_friend).pack(fill="x",pady=(0,8))
        if self.friend_requests:
            tk.Label(self.left_list,text="İSTEKLER",bg="#111419",fg=MUTED,font=("Segoe UI",8,"bold")).pack(anchor="w",pady=(6,3))
            for req in self.friend_requests:
                row=tk.Frame(self.left_list,bg=PANEL2);row.pack(fill="x",pady=2)
                tk.Label(row,text=req["sender"],bg=PANEL2,fg=TEXT,font=("Segoe UI",9,"bold")).pack(side="left",padx=6)
                ttk.Button(row,text="✓",command=lambda rid=req["id"]:self.respond_friend(rid,True)).pack(side="right",padx=2)
                ttk.Button(row,text="✕",command=lambda rid=req["id"]:self.respond_friend(rid,False)).pack(side="right",padx=2)
        tk.Label(self.left_list,text="ARKADAŞLARIM",bg="#111419",fg=MUTED,font=("Segoe UI",8,"bold")).pack(anchor="w",pady=(10,3))
        for f in self.friends:
            row=tk.Frame(self.left_list,bg="#111419");row.pack(fill="x",pady=2)
            av=self.avatar_widget(row,f["username"],f.get("avatar"),32);av.pack(side="left",padx=(3,6))
            txt=f'{f["username"]}\n{"Çevrimiçi" if f["online"] else "Son: "+fmt_seen(f.get("last_seen"))}'
            b=tk.Button(row,text=txt,bg="#111419",fg=f.get("color",TEXT) if f["online"] else "#7e8590",activebackground="#21191b",activeforeground="white",bd=0,anchor="w",justify="left",font=("Segoe UI",9),command=lambda u=f["username"]:self.open_dm(u))
            b.pack(side="left",fill="x",expand=True,ipady=5)

    def add_friend(self):
        u=simpledialog.askstring("Arkadaş Ekle","Kullanıcı adı:",parent=self.r)
        if u:self.send({"action":"friend_request","username":u})

    def respond_friend(self,rid,accept):
        self.send({"action":"friend_respond","id":rid,"accept":accept})
        self.r.after(500,lambda:self.send({"action":"friends"}))

    def open_dm(self,username):
        self.current_mode="dm"; self.current_dm=username; self.reply_to=None
        self.transition_pulse()
        self.title_label.config(text="@" + username); self.invite_label.config(text="Özel Sohbet"); self.call_btn.config(text="ÖZELDEN ARA")
        self.right_title.config(text="KİŞİ")
        self.clear_frame(self.right_list)
        f=next((x for x in self.friends if x["username"]==username),None)
        if f:
            av=self.avatar_widget(self.right_list,username,f.get("avatar"),72);av.pack(pady=(18,8))
            tk.Label(self.right_list,text=username,bg="#111419",fg=TEXT,font=("Segoe UI",14,"bold")).pack()
            tk.Label(self.right_list,text="Çevrimiçi" if f["online"] else "Son görülme: "+fmt_seen(f.get("last_seen")),bg="#111419",fg=GREEN if f["online"] else MUTED,font=("Segoe UI",9)).pack(pady=5)
        cached=self.dm_history_cache.get(username)
        if cached:self.load_history(cached)
        self.send({"action":"dm_open","username":username})

    # ---------- server ----------
    def create_server(self):
        n=simpledialog.askstring("Sunucu oluştur","Sunucu adı:",initialvalue="Arkadaşlarım",parent=self.r)
        if n:self.send({"action":"create","name":n})

    def join_server(self):
        raw=simpledialog.askstring("Sunucuya katıl","Davet kodu veya sikkord://join/KOD bağlantısı:",parent=self.r)
        if not raw:return
        code=raw.strip()
        if "sikkord://join/" in code.lower():code=code.rsplit("/",1)[-1]
        self.send({"action":"join","code":code})

    def enter_server(self,code):
        self.current_mode="server";self.current_dm=None
        if hasattr(self,"call_btn"):self.call_btn.config(text="GRUP ARAMASI")
        cached=self.server_history_cache.get(code)
        if cached:
            self.sc=code
            s=next((x for x in self.servers if x.get("code")==code),{})
            self.sn=s.get("name",self.sn)
            self.title_label.config(text=self.sn or "Sunucu")
            self.load_history(cached)
        self.send({"action":"enter","code":code})

    def copy_invite(self):
        if not self.sc:return
        link=f"sikkord://join/{self.sc}"
        self.r.clipboard_clear();self.r.clipboard_append(link)
        messagebox.showinfo("Davet bağlantısı",f"Kopyalandı:\n{link}")

    def update_voice_strip(self):
        if not hasattr(self,"voice_strip_label"):
            return
        voice_users=[m for m in self.members if m.get("voice") or m.get("screen")]
        screen_users=[m for m in self.members if m.get("screen")]
        # Local voice may become active before the next server members packet.
        names=[m.get("username","?") for m in voice_users]
        if self.voice and self.user not in names:
            names.insert(0,self.user)
        if self.active_dm_call:
            self.voice_strip_label.config(
                text=f"ÖZEL ARAMA • {self.active_dm_call} • BAĞLI",
                fg=GREEN
            )
            self.show_call_btn.config(state="normal")
            return
        if names:
            preview=", ".join(names[:4])
            if len(names)>4:
                preview+=f" +{len(names)-4}"
            suffix=""
            if screen_users:
                suffix=f"  •  EKRAN: {screen_users[0].get('username','?')}"
            self.voice_strip_label.config(
                text=f"SESTE {len(names)} KİŞİ • {preview}{suffix}",
                fg=GREEN
            )
            self.show_call_btn.config(state="normal")
        elif screen_users:
            self.voice_strip_label.config(
                text=f"EKRAN PAYLAŞIMI • {screen_users[0].get('username','?')}",
                fg="#7fb7ff"
            )
            self.show_call_btn.config(state="normal")
        else:
            self.voice_strip_label.config(text="SES: Bağlı değil",fg="#8b96a7")
            self.show_call_btn.config(state="disabled")

    def show_call_screen(self):
        screen_users=[m for m in self.members if m.get("screen")]
        if screen_users:
            self.ensure_media("screen")
        if self.voice or self.active_dm_call or screen_users or any(m.get("voice") for m in self.members):
            self.open_call_stage()
        else:
            messagebox.showinfo("SIKKORD","Şu anda seste veya ekran paylaşımında kimse yok.")

    def handle_members_packet(self, items):
        previous=set(self.remote_screen_users)
        self.members=list(items or [])
        active={m.get("username") for m in self.members if m.get("screen") and m.get("username")!=self.user}
        new_screens=active-previous
        self.remote_screen_users=active
        if active:
            self.ensure_media("screen")
        self._member_sig=None
        self.ui(lambda:[self.render_members(),self.update_voice_strip()])
        # Auto-open only when a NEW screen share begins. If user closes the
        # call window, subsequent members refreshes won't force it open again.
        if new_screens:
            self.ui(self.open_call_stage)
        if previous-active and self.last_video_sender in (previous-active):
            self.ui(lambda:self.clear_shared_screen(self.last_video_sender))

    def render_members(self):
        if self.call_stage and self.call_stage.winfo_exists():self.refresh_call_stage()
        if not hasattr(self,"right_list"):return
        self.right_title.config(text="SUNUCUDAKİLER")
        if self.current_mode!="server":return
        sig=tuple((m.get("username"),m.get("online"),m.get("voice"),m.get("muted"),m.get("last_seen"),hash(m.get("avatar") or "")) for m in self.members)
        if sig==self._member_sig and self.right_list.winfo_children():
            return
        self._member_sig=sig
        self.clear_frame(self.right_list)
        for m in self.members:
            row=tk.Frame(self.right_list,bg="#111419",height=50);row.pack(fill="x",pady=2);row.pack_propagate(False)
            av=self.avatar_widget(row,m["username"],m.get("avatar"),34);av.pack(side="left",padx=(5,8),pady=7)
            mid=tk.Frame(row,bg="#111419");mid.pack(side="left",fill="both",expand=True)
            tk.Label(mid,text=m["username"],bg="#111419",fg=m.get("color",TEXT) if m["online"] else "#686f79",anchor="w",font=("Segoe UI Semibold",9)).pack(fill="x",pady=(6,0))
            st="Ekran paylaşıyor" if m.get("screen") else ("Seste" if m.get("voice") else ("Çevrimiçi" if m["online"] else "Son: "+fmt_seen(m.get("last_seen"))))
            if m.get("muted") and m.get("voice"):st="🔇 Sessiz"
            tk.Label(mid,text=st,bg="#111419",fg=GREEN if m["online"] else "#5d646e",anchor="w",font=("Segoe UI",8)).pack(fill="x")
        self.update_voice_strip()

    # ---------- chat ----------
    def clear_chat(self):
        self.current_history=[]; self.message_map={}; self.chat_images={}
        self.chat.config(state="normal");self.chat.delete("1.0","end");self.chat.config(state="disabled")

    def load_history(self,msgs):
        self.current_history=list(msgs or [])
        self._render_token+=1
        token=self._render_token
        self.message_map={};self.chat_images={}
        self.chat.config(state="normal");self.chat.delete("1.0","end");self.chat.config(state="disabled")
        if not self.current_history:
            self.chat.config(state="normal")
            self.chat.insert("end","Henüz mesaj yok. İlk mesajı sen gönder.\n",("system",))
            self.chat.config(state="disabled")
            return
        self._render_history_batch(0,token)

    def _render_history_batch(self,index,token):
        if token!=self._render_token or not hasattr(self,"chat"):return
        batch=self.current_history[index:index+10]
        if not batch:return
        self.chat.config(state="normal")
        for x in batch:
            self.add_message_widget(x,record=False,manage_state=False,history_mode=True)
        self.chat.config(state="disabled")
        nxt=index+len(batch)
        if nxt<len(self.current_history):
            self.r.after(4,lambda:self._render_history_batch(nxt,token))
        else:
            self.chat.see("end")

    def rebuild_chat(self):
        # Only used for rare destructive updates; render in small batches.
        self.load_history(self.current_history)

    def add_message_widget(self,x,record=True,manage_state=True,history_mode=False):
        if manage_state:self.chat.config(state="normal")
        start=self.chat.index("end-1c")
        who=x.get("username") or x.get("sender") or "?"
        # History avatars are resolved from the already-loaded member/friend lists.
        avdata=x.get("avatar")
        if not avdata:
            if who==self.user:
                avdata=self.avatar
            else:
                mm=next((m for m in self.members if m.get("username")==who),None)
                ff=next((f for f in self.friends if f.get("username")==who),None)
                avdata=(mm or ff or {}).get("avatar")
        av=self.avatar_img(avdata,30)
        if av:
            self.chat.image_create("end",image=av,padx=2,pady=2);self.chat_images[x["id"]]=av
        else:self.chat.insert("end"," ● ",("user",))
        color=x.get("color") or next((m.get("color") for m in self.members if m.get("username")==who),None) or next((f.get("color") for f in self.friends if f.get("username")==who),None) or (self.name_color if who==self.user else "#ff6b76")
        tag=f"user_{abs(hash(color))}"
        self.chat.tag_configure(tag,foreground=color,font=("Segoe UI Semibold",11))
        self.chat.insert("end",who+"  ",(tag,))
        self.chat.insert("end",time.strftime("%H:%M",time.localtime(x.get("created_at",time.time())))+"\n",("meta",))
        if x.get("reply_to"):self.chat.insert("end",f"↪ Yanıt: #{x['reply_to']}\n",("meta",))
        if x.get("deleted"):
            self.chat.insert("end","[mesaj silindi]\n\n",("deleted",))
        else:
            body=x.get("text")
            if body is None:body=x.get("message","")
            if body:
                body=str(body);self.chat.insert("end",body+"\n")
                match=re.search(r"sikkord://join/([A-Z0-9-]+)",body,re.I)
                if match:
                    code=match.group(1).upper();tag=f"join_{x['id']}"
                    self.chat.insert("end","[ SUNUCUYA KATIL ]\n",(tag,))
                    self.chat.tag_configure(tag,foreground="#ff6676",underline=True,font=("Segoe UI Semibold",9))
                    self.chat.tag_bind(tag,"<Button-1>",lambda e,c=code:self.send({"action":"join","code":c}))
            if x.get("attachment_name"):
                tag=f"att_{x['id']}"
                label="GÖRSELİ AÇ" if (x.get("attachment_mime") or "").startswith("image/") else "DOSYAYI AÇ"
                self.chat.insert("end",f"▣ {x.get('attachment_name')}  [ {label} ]\n",(tag,))
                self.chat.tag_configure(tag,foreground="#7fb7ff",underline=True,font=("Segoe UI Semibold",9))
                scope="dm" if self.current_mode=="dm" else "server"
                self.chat.tag_bind(tag,"<Button-1>",lambda e,mid=x["id"],sc=scope:self.send({"action":"attachment_get","id":mid,"scope":sc}))
                self.chat.tag_bind(tag,"<Enter>",lambda e:self.chat.config(cursor="hand2"))
                self.chat.tag_bind(tag,"<Leave>",lambda e:self.chat.config(cursor="arrow"))
            if self.current_mode=="dm" and x.get("sender")==self.user:
                mark="✓"
                if x.get("delivered_at"):mark="✓✓"
                if x.get("read_at"):mark="✓✓ Okundu"
                self.chat.insert("end",mark+"\n",("meta",))
            self.chat.insert("end","\n")
        self.message_map[x["id"]]={"start":start,"username":who}
        if manage_state:
            self.chat.see("end");self.chat.config(state="disabled")
        if record and not any(i.get("id")==x.get("id") for i in self.current_history):
            self.current_history.append(x)
            if self.current_mode=="server" and self.sc:
                self.server_history_cache[self.sc]=list(self.current_history[-60:])
            elif self.current_mode=="dm" and self.current_dm:
                self.dm_history_cache[self.current_dm]=list(self.current_history[-70:])

    def show_attachment(self,m):
        data=m.get("data")
        if not data:
            messagebox.showerror("Dosya","Dosya alınamadı.");return
        name=m.get("name") or "dosya"
        mime=m.get("mime") or ""
        try:
            raw=base64.b64decode(data)
            if mime.startswith("image/"):
                im=Image.open(io.BytesIO(raw)).convert("RGB")
                im.thumbnail((1100,760),Image.Resampling.LANCZOS)
                w=tk.Toplevel(self.r);w.title(name);w.configure(bg="#07090c")
                photo=ImageTk.PhotoImage(im)
                lab=tk.Label(w,image=photo,bg="#07090c");lab.image=photo;lab.pack(padx=12,pady=12)
            else:
                dest=filedialog.asksaveasfilename(parent=self.r,initialfile=name,title="Dosyayı kaydet")
                if dest:Path(dest).write_bytes(raw)
        except Exception as e:
            messagebox.showerror("Dosya",str(e))


    def send_message(self):
        text=self.msg.get().strip()
        if text=="Mesaj yaz...":text=""
        if not text and not getattr(self,"pending_attachment",None):return
        att=getattr(self,"pending_attachment",None) or {}
        payload={"text":text,"reply_to":self.reply_to,**att}
        if self.current_mode=="dm" and self.current_dm:
            self.send({"action":"dm_send","to":self.current_dm,**payload})
        elif self.current_mode=="server" and self.sc:
            self.send({"action":"server_chat","message":payload.get("text",""),**payload})
        self.msg.delete(0,"end");self.reply_to=None;self.reply_label.config(text="")
        self.pending_attachment=None

    def attach_file(self):
        p=filedialog.askopenfilename(parent=self.r,title="Dosya gönder")
        if not p:return
        try:
            raw=Path(p).read_bytes()
            if len(raw)>6_000_000:
                messagebox.showerror("Dosya","Dosya en fazla 6 MB olabilir.");return
            mime=mimetypes.guess_type(p)[0] or "application/octet-stream"
            self.pending_attachment={"attachment_name":Path(p).name,"attachment_mime":mime,"attachment_data":base64.b64encode(raw).decode("ascii")}
            self.reply_label.config(text="📎 "+Path(p).name)
        except Exception as e:messagebox.showerror("Dosya",str(e))

    def ctrl_v_image(self,event=None):
        try:
            clip=ImageGrab.grabclipboard()
            if isinstance(clip,Image.Image):
                im=clip.convert("RGB");im.thumbnail((1600,1200),Image.Resampling.LANCZOS)
                b=io.BytesIO();im.save(b,"JPEG",quality=82,optimize=True)
                self.pending_attachment={"attachment_name":"clipboard.jpg","attachment_mime":"image/jpeg","attachment_data":base64.b64encode(b.getvalue()).decode("ascii")}
                self.reply_label.config(text="🖼 Panodan resim eklendi")
                return "break"
        except:pass

    def message_menu(self,event):
        idx=self.chat.index(f"@{event.x},{event.y}");line=int(idx.split(".")[0]);target=None
        for mid,info in reversed(list(self.message_map.items())):
            try:
                if int(info["start"].split(".")[0])<=line:target=(mid,info);break
            except:pass
        if not target:return
        mid,info=target
        menu=tk.Menu(self.r,tearoff=0,bg=PANEL2,fg=TEXT,activebackground=RED,activeforeground="white")
        menu.add_command(label="Yanıtla",command=lambda:self.set_reply(mid))
        if self.current_mode=="server":
            menu.add_command(label="Mesaj bilgisi / okuyanlar",command=lambda:self.send({"action":"message_info","id":mid}))
        if info["username"]==self.user:
            if self.current_mode=="dm":menu.add_command(label="🗑 Mesajı sil",command=lambda:self.send({"action":"delete_dm","id":mid}))
            else:menu.add_command(label="🗑 Mesajı sil",command=lambda:self.send({"action":"delete_server_message","id":mid}))
        menu.tk_popup(event.x_root,event.y_root)

    def set_reply(self,mid):
        self.reply_to=mid;self.reply_label.config(text=f"↪ #{mid} mesajına yanıt veriyorsun")

    def mark_deleted(self,mid):
        for x in self.current_history:
            if x.get("id")==mid:x["deleted"]=True;x["text"]="[mesaj silindi]";x["attachment_data"]=None
        self.rebuild_chat()

    # ---------- voice ----------
    def toggle_voice(self):
        if self.voice:self.end_active_call()
        else:self.start_voice("server")

    def start_voice(self,mode="server",peer=None):
        if mode=="server" and not self.sc:
            messagebox.showwarning("SIKKORD","Önce bir sunucuya gir.");return
        with self.voice_start_lock:
            if self.voice or self.voice_starting:return
            self.voice_starting=True
        self.ensure_media("voice")
        threading.Thread(target=self._start_voice_worker,args=(mode,peer),daemon=True).start()

    def _start_voice_worker(self,mode="server",peer=None):
        if not self.voice_ready.wait(timeout=8):
            with self.voice_start_lock:self.voice_starting=False
            self.ui(lambda:messagebox.showerror("Ses","Ses medya bağlantısı hazır değil."));return
        try:
            din,dout=sd.default.device
            indev=self.input_device if self.input_device is not None else din
            outdev=self.output_device if self.output_device is not None else dout
            if indev is None or int(indev)<0:raise RuntimeError("Windows varsayılan mikrofonu bulunamadı.")
            if outdev is None or int(outdev)<0:raise RuntimeError("Windows varsayılan hoparlörü bulunamadı.")
            indev=int(indev);outdev=int(outdev)
            in_rate=int(float(sd.query_devices(indev,"input").get("default_samplerate",48000)))
            out_rate=int(float(sd.query_devices(outdev,"output").get("default_samplerate",48000)))
            in_block=max(160,int(in_rate*.02))
            out_block=max(160,int(out_rate*.02))
            with self.audio_lock:self.audio_by_user.clear()
            self.audio_fx=NoiseSuppressor()
            self.audio_worker_stop.clear()

            self.ins=sd.RawInputStream(
                device=indev,samplerate=in_rate,channels=1,dtype="int16",
                blocksize=in_block,latency="low"
            )
            self.outs=sd.RawOutputStream(
                device=outdev,samplerate=out_rate,channels=1,dtype="int16",
                blocksize=out_block,latency="low"
            )
            self.ins.start();self.outs.start()
            self.voice=True;self.muted=False

            def capture_loop():
                while self.voice and not self.audio_worker_stop.is_set():
                    try:
                        raw,overflow=self.ins.read(in_block)
                        if self.muted:continue
                        x=np.frombuffer(raw,dtype=np.int16).astype(np.float32)/32768.0
                        x=self.audio_fx.process(
                            x,
                            bool(self.app_settings.get("noise_suppression",True)),
                            int(self.app_settings.get("noise_strength",58)),
                        )
                        # Speaker feedback protection: gently duck the mic while
                        # remote speech is coming out of speakers. Headphone users
                        # can disable this in Settings.
                        if bool(self.app_settings.get("echo_protection",True)) and time.monotonic()<self.remote_audio_until:
                            x*=0.32
                        net=resample_linear(x,NETWORK_SAMPLES)
                        try:self.speaking_levels[self.user]=(min(1.0,float(np.sqrt(np.mean(net*net)+1e-9))*3.8),time.monotonic())
                        except:pass
                        self.raw_media("voice",b"P"+pcm16_encode(net))
                    except Exception:
                        if self.voice:time.sleep(.005)

            silence=(np.zeros(out_block,dtype=np.int16)).tobytes()
            def playback_loop():
                while self.voice and not self.audio_worker_stop.is_set():
                    mixes=[]
                    with self.audio_lock:
                        items=list(self.audio_by_user.items())
                    for speaker,q in items:
                        try:raw=q.get_nowait()
                        except queue.Empty:continue
                        try:mixes.append(pcm16_decode(raw))
                        except Exception:pass
                    try:
                        if mixes:
                            y=np.sum(mixes,axis=0)
                            if len(mixes)>1:y/=max(1.0,np.sqrt(float(len(mixes))))
                            peak=float(np.max(np.abs(y))+1e-9)
                            if peak>0.92:y*=0.92/peak
                            self.remote_audio_level=float(np.sqrt(np.mean(y*y)+1e-10))
                            if self.remote_audio_level>0.012:self.remote_audio_until=time.monotonic()+0.055
                            y*=max(0.0,min(1.0,float(self.app_settings.get("output_volume",72))/100.0))
                            y=resample_linear(y,out_block)
                            pcm=(np.clip(y,-0.95,0.95)*32767).astype(np.int16).tobytes()
                            self.outs.write(pcm)
                        else:
                            self.outs.write(silence)
                    except Exception:
                        if self.voice:time.sleep(.005)

            self.capture_thread=threading.Thread(target=capture_loop,daemon=True)
            self.playback_thread=threading.Thread(target=playback_loop,daemon=True)
            self.capture_thread.start();self.playback_thread.start()

            if mode=="dm":
                self.active_dm_call=peer;label=f"Özel arama • {peer}"
            else:
                label=f"Ses kanalı • {self.sn or self.sc}";self.send({"action":"voice","on":True})
            self.ui(lambda:[self._voice_ui_on(label),self.open_call_stage()])
        except Exception as e:
            self.voice=False
            with self.voice_start_lock:self.voice_starting=False
            self.ui(lambda:messagebox.showerror("Ses cihazı",str(e)))

    def _voice_ui_on(self,label):
        if hasattr(self,"vb"):self.vb.config(text="SESTEN AYRIL")
        if hasattr(self,"mb"):self.mb.config(text="MİKROFON AÇIK")
        if hasattr(self,"voice_strip_label"):self.voice_strip_label.config(text=label+"  •  BAĞLI",fg=GREEN)
        if hasattr(self,"show_call_btn"):self.show_call_btn.config(state="normal")
        self.update_voice_strip()
        self.ensure_speaker_overlay()

    def stop_voice(self,notify=True):
        self.voice=False;self.muted=False
        with self.voice_start_lock:self.voice_starting=False
        self.audio_worker_stop.set()
        for s in (self.ins,self.outs):
            try:s.stop();s.close()
            except:pass
        self.ins=self.outs=None
        with self.audio_lock:self.audio_by_user.clear()
        if hasattr(self,"vb"):self.vb.config(text="SESE KATIL")
        if hasattr(self,"mb"):self.mb.config(text="MİKROFON")
        if hasattr(self,"voice_strip_label"):self.voice_strip_label.config(text="SES: Bağlı değil",fg=MUTED)
        self.hide_speaker_overlay()
        if notify and not self.active_dm_call and self.sc:self.send({"action":"voice","on":False})
        self.close_call_stage()

    def end_active_call(self):
        if self.active_dm_call:
            peer=self.active_dm_call;self.active_dm_call=None
            self.send({"action":"dm_call_end","peer":peer});self.stop_voice(notify=False)
        elif self.voice:self.stop_voice(notify=True)

    def toggle_mute(self):
        if not self.voice:
            if self.current_mode=="dm":messagebox.showinfo("SIKKORD","Önce özel aramayı başlat.")
            else:self.start_voice("server")
            return
        self.muted=not self.muted
        if hasattr(self,"mb"):self.mb.config(text="MİKROFON KAPALI" if self.muted else "MİKROFON AÇIK")
        self.send({"action":"mic_mute","muted":self.muted})
        self.refresh_call_stage()

    def call_participants(self):
        if self.active_dm_call:
            peer=next((f for f in self.friends if f.get("username")==self.active_dm_call),{})
            return [
                {"username":self.user,"avatar":self.avatar,"muted":self.muted,"online":True},
                {"username":self.active_dm_call,"avatar":peer.get("avatar"),"muted":False,"online":True},
            ]
        users=[m for m in self.members if m.get("voice")]
        if self.voice and not any(m.get("username")==self.user for m in users):
            users.append({"username":self.user,"avatar":self.avatar,"muted":self.muted,"online":True,"voice":True})
        return users

    def open_call_stage(self):
        if self.call_stage and self.call_stage.winfo_exists():
            self.call_stage.deiconify();self.refresh_call_stage();return
        self.call_stage=tk.Toplevel(self.r);self.call_stage.title("SIKKORD • Arama")
        self.call_stage.geometry("1180x760");self.call_stage.minsize(980,650);self.call_stage.configure(bg="#070a0f")
        top=tk.Frame(self.call_stage,bg="#0d1118",height=62);top.pack(fill="x");top.pack_propagate(False)
        tk.Label(top,text="SIKKORD ARAMA",bg="#0d1118",fg=TEXT,font=("Segoe UI Semibold",15)).pack(side="left",padx=20)
        ttk.Button(top,text="MİKROFON",command=self.toggle_mute).pack(side="right",padx=4,pady=11)
        ttk.Button(top,text="EKRAN PAYLAŞ",command=self.toggle_screen).pack(side="right",padx=4,pady=11)
        ttk.Button(top,text="ARAMAYI BİTİR",style="Red.TButton",command=self.end_active_call).pack(side="right",padx=12,pady=11)
        self.call_stage_people=tk.Frame(self.call_stage,bg="#0a0e14",height=154);self.call_stage_people.pack(fill="x",padx=12,pady=(10,6));self.call_stage_people.pack_propagate(False)
        self.call_stage_body=tk.Frame(self.call_stage,bg="#06080c");self.call_stage_body.pack(fill="both",expand=True,padx=12,pady=(0,12))
        self.call_stage_screen_canvas=tk.Canvas(self.call_stage_body,bg="#05070a",highlightbackground="#28313d",highlightthickness=1)
        self.call_stage_screen_canvas.pack(fill="both",expand=True)
        self.call_stage_screen_canvas.create_text(590,280,text="Ekran paylaşımı yok",fill="#4f5968",font=("Segoe UI Semibold",14),tags="empty")
        self.call_stage.protocol("WM_DELETE_WINDOW",self.call_stage.withdraw)
        self.refresh_call_stage();self.schedule_call_stage_refresh();self.call_visualizer_tick()
        if self.last_video_frame:self.render_shared_screen()

    def schedule_call_stage_refresh(self):
        if not self.call_stage or not self.call_stage.winfo_exists():return
        try:
            if self.call_stage_timer:self.call_stage.after_cancel(self.call_stage_timer)
        except:pass
        self.call_stage_timer=self.call_stage.after(2500,self._call_stage_tick)

    def _call_stage_tick(self):
        self.call_stage_timer=None
        if self.call_stage and self.call_stage.winfo_exists():
            self.refresh_call_stage();self.schedule_call_stage_refresh()

    def refresh_call_stage(self):
        if not self.call_stage or not self.call_stage.winfo_exists() or not getattr(self,"call_stage_people",None):return
        users=self.call_participants() or [{"username":self.user,"avatar":self.avatar,"muted":self.muted,"color":self.name_color}]
        sig=tuple((u.get("username"),u.get("muted"),u.get("screen"),hash(u.get("avatar") or ""),u.get("color")) for u in users)
        if sig==self.call_stage_sig and self.call_stage_people.winfo_children():return
        self.call_stage_sig=sig;self.call_stage_rings={};self.call_stage_photos={}
        for w in self.call_stage_people.winfo_children():w.destroy()
        for u in users[:8]:
            card=tk.Frame(self.call_stage_people,bg="#111720",highlightbackground="#273241",highlightthickness=1,width=155,height=136);card.pack(side="left",padx=6,pady=8);card.pack_propagate(False)
            c=tk.Canvas(card,width=76,height=76,bg="#111720",highlightthickness=0);c.pack(pady=(8,2))
            c.create_oval(4,4,72,72,outline="#394556",width=2,tags="ring")
            photo=self.avatar_img(u.get("avatar"),58)
            if photo:
                self.call_stage_photos[u.get("username")]=photo;c.create_image(38,38,image=photo)
            else:c.create_text(38,38,text=(u.get("username","?")[:1]).upper(),fill="white",font=("Segoe UI Semibold",18))
            self.call_stage_rings[u.get("username")]=c
            tk.Label(card,text=u.get("username","?"),bg="#111720",fg=u.get("color",TEXT),font=("Segoe UI Semibold",9)).pack()
            status="Ekran paylaşıyor" if u.get("screen") else ("Mikrofon kapalı" if u.get("muted") else "Aramada")
            tk.Label(card,text=status,bg="#111720",fg="#ff8290" if u.get("muted") else GREEN,font=("Segoe UI",7)).pack()

    def call_visualizer_tick(self):
        if not self.call_stage or not self.call_stage.winfo_exists():return
        nowm=time.monotonic()
        for name,c in list(self.call_stage_rings.items()):
            level,ts=self.speaking_levels.get(name,(0.0,0.0))
            active=(nowm-ts)<0.28 and level>0.025
            try:
                delta=max(0,min(4,int(level*5))) if active else 0
                c.coords("ring",4-delta,4-delta,72+delta,72+delta)
                c.itemconfigure("ring",outline="#43e58c" if active else "#394556",width=3 if active else 2)
            except:pass
        self.overlay_tick_once()
        self.call_stage.after(100,self.call_visualizer_tick)

    def overlay_tick_once(self):
        if self.voice and self.app_settings.get("overlay",True):self.ensure_speaker_overlay()

    def close_call_stage(self):
        try:
            if self.call_stage and self.call_stage_timer:self.call_stage.after_cancel(self.call_stage_timer)
        except:pass
        self.call_stage_timer=None
        try:
            if self.call_stage:self.call_stage.destroy()
        except:pass
        self.call_stage=None;self.call_stage_body=None;self.call_stage_sig=None

    # ---------- call ----------
    def call(self):
        if self.current_mode=="dm" and self.current_dm:
            peer=self.current_dm;self.send({"action":"dm_call_start","peer":peer});self.start_voice("dm",peer)
            if hasattr(self,"voice_strip_label"):self.voice_strip_label.config(text=f"ARANIYOR: {peer} ...",fg="#ffcc66")
            return
        if self.current_mode=="server" and self.sc:
            self.open_call_stage()
            if hasattr(self,"voice_strip_label"):
                self.voice_strip_label.config(text="GRUP ARAMASI BAŞLATILIYOR...",fg="#ffcc66")
            self.send({"action":"call_start"})
            if not self.voice:self.start_voice("server")
            return
        messagebox.showinfo("Arama","Bir sunucu veya özel sohbet seç.")

    def incoming(self,username):
        self.start_ringtone()
        w=tk.Toplevel(self.r);w.title("Grup Araması");w.geometry("450x280");w.configure(bg=PANEL);w.grab_set()
        tk.Label(w,text="GELEN GRUP ARAMASI",bg=PANEL,fg=RED2,font=("Segoe UI Semibold",18)).pack(pady=(26,8))
        tk.Label(w,text=f"{username} sunucuyu arıyor",bg=PANEL,fg=TEXT,font=("Segoe UI",12)).pack()
        f=tk.Frame(w,bg=PANEL);f.pack(pady=30)
        def accept():
            self.stop_ringtone()
            self.open_call_stage()
            self.send({"action":"call_answer"})
            self.start_voice("server")
            w.destroy()
        def reject():self.stop_ringtone();self.send({"action":"call_reject"});w.destroy()
        ttk.Button(f,text="KABUL ET",style="Red.TButton",command=accept).pack(side="left",padx=8)
        ttk.Button(f,text="REDDET",command=reject).pack(side="left",padx=8);w.protocol("WM_DELETE_WINDOW",reject)

    def incoming_dm(self,username):
        self.start_ringtone()
        w=tk.Toplevel(self.r);w.title("Özel Arama");w.geometry("450x300");w.configure(bg=PANEL);w.grab_set()
        tk.Label(w,text="ÖZEL ARAMA",bg=PANEL,fg=RED2,font=("Segoe UI Semibold",20)).pack(pady=(28,8))
        tk.Label(w,text=f"{username} seni arıyor",bg=PANEL,fg=TEXT,font=("Segoe UI",13)).pack()
        tk.Label(w,text="Arama cevaplanana kadar zil çalmaya devam eder.",bg=PANEL,fg=MUTED,font=("Segoe UI",9)).pack(pady=6)
        f=tk.Frame(w,bg=PANEL);f.pack(pady=28)
        def accept():self.stop_ringtone();self.send({"action":"dm_call_answer","peer":username});self.start_voice("dm",username);w.destroy()
        def reject():self.stop_ringtone();self.send({"action":"dm_call_reject","peer":username});w.destroy()
        ttk.Button(f,text="KABUL ET",style="Red.TButton",command=accept).pack(side="left",padx=8)
        ttk.Button(f,text="REDDET",command=reject).pack(side="left",padx=8);w.protocol("WM_DELETE_WINDOW",reject)

    # ---------- screen ----------
    def toggle_screen(self):
        if self.screen:
            self.stop_screen()
        elif not self.screen_starting:
            self.start_screen()

    def start_screen(self):
        if not self.sc:
            messagebox.showwarning("Ekran paylaşımı","Önce bir sunucuya gir.")
            return
        if self.screen or self.screen_starting:
            return
        self.screen_starting=True
        self.open_call_stage()
        if hasattr(self,"sb"):
            self.sb.config(text="HAZIRLANIYOR...")
        self.ensure_media("screen")
        threading.Thread(target=self._start_screen_worker,daemon=True).start()

    def _start_screen_worker(self):
        if not self.screen_ready.wait(timeout=8):
            self.screen_starting=False
            self.ui(lambda:[
                self.sb.config(text="EKRAN") if hasattr(self,"sb") else None,
                messagebox.showerror("Ekran paylaşımı","Ekran medya bağlantısı kurulamadı.")
            ])
            return
        self.screen=True
        self.screen_starting=False
        self.send({"action":"screen","on":True})
        self.ui(lambda:[
            self.sb.config(text="EKRANI DURDUR") if hasattr(self,"sb") else None,
            self.show_share_banner(),
            self.open_call_stage(),
            self.update_voice_strip()
        ])
        threading.Thread(target=self.screen_loop,daemon=True).start()

    def screen_loop(self):
        profile=str(self.app_settings.get("screen_profile","Eco 540p"))
        if profile.startswith("Dengeli"):
            max_size=(1280,720);fps=6;quality=62
        elif profile.startswith("Net"):
            max_size=(1600,900);fps=6;quality=68
        else:
            max_size=(960,540);fps=5;quality=54

        frame_interval=1.0/fps
        try:
            with mss.mss() as sct:
                monitors=sct.monitors
                monitor=monitors[1] if len(monitors)>1 else monitors[0]
                while self.screen:
                    started=time.perf_counter()
                    shot=sct.grab(monitor)
                    im=Image.frombytes("RGB",shot.size,shot.bgra,"raw","BGRX")
                    im.thumbnail(max_size,Image.Resampling.BILINEAR)
                    b=io.BytesIO()
                    im.save(b,"JPEG",quality=quality,optimize=False)
                    frame=b.getvalue()

                    # Sender also receives a lightweight local preview so it's
                    # immediately obvious that sharing is actually active.
                    nowm=time.monotonic()
                    if nowm-self.local_preview_last>0.35:
                        self.local_preview_last=nowm
                        self.last_video_sender=self.user
                        self.last_video_frame=frame
                        self.schedule_screen_render()

                    self.raw_media("screen",b"S"+frame)
                    time.sleep(max(0.0,frame_interval-(time.perf_counter()-started)))
        except Exception as e:
            self.screen=False
            self.screen_starting=False
            self.send({"action":"screen","on":False})
            self.ui(lambda:[
                self.hide_share_banner(),
                self.sb.config(text="EKRAN") if hasattr(self,"sb") else None,
                messagebox.showerror("Ekran paylaşımı",str(e))
            ])

    def stop_screen(self):
        was_screen=self.screen or self.screen_starting
        self.screen=False
        self.screen_starting=False
        if hasattr(self,"sb"):
            self.sb.config(text="EKRAN")
        if was_screen:
            self.send({"action":"screen","on":False})
        self.hide_share_banner()
        if self.last_video_sender==self.user:
            self.clear_shared_screen(self.user)
        self.update_voice_strip()

    def schedule_screen_render(self):
        if self.screen_render_pending:
            return
        self.screen_render_pending=True
        def do_render():
            self.screen_render_pending=False
            self.render_shared_screen()
        try:self.r.after(70,do_render)
        except Exception:self.screen_render_pending=False

    def open_video(self):
        # V10 uses one consistent Teams-like call stage instead of a second,
        # confusing viewer window.
        self.open_call_stage()

    def render_shared_screen(self):
        if not self.last_video_frame:
            return
        try:
            im=Image.open(io.BytesIO(self.last_video_frame)).convert("RGB")
        except Exception:
            return

        if self.call_stage_screen_canvas and self.call_stage_screen_canvas.winfo_exists():
            c=self.call_stage_screen_canvas
            cw=max(700,c.winfo_width())
            ch=max(380,c.winfo_height())
            fit=ImageOps.contain(im,(max(320,cw-20),max(200,ch-20)),Image.Resampling.BILINEAR)
            photo=ImageTk.PhotoImage(fit)
            self.call_stage_screen_photo=photo
            c.delete("empty")
            c.delete("screen")
            c.create_image(cw//2,ch//2,image=photo,anchor="center",tags="screen")
            c.delete("screen_text")
            owner=self.last_video_sender or "Kullanıcı"
            c.create_text(
                18,18,text=f"{owner} • EKRAN PAYLAŞIYOR",
                fill="#b9c5d6",anchor="nw",
                font=("Segoe UI Semibold",9),tags="screen_text"
            )

    def clear_shared_screen(self,username=None):
        if username and self.last_video_sender and username!=self.last_video_sender:
            return
        self.last_video_frame=None
        self.last_video_sender=None
        self.remote_screen_until=0.0
        if self.call_stage_screen_canvas and self.call_stage_screen_canvas.winfo_exists():
            c=self.call_stage_screen_canvas
            c.delete("screen");c.delete("screen_text");c.delete("empty")
            c.create_text(
                max(350,c.winfo_width()//2),
                max(220,c.winfo_height()//2),
                text="Ekran paylaşımı yok",
                fill="#4f5968",
                font=("Segoe UI Semibold",14),
                tags="empty"
            )

    def update_video(self):
        # Kept for compatibility with old callbacks. V10 renders the latest
        # frame directly through schedule_screen_render().
        self.schedule_screen_render()

    def close_video(self):
        # Closing a viewer must NEVER throw the user back to the login screen.
        # The call stage is hidden; GÖSTER reopens it.
        try:
            if self.call_stage and self.call_stage.winfo_exists():
                self.call_stage.withdraw()
        except Exception:
            pass

    # ---------- settings/profile ----------
    def save_app_settings(self):
        self.app_settings.update({
            "noise_suppression":bool(getattr(self,"noise_var",tk.BooleanVar(value=True)).get()) if hasattr(self,"noise_var") else self.app_settings.get("noise_suppression",True),
            "noise_strength":int(getattr(self,"noise_strength",tk.IntVar(value=58)).get()) if hasattr(self,"noise_strength") else self.app_settings.get("noise_strength",58),
            "echo_protection":bool(self.echo_var.get()) if hasattr(self,"echo_var") else self.app_settings.get("echo_protection",True),
            "output_volume":int(self.output_volume.get()) if hasattr(self,"output_volume") else self.app_settings.get("output_volume",72),
            "screen_profile":str(self.screen_combo.get()) if hasattr(self,"screen_combo") else self.app_settings.get("screen_profile","Eco 540p"),
            "overlay":bool(self.overlay_var.get()) if hasattr(self,"overlay_var") else self.app_settings.get("overlay",True),
            "message_sound":bool(self.msg_sound_var.get()) if hasattr(self,"msg_sound_var") else self.app_settings.get("message_sound",True),
            "call_sound":bool(self.call_sound_var.get()) if hasattr(self,"call_sound_var") else self.app_settings.get("call_sound",True),
            "reduced_motion":bool(self.motion_var.get()) if hasattr(self,"motion_var") else self.app_settings.get("reduced_motion",False),
            "compact_chat":bool(self.compact_var.get()) if hasattr(self,"compact_var") else self.app_settings.get("compact_chat",False),
            "startup":bool(self.startup_var.get()) if hasattr(self,"startup_var") else self.app_settings.get("startup",False),
            "input_device":self.input_device,"output_device":self.output_device,
        })
        save_json(SETTINGS_FILE,self.app_settings)
        if hasattr(self,"chat"):
            try:self.chat.config(font=("Segoe UI",10 if self.app_settings.get("compact_chat") else 11),spacing3=1 if self.app_settings.get("compact_chat") else 4)
            except:pass


    def settings(self):
        w=tk.Toplevel(self.r);w.title("SIKKORD • Ayarlar");w.geometry("980x760");w.minsize(900,700);w.configure(bg=PANEL);w.grab_set()
        tk.Label(w,text="Ayarlar",bg=PANEL,fg=TEXT,font=("Segoe UI Semibold",25)).pack(anchor="w",padx=28,pady=(22,4))
        tk.Label(w,text="Profil • Ses • Görünüm • Bildirimler • Ekran Paylaşımı",bg=PANEL,fg=MUTED,font=("Segoe UI",9)).pack(anchor="w",padx=28,pady=(0,12))
        nb=ttk.Notebook(w);nb.pack(fill="both",expand=True,padx=24,pady=8)
        prof=tk.Frame(nb,bg=PANEL2,padx=24,pady=22);audio=tk.Frame(nb,bg=PANEL2,padx=24,pady=22);look=tk.Frame(nb,bg=PANEL2,padx=24,pady=22);notify=tk.Frame(nb,bg=PANEL2,padx=24,pady=22);screen_box=tk.Frame(nb,bg=PANEL2,padx=24,pady=22)
        nb.add(prof,text="Profil");nb.add(audio,text="Ses");nb.add(look,text="Görünüm");nb.add(notify,text="Bildirimler");nb.add(screen_box,text="Ekran")
        # PROFILE
        row=tk.Frame(prof,bg=PANEL2);row.pack(fill="x")
        av=self.avatar_widget(row,self.user,self.avatar,74);av.pack(side="left",padx=(0,16))
        info=tk.Frame(row,bg=PANEL2);info.pack(side="left",fill="x",expand=True)
        tk.Label(info,text=self.user,bg=PANEL2,fg=self.name_color,font=("Segoe UI Semibold",16)).pack(anchor="w")
        tk.Label(info,text="Çevrimiçi • Profiliniz tüm SIKKORD'da görünür",bg=PANEL2,fg=MUTED,font=("Segoe UI",9)).pack(anchor="w",pady=3)
        ttk.Button(row,text="PROFİL FOTOĞRAFI",command=self.choose_avatar).pack(side="right")
        tk.Label(prof,text="Kullanıcı adı",bg=PANEL2,fg=TEXT,font=("Segoe UI Semibold",10)).pack(anchor="w",pady=(24,5))
        self.rename_entry=ttk.Entry(prof);self.rename_entry.pack(fill="x");self.rename_entry.insert(0,self.user)
        ttk.Button(prof,text="İSMİ DEĞİŞTİR",style="Red.TButton",command=lambda:self.send({"action":"profile_update","username":self.rename_entry.get().strip(),"color":self.name_color})).pack(anchor="w",pady=8)
        tk.Label(prof,text="İsim rengi",bg=PANEL2,fg=TEXT,font=("Segoe UI Semibold",10)).pack(anchor="w",pady=(18,8))
        colors=["#f4f5f7","#ff6575","#ffad5a","#f5df62","#55dc9b","#62b8ff","#9a8cff","#df7cff"]
        colorrow=tk.Frame(prof,bg=PANEL2);colorrow.pack(anchor="w")
        for col in colors:
            tk.Button(colorrow,bg=col,activebackground=col,width=3,height=1,bd=0,command=lambda c=col:self.send({"action":"profile_update","username":self.user,"color":c})).pack(side="left",padx=4)
        ttk.Button(prof,text="OTURUMU KAPAT",command=lambda:[self.logout(),w.destroy()]).pack(anchor="w",pady=(28,0))
        # AUDIO
        tk.Label(audio,text="Ses cihazları",bg=PANEL2,fg=TEXT,font=("Segoe UI Semibold",13)).pack(anchor="w")
        tk.Label(audio,text="Mikrofon",bg=PANEL2,fg=MUTED).pack(anchor="w",pady=(14,4));self.in_combo=ttk.Combobox(audio,state="readonly");self.in_combo.pack(fill="x")
        tk.Label(audio,text="Hoparlör / Kulaklık",bg=PANEL2,fg=MUTED).pack(anchor="w",pady=(12,4));self.out_combo=ttk.Combobox(audio,state="readonly");self.out_combo.pack(fill="x");self.populate_devices()
        self.noise_var=tk.BooleanVar(value=bool(self.app_settings.get("noise_suppression",True)));ttk.Checkbutton(audio,text="Gürültü engelleme",variable=self.noise_var,command=self.save_app_settings).pack(anchor="w",pady=(18,4))
        self.noise_strength=tk.IntVar(value=int(self.app_settings.get("noise_strength",58)));tk.Label(audio,text="Gürültü engelleme gücü",bg=PANEL2,fg=MUTED).pack(anchor="w");tk.Scale(audio,from_=0,to=100,orient="horizontal",variable=self.noise_strength,bg=PANEL2,fg=TEXT,troughcolor="#272d38",highlightthickness=0,command=lambda _=None:self.save_app_settings()).pack(fill="x")
        self.echo_var=tk.BooleanVar(value=bool(self.app_settings.get("echo_protection",True)));ttk.Checkbutton(audio,text="Yankı / geri besleme koruması",variable=self.echo_var,command=self.save_app_settings).pack(anchor="w",pady=6)
        self.output_volume=tk.IntVar(value=int(self.app_settings.get("output_volume",72)));tk.Label(audio,text="Çıkış ses seviyesi",bg=PANEL2,fg=MUTED).pack(anchor="w",pady=(12,0));tk.Scale(audio,from_=20,to=100,orient="horizontal",variable=self.output_volume,bg=PANEL2,fg=TEXT,troughcolor="#272d38",highlightthickness=0,command=lambda _=None:self.save_app_settings()).pack(fill="x")
        # APPEARANCE
        tk.Label(look,text="Premium görünüm",bg=PANEL2,fg=TEXT,font=("Segoe UI Semibold",13)).pack(anchor="w")
        self.overlay_var=tk.BooleanVar(value=bool(self.app_settings.get("overlay",True)));ttk.Checkbutton(look,text="Oyun oynarken konuşan kişiyi ekran köşesinde göster",variable=self.overlay_var,command=self.save_app_settings).pack(anchor="w",pady=(18,6))
        self.motion_var=tk.BooleanVar(value=bool(self.app_settings.get("reduced_motion",False)));ttk.Checkbutton(look,text="Animasyonları azalt (düşük sistem kullanımı)",variable=self.motion_var,command=self.save_app_settings).pack(anchor="w",pady=6)
        self.compact_var=tk.BooleanVar(value=bool(self.app_settings.get("compact_chat",False)));ttk.Checkbutton(look,text="Kompakt sohbet görünümü",variable=self.compact_var,command=self.save_app_settings).pack(anchor="w",pady=6)
        tk.Label(look,text="SIKKORD mikro animasyonları performans odaklıdır; ağır CSS/Chromium animasyonları kullanılmaz.",bg=PANEL2,fg=MUTED,font=("Segoe UI",9),wraplength=760,justify="left").pack(anchor="w",pady=18)
        # NOTIFICATIONS
        tk.Label(notify,text="Bildirim ve arka plan",bg=PANEL2,fg=TEXT,font=("Segoe UI Semibold",13)).pack(anchor="w")
        self.msg_sound_var=tk.BooleanVar(value=bool(self.app_settings.get("message_sound",True)));ttk.Checkbutton(notify,text="Mesaj bildirim sesi",variable=self.msg_sound_var,command=self.save_app_settings).pack(anchor="w",pady=(18,6))
        self.call_sound_var=tk.BooleanVar(value=bool(self.app_settings.get("call_sound",True)));ttk.Checkbutton(notify,text="Gelen arama zil sesi",variable=self.call_sound_var,command=self.save_app_settings).pack(anchor="w",pady=6)
        self.startup_var=tk.BooleanVar(value=bool(self.app_settings.get("startup",False)));ttk.Checkbutton(notify,text="Windows açıldığında SIKKORD'u başlat",variable=self.startup_var,command=lambda:[self.save_app_settings(),self.apply_startup_setting()]).pack(anchor="w",pady=6)
        tk.Label(notify,text="X'e bastığınızda uygulama sistem tepsisinde açık kalır.",bg=PANEL2,fg=MUTED,font=("Segoe UI",9)).pack(anchor="w",pady=14)
        # SCREEN
        tk.Label(screen_box,text="Ekran paylaşımı",bg=PANEL2,fg=TEXT,font=("Segoe UI Semibold",13)).pack(anchor="w")
        tk.Label(screen_box,text="Performans profili",bg=PANEL2,fg=MUTED).pack(anchor="w",pady=(16,5))
        self.screen_combo=ttk.Combobox(screen_box,state="readonly",values=["Eco 540p","Dengeli 720p","Net 900p"]);self.screen_combo.pack(fill="x")
        cur=str(self.app_settings.get("screen_profile","Eco 540p"));self.screen_combo.set(cur if cur in self.screen_combo["values"] else "Eco 540p");self.screen_combo.bind("<<ComboboxSelected>>",lambda e:self.save_app_settings())
        tk.Label(screen_box,text="Paylaşım açıldığında sağ altta kalıcı durum kartı görünür. İzleme penceresi sabit canvas kullanır; görüntü boyutu yüzünden pencere artık büyüyüp küçülmez.",bg=PANEL2,fg=MUTED,font=("Segoe UI",9),wraplength=760,justify="left").pack(anchor="w",pady=18)
        ttk.Button(w,text="KAYDET VE KAPAT",style="Red.TButton",command=lambda:[self.save_app_settings(),w.destroy()]).pack(pady=(2,18))

    def populate_devices(self):
        try:dev=sd.query_devices()
        except:dev=[]
        ins=[(i,d["name"]) for i,d in enumerate(dev) if d.get("max_input_channels",0)>0]
        outs=[(i,d["name"]) for i,d in enumerate(dev) if d.get("max_output_channels",0)>0]
        self._ins=ins;self._outs=outs
        self.in_combo["values"]=[f"{i}: {n}" for i,n in ins];self.out_combo["values"]=[f"{i}: {n}" for i,n in outs]
        din,dout=sd.default.device
        if ins:
            preferred=self.input_device if self.input_device is not None else din
            k=next((k for k,(i,n) in enumerate(ins) if i==preferred),0);self.in_combo.current(k);self.input_device=ins[k][0]
        if outs:
            preferred=self.output_device if self.output_device is not None else dout
            k=next((k for k,(i,n) in enumerate(outs) if i==preferred),0);self.out_combo.current(k);self.output_device=outs[k][0]
        self.in_combo.bind("<<ComboboxSelected>>",lambda e:[setattr(self,"input_device",ins[self.in_combo.current()][0]),self.save_app_settings()])
        self.out_combo.bind("<<ComboboxSelected>>",lambda e:[setattr(self,"output_device",outs[self.out_combo.current()][0]),self.save_app_settings()])

    def apply_startup_setting(self):
        if os.name!="nt":return
        try:
            startup=Path(os.environ.get("APPDATA",str(Path.home()))) / "Microsoft/Windows/Start Menu/Programs/Startup/Sikkord.bat"
            if self.app_settings.get("startup",False):
                exe=Path(sys.executable)
                startup.write_text(f'@echo off\nstart "" "{exe}"\n',encoding="utf-8")
            else:startup.unlink(missing_ok=True)
        except Exception:pass

    def choose_avatar(self):
        p=filedialog.askopenfilename(parent=self.r,title="Profil fotoğrafı seç",filetypes=[("Resim","*.png *.jpg *.jpeg *.webp")])
        if not p:return
        try:
            im=Image.open(p).convert("RGB");im=ImageOps.fit(im,(512,512),Image.Resampling.LANCZOS)
            b=io.BytesIO();im.save(b,"JPEG",quality=84,optimize=True)
            self.avatar=base64.b64encode(b.getvalue()).decode("ascii");self.send({"action":"profile","avatar":self.avatar})
        except Exception as e:messagebox.showerror("Profil",str(e))

    def logout(self):
        self.end_active_call()
        self.clear_token();self.user="";self.token="";self.avatar=None;self.name_color="#f4f5f7";self.sc="";self.current_dm=None
        self.login_ui()

    # ---------- events ----------
    def handle(self,m):
        t=m.get("type")
        if t=="login":
            if m.get("ok"):
                self.user=m["username"];self.avatar=m.get("avatar");self.name_color=m.get("color","#f4f5f7");self.token=m.get("token","");self.save_token(self.token)
                if not hasattr(self,"left_list"):
                    self.ui(self.main_ui)
                else:
                    self.ui(lambda:self.set_status("BAĞLI",GREEN))
                    self.send({"action":"servers"});self.send({"action":"friends"})
            else:self.ui(lambda:messagebox.showerror("SIKKORD",m.get("message","Giriş başarısız.")))
        elif t=="token_login":
            if not m.get("ok"):self.clear_token()
        elif t=="register":self.ui(lambda:messagebox.showinfo("SIKKORD",m.get("message","")))
        elif t=="servers":self.servers=m.get("items",[]);self.ui(self.show_servers)
        elif t=="created":self.servers.append({"name":m["name"],"code":m["code"]});self.ui(self.show_servers);self.send({"action":"enter","code":m["code"]})
        elif t=="joined":
            if m.get("ok"):self.send({"action":"enter","code":m["code"]});self.send({"action":"servers"})
            else:self.ui(lambda:messagebox.showerror("SIKKORD","Davet geçersiz."))
        elif t=="entered":
            self.current_mode="server";self.sc=m["code"];self.sn=m["name"];self.members=m.get("members",[])
            self.server_history_cache[self.sc]=list(m.get("history",[]))
            self.ui(lambda:[self.title_label.config(text=self.sn),self.call_btn.config(text="GRUP ARAMASI"),self.invite_label.config(text=f"Davet: {self.sc}  (tıkla-kopyala)"),self.invite_label.bind("<Button-1>",lambda e:self.copy_invite()),self.load_history(m.get("history",[])),self.render_members(),self.show_servers()])
            active_screens={x.get("username") for x in self.members if x.get("screen") and x.get("username")!=self.user}
            self.remote_screen_users=active_screens
            if active_screens:
                self.ensure_media("screen")
                self.ui(self.open_call_stage)
            self.ui(self.update_voice_strip)
            self.send({"action":"server_read"})
        elif t=="members":self.handle_members_packet(m.get("items",[]))
        elif t in ("server_chat","chat"):
            if "text" not in m and "message" in m:m["text"]=m.get("message","")
            self.ui(lambda:self.add_message_widget(m))
            if m.get("username")!=self.user:
                self.play_notification()
                if self.current_mode=="server" and self.sc:self.send({"action":"server_read"})
        elif t=="server_read_update":
            self.server_read_cache.setdefault(m.get("username",""),set()).update(m.get("ids",[]))
        elif t=="message_info":
            info=m.get("info")
            if info:
                readers=info.get("read_by",[])
                text="Mesaj bilgisi\n\n"+("Okuyanlar:\n"+"\n".join(f"• {x['username']} ({time.strftime('%H:%M',time.localtime(x['read_at']))})" for x in readers) if readers else "Henüz kimse okumadı.")
                self.ui(lambda:messagebox.showinfo("Mesaj Bilgisi",text))
        elif t=="server_manage":
            self.ui(lambda:messagebox.showinfo("Sunucu",m.get("message","")))
            if m.get("ok"):
                self.sc="";self.sn="";self.members=[];self.send({"action":"servers"});self.ui(self.open_servers_home)
        elif t=="server_message_deleted":self.ui(lambda:self.mark_deleted(m["id"]))
        elif t=="friends":
            self.friends=m.get("items",[]);self.friend_requests=m.get("requests",[])
            if self.current_mode=="friends":self.ui(self.show_friends)
        elif t=="friend_request_result":self.ui(lambda:messagebox.showinfo("Arkadaş",m.get("message","")));self.send({"action":"friends"})
        elif t=="friend_request_notice":
            self.ui(lambda:self.r.bell());self.send({"action":"friends"})
        elif t=="friend_presence":
            self.send({"action":"friends"})
        elif t=="dm_history":
            self.dm_history_cache[m.get("peer")]=list(m.get("messages",[]))
            if self.current_dm==m.get("peer"):
                self.ui(lambda:self.load_history(m.get("messages",[])))
                self.send({"action":"dm_read","peer":self.current_dm})
        elif t=="dm_message":
            if m.get("sender")!=self.user:self.play_notification()
            if self.current_mode=="dm" and self.current_dm in (m.get("sender"),m.get("receiver")):
                self.ui(lambda:self.add_message_widget(m))
                if m.get("sender")==self.current_dm:self.send({"action":"dm_read","peer":self.current_dm})
            elif m.get("receiver")==self.user:
                self.ui(lambda:self.r.bell())
        elif t=="dm_read":
            ids=set(m.get("ids",[]))
            for x in self.current_history:
                if x.get("id") in ids:x["read_at"]=m.get("read_at")
            if self.current_dm:
                self.dm_history_cache[self.current_dm]=list(self.current_history[-70:])
        elif t=="dm_deleted":self.ui(lambda:self.mark_deleted(m["id"]))
        elif t=="attachment_data":
            self.ui(lambda:self.show_attachment(m))
        elif t=="group_call_state":
            if m.get("state")=="started":
                self.ui(lambda:[self.open_call_stage(),self.update_voice_strip()])
            elif m.get("state")=="accepted":
                self.ui(self.update_voice_strip)
        elif t=="incoming_call":self.ui(lambda:self.incoming(m["from"]))
        elif t=="incoming_dm_call":self.ui(lambda:self.incoming_dm(m["from"]))
        elif t=="dm_call_state":
            state=m.get("state");peer=m.get("peer")
            if state=="accepted":
                self.stop_ringtone();self.active_dm_call=peer;self.ui(lambda:self._voice_ui_on(f"ÖZEL ARAMA: {peer}"))
            elif state in ("rejected","ended"):
                self.stop_ringtone();self.active_dm_call=None
                self.ui(lambda:[self.stop_voice(notify=False),messagebox.showinfo("Arama","Arama reddedildi." if state=="rejected" else "Arama sona erdi.")])
        elif t=="profile_update":
            if m.get("ok"):
                old=self.user;self.user=m.get("username",self.user);self.name_color=m.get("color",self.name_color)
                self.avatar_cache.clear();self._server_sig=None;self._friend_sig=None;self._member_sig=None
                self.ui(lambda:[self.userbar_name.config(text=self.user,fg=self.name_color) if hasattr(self,"userbar_name") else None,messagebox.showinfo("Profil",m.get("message","Profil güncellendi."))])
                self.send({"action":"servers"});self.send({"action":"friends"})
            else:self.ui(lambda:messagebox.showerror("Profil",m.get("message","Profil güncellenemedi.")))
        elif t=="profile_changed":
            old=m.get("old_username");newu=m.get("username");color=m.get("color","#f4f5f7")
            for arr in (self.members,self.friends,self.current_history):
                for item in arr:
                    if item.get("username")==old:item["username"]=newu;item["color"]=color
                    if item.get("sender")==old:item["sender"]=newu;item["color"]=color
                    if item.get("receiver")==old:item["receiver"]=newu
            self._member_sig=None;self._friend_sig=None;self.ui(lambda:[self.render_members(),self.rebuild_chat() if self.current_history else None])
        elif t=="screen":
            username=m.get("username")
            on=bool(m.get("on"))
            for mm in self.members:
                if mm.get("username")==username:
                    mm["screen"]=on
            self._member_sig=None

            if username!=self.user:
                if on:
                    is_new=username not in self.remote_screen_users
                    self.remote_screen_users.add(username)
                    self.ensure_media("screen")
                    if is_new:
                        self.ui(self.open_call_stage)
                else:
                    self.remote_screen_users.discard(username)
                    self.ui(lambda:self.clear_shared_screen(username))
            self.ui(lambda:[self.render_members(),self.update_voice_strip()])
        elif t=="profile":
            if m.get("ok"):
                self.avatar=m.get("avatar");self.avatar_cache.clear();self._member_sig=None;self._friend_sig=None
                self.send({"action":"friends"})
                if self.sc:self.send({"action":"enter","code":self.sc})
        elif t=="error":self.ui(lambda:messagebox.showerror("SIKKORD",m.get("message","Hata")))

    def close(self):
        if self.force_exit:
            self.exit_app();return
        self.ensure_tray()
        if self.tray_icon is not None:self.r.withdraw()
        else:self.r.iconify()


if __name__=="__main__":
    root=tk.Tk();Sikkord(root);root.mainloop()
