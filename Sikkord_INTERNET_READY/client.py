import asyncio
import base64
import io
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
import sounddevice as sd
import websockets
from PIL import Image, ImageGrab, ImageOps, ImageTk

SERVER_URL = "wss://sikkord-jrbh.onrender.com"
RATE = 16000
BLOCK = 320  # 20 ms: much lower voice latency than the old 40 ms blocks.

BG = "#0b0d10"
PANEL = "#11151a"
PANEL2 = "#171b21"
RED = "#e33434"
RED2 = "#ff4a4a"
TEXT = "#f3f4f6"
MUTED = "#8f98a6"
GREEN = "#35d07f"


class Sikkord:
    def __init__(self, root):
        self.r = root
        self.r.title("SIKKORD")
        self.r.geometry("1280x820")
        self.r.minsize(1050, 680)
        self.r.configure(bg=BG)
        self.r.protocol("WM_DELETE_WINDOW", self.close)

        self.ws = None
        self.voice_ws = None
        self.screen_ws = None
        self.loop = None
        self.connected = False
        self.user = ""
        self.token = ""
        self.avatar = None
        self.sc = ""
        self.sn = ""
        self.invite = ""
        self.servers = []
        self.members = []
        self.voice = False
        self.muted = False
        self.screen = False
        self.audio = queue.Queue(maxsize=8)
        self.video = queue.Queue(maxsize=2)
        self.ins = None
        self.outs = None
        self.input_device = None
        self.output_device = None
        self.vwin = None
        self.vlabel = None
        self.pending = queue.Queue()
        self.reconnect = True
        self.message_widgets = {}
        self.current_history = []
        self.member_rows = {}
        self.chat_images = {}
        self.voice_ready = threading.Event()
        self.screen_ready = threading.Event()

        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure("TButton", background=PANEL2, foreground=TEXT, borderwidth=0,
                             padding=(12, 8), font=("Segoe UI", 10, "bold"))
        self.style.map("TButton", background=[("active", "#252b33")])
        self.style.configure("Red.TButton", background=RED, foreground="white")
        self.style.map("Red.TButton", background=[("active", RED2)])
        self.style.configure("TEntry", fieldbackground="#0f1216", foreground=TEXT, borderwidth=0, padding=9)
        self.style.configure("TCombobox", fieldbackground="#0f1216", foreground=TEXT, padding=8)

        self.login_ui()
        threading.Thread(target=self.net, daemon=True).start()

    # ---------- network ----------
    def net(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.net_run())

    async def connect_media(self, kind):
        ready = self.voice_ready if kind == "voice" else self.screen_ready
        ready.clear()
        try:
            w = await websockets.connect(SERVER_URL, max_size=12_000_000, ping_interval=15, ping_timeout=15, compression=None)
            await w.send(json.dumps({"action": "media_auth", "token": self.token, "kind": kind}, ensure_ascii=False))
            auth = await asyncio.wait_for(w.recv(), timeout=8)
            if isinstance(auth, bytes) or not json.loads(auth).get("ok"):
                await w.close(); return
            if kind == "voice": self.voice_ws = w
            else: self.screen_ws = w
            ready.set()
            async for raw in w:
                if kind == "voice" and isinstance(raw, bytes) and raw[:1] == b"V":
                    self.put_audio(raw[1:])
                elif kind == "screen" and isinstance(raw, bytes) and raw[:1] == b"S":
                    self.put_video(raw[1:])
        except Exception:
            pass
        finally:
            ready.clear()
            if kind == "voice": self.voice_ws = None
            else: self.screen_ws = None

    async def net_run(self):
        while self.reconnect:
            try:
                async with websockets.connect(SERVER_URL, max_size=12_000_000, ping_interval=15, ping_timeout=15) as w:
                    self.ws = w
                    self.connected = True
                    self.ui(lambda: self.set_connection("BAĞLI", GREEN))
                    self.flush_pending()
                    while True:
                        try:
                            x = await w.recv()
                        except Exception:
                            break
                        if isinstance(x, bytes):
                            continue
                        try:
                            self.handle(json.loads(x))
                        except Exception:
                            pass
            except Exception:
                self.connected = False
                self.ui(lambda: self.set_connection("SUNUCU BEKLENİYOR", RED))
            self.ws = None
            self.connected = False
            await asyncio.sleep(3)

    def put_audio(self, data):
        # Never let old audio accumulate. Dropping an old 20 ms packet is preferable
        # to building a multi-second delay.
        try:
            if self.audio.full():
                self.audio.get_nowait()
            self.audio.put_nowait(data)
        except Exception:
            pass

    def put_video(self, data):
        try:
            if self.video.full():
                self.video.get_nowait()
            self.video.put_nowait(data)
        except Exception:
            pass

    def send(self, payload):
        if self.loop and self.ws and self.connected:
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(payload, ensure_ascii=False)), self.loop)
        else:
            try: self.pending.put_nowait(payload)
            except Exception: pass

    def flush_pending(self):
        while self.connected and not self.pending.empty():
            try: self.send(self.pending.get_nowait())
            except Exception: break

    def raw_media(self, kind, data):
        w = self.voice_ws if kind == "voice" else self.screen_ws
        if self.loop and w:
            fut=asyncio.run_coroutine_threadsafe(w.send(data), self.loop)
            try: fut.result(timeout=1.0)
            except Exception: pass

    def ui(self, fn):
        try: self.r.after(0, fn)
        except Exception: pass

    # ---------- UI helpers ----------
    def clear(self):
        for w in self.r.winfo_children(): w.destroy()

    def set_connection(self, text, color):
        if hasattr(self, "status_label"):
            self.status_label.configure(text="● " + text, fg=color)

    def avatar_image(self, data, size=40):
        if not data: return None
        try:
            raw = base64.b64decode(data)
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            im = ImageOps.fit(im, (size, size))
            return ImageTk.PhotoImage(im)
        except Exception:
            return None

    def initials(self, username):
        return (username[:1] or "?").upper()

    def circle_avatar(self, parent, username, data, size=40, bg="#242a31"):
        img = self.avatar_image(data, size)
        if img:
            l = tk.Label(parent, image=img, bg=parent.cget("bg")); l.image = img
        else:
            l = tk.Label(parent, text=self.initials(username), bg=bg, fg="white",
                         font=("Segoe UI", max(10, size//3), "bold"), width=2, height=1)
        return l

    # ---------- login ----------
    def login_ui(self):
        self.clear()
        outer = tk.Frame(self.r, bg=BG); outer.pack(fill="both", expand=True)
        tk.Frame(outer, bg=RED, height=5).pack(fill="x")
        card = tk.Frame(outer, bg=PANEL, padx=42, pady=38); card.place(relx=.5, rely=.48, anchor="center")
        tk.Label(card, text="SIKKORD", bg=PANEL, fg=RED2, font=("Segoe UI", 42, "bold")).pack()
        tk.Label(card, text="Arkadaşlarınla konuş. Yazış. Ekranını paylaş.", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 11)).pack(pady=(2, 25))
        self.eu = ttk.Entry(card, width=38); self.eu.pack(pady=7); self.eu.insert(0, "Kullanıcı adı")
        self.ep = ttk.Entry(card, width=38, show="*"); self.ep.pack(pady=7)
        ttk.Button(card, text="GİRİŞ YAP", style="Red.TButton", command=self.login).pack(fill="x", pady=(12,5))
        ttk.Button(card, text="KAYIT OL", command=self.register).pack(fill="x", pady=5)
        self.status_label = tk.Label(card, text="● SUNUCU BEKLENİYOR", bg=PANEL, fg=RED, font=("Segoe UI",9,"bold"))
        self.status_label.pack(pady=(18,0))

    def login(self):
        self.send({"action":"login","username":self.eu.get().strip(),"password":self.ep.get()})

    def register(self):
        self.send({"action":"register","username":self.eu.get().strip(),"password":self.ep.get()})

    # ---------- server/app ----------
    def main_ui(self):
        self.clear()
        top = tk.Frame(self.r, bg="#0e1115", height=64); top.pack(fill="x"); top.pack_propagate(False)
        tk.Label(top, text="SIKKORD", bg="#0e1115", fg=RED2, font=("Segoe UI",22,"bold")).pack(side="left", padx=18)
        self.status_label = tk.Label(top, text="● BAĞLI", bg="#0e1115", fg=GREEN, font=("Segoe UI",9,"bold")); self.status_label.pack(side="left")
        ttk.Button(top, text="⚙ Ayarlar", command=self.settings).pack(side="right", padx=8, pady=12)
        ttk.Button(top, text="＋ Sunucu", command=self.create).pack(side="right", padx=2)
        ttk.Button(top, text="🔗 Katıl", command=self.join).pack(side="right", padx=2)
        self.invite_label = tk.Label(top, text="Davet: —", bg="#0e1115", fg="#d7dbe3", font=("Segoe UI",10,"bold")); self.invite_label.pack(side="right", padx=18)

        body = tk.Frame(self.r, bg=BG); body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg="#101419", width=220); left.pack(side="left", fill="y"); left.pack_propagate(False)
        tk.Label(left, text="SUNUCULAR", bg="#101419", fg=MUTED, font=("Segoe UI",9,"bold"), anchor="w").pack(fill="x", padx=14, pady=15)
        self.lb = tk.Listbox(left, bg="#101419", fg=TEXT, selectbackground="#4a2024", selectforeground="white", border=0, highlightthickness=0, font=("Segoe UI",11))
        self.lb.pack(fill="both", expand=True, padx=8); self.lb.bind("<Double-1>", self.pick)
        tk.Label(left, text="SIKKORD • 2026", bg="#101419", fg="#4d5560", font=("Segoe UI",8)).pack(pady=10)

        center = tk.Frame(body, bg=BG); center.pack(side="left", fill="both", expand=True)
        head = tk.Frame(center, bg=PANEL, height=58); head.pack(fill="x"); head.pack_propagate(False)
        self.server_title = tk.Label(head, text="Bir sunucu seç", bg=PANEL, fg=TEXT, font=("Segoe UI",16,"bold")); self.server_title.pack(side="left", padx=18)
        ttk.Button(head, text="📞 Grup Ara", style="Red.TButton", command=self.call).pack(side="right", padx=10, pady=10)

        chat_wrap = tk.Frame(center, bg=BG); chat_wrap.pack(fill="both", expand=True)
        self.chat = tk.Text(chat_wrap, bg=BG, fg=TEXT, insertbackground="white", border=0, font=("Segoe UI",10), wrap="word", state="disabled", padx=18, pady=15, cursor="arrow")
        self.chat.pack(fill="both", expand=True)
        self.chat.tag_configure("user", foreground="#ff8585", font=("Segoe UI",10,"bold"))
        self.chat.tag_configure("system", foreground="#747d89", font=("Segoe UI",9,"italic"))
        self.chat.tag_configure("deleted", foreground="#707784", font=("Segoe UI",9,"italic"))
        self.chat.bind("<Button-3>", self.message_menu)

        controls = tk.Frame(center, bg=PANEL, height=108); controls.pack(fill="x"); controls.pack_propagate(False)
        self.msg = tk.Entry(controls, bg="#0f1216", fg=TEXT, insertbackground="white", relief="flat", bd=0, font=("Segoe UI",11))
        self.msg.pack(side="left", fill="x", expand=True, padx=(12,6), pady=15, ipady=9)
        self.msg.bind("<Return>", lambda e:(self.chat_send(), "break")[1])
        self.msg.bind("<Button-1>", lambda e:self.msg.focus_set())
        ttk.Button(controls, text="Gönder", style="Red.TButton", command=self.chat_send).pack(side="left", padx=5)
        self.vb = ttk.Button(controls, text="🎙 Sese Katıl", command=self.toggle_voice); self.vb.pack(side="left", padx=5)
        self.mb = ttk.Button(controls, text="🔇 Mikrofon Kapalı", command=self.toggle_mute); self.mb.pack(side="left", padx=5)
        self.sb = ttk.Button(controls, text="🖥 Ekran Paylaş", command=self.toggle_screen); self.sb.pack(side="left", padx=(5,12))

        right = tk.Frame(body, bg="#101419", width=260); right.pack(side="right", fill="y"); right.pack_propagate(False)
        tk.Label(right, text="SUNUCUDAKİLER", bg="#101419", fg=MUTED, font=("Segoe UI",9,"bold"), anchor="w").pack(fill="x", padx=14, pady=15)
        self.members_frame = tk.Frame(right, bg="#101419"); self.members_frame.pack(fill="both", expand=True, padx=8)
        self.refresh_servers()
        self.render_members()

    def refresh_servers(self):
        if not hasattr(self, "lb"): return
        self.lb.delete(0, "end")
        for s in self.servers:
            mark = "  ● " if s["code"] == self.sc else "  "
            self.lb.insert("end", f"{mark}{s['name']}")

    def pick(self, _=None):
        i = self.lb.curselection()
        if i:
            self.send({"action":"enter","code":self.servers[i[0]]["code"]})

    def create(self):
        name = simpledialog.askstring("Sunucu oluştur", "Sunucu adı:", initialvalue="Arkadaşlarım", parent=self.r)
        if name: self.send({"action":"create","name":name})

    def join(self):
        code = simpledialog.askstring("Sunucuya katıl", "Davet kodu:", parent=self.r)
        if code: self.send({"action":"join","code":code})

    # ---------- server data ----------
    def load(self, history):
        self.current_history = list(history)
        self.rebuild_chat()

    def rebuild_chat(self):
        if not hasattr(self,"chat"): return
        self.chat.configure(state="normal"); self.chat.delete("1.0","end"); self.chat.configure(state="disabled")
        self.message_widgets.clear(); self.chat_images.clear()
        for item in self.current_history:
            self.add_chat(item["username"],item["message"],item["id"],item.get("deleted",False),item.get("avatar"),record=False)

    def add_chat(self, username, message, mid, deleted=False, avatar=None, record=True):
        self.chat.configure(state="normal")
        start=self.chat.index("end-1c")
        img=self.avatar_image(avatar,28)
        if img:
            self.chat.image_create("end", image=img, padx=2, pady=2)
            self.chat_images[mid]=img
        else:
            self.chat.insert("end", f"  {self.initials(username)}  ", ("user",))
        self.chat.insert("end", f"{username}  ", ("user",))
        self.chat.insert("end", ("[mesaj silindi]" if deleted else message)+"\n\n", ("deleted" if deleted else "",))
        self.message_widgets[mid]={"start":start,"username":username}
        self.chat.see("end"); self.chat.configure(state="disabled")
        if record and not any(x.get("id")==mid for x in self.current_history):
            self.current_history.append({"id":mid,"username":username,"avatar":avatar,"message":message,"deleted":deleted})

    def mark_deleted(self, mid):
        for x in self.current_history:
            if x.get("id") == mid:
                x["deleted"] = True
                x["message"] = "[mesaj silindi]"
        self.rebuild_chat()

    def add_system(self, text):
        if not hasattr(self,"chat"): return
        self.chat.configure(state="normal"); self.chat.insert("end", text+"\n\n", ("system",)); self.chat.see("end"); self.chat.configure(state="disabled")

    def message_menu(self, event):
        # Right click near the latest messages: determine username by scanning the nearest lines.
        idx = self.chat.index(f"@{event.x},{event.y}")
        line = int(idx.split('.')[0])
        menu = tk.Menu(self.r, tearoff=0, bg="#171b21", fg=TEXT, activebackground=RED, activeforeground="white")
        target_id = None
        for mid, info in reversed(list(self.message_widgets.items())):
            try:
                l = int(info["start"].split('.')[0])
                if l <= line:
                    target_id = mid; target_user = info["username"]; break
            except Exception: pass
        if target_id and target_user == self.user:
            menu.add_command(label="🗑 Mesajı sil", command=lambda:self.send({"action":"delete_message","id":target_id}))
            menu.tk_popup(event.x_root,event.y_root)

    def chat_send(self):
        text = self.msg.get().strip()
        if text and self.sc:
            self.send({"action":"chat","message":text}); self.msg.delete(0,"end")

    def render_members(self):
        if not hasattr(self,"members_frame"): return
        for w in self.members_frame.winfo_children(): w.destroy()
        self.member_rows.clear()
        for m in self.members:
            row = tk.Frame(self.members_frame, bg="#101419", height=48); row.pack(fill="x", pady=2); row.pack_propagate(False)
            av = self.circle_avatar(row, m["username"], m.get("avatar"), 34); av.pack(side="left", padx=(6,8), pady=6)
            mid = tk.Frame(row, bg="#101419"); mid.pack(side="left", fill="both", expand=True)
            name = tk.Label(mid, text=m["username"], bg="#101419", fg=TEXT if m["online"] else "#626a76", anchor="w", font=("Segoe UI",9,"bold")); name.pack(fill="x", pady=(6,0))
            state = "Çevrimdışı" if not m["online"] else ("🎙 Seste" if m["voice"] else "Çevrimiçi")
            if m.get("muted") and m["voice"]: state = "🔇 Sessiz"
            tk.Label(mid, text=state, bg="#101419", fg=GREEN if m["online"] else "#555d68", anchor="w", font=("Segoe UI",8)).pack(fill="x")
            dot = tk.Label(row, text="●" if m["online"] else "○", bg="#101419", fg=GREEN if m["online"] else "#4c535d", font=("Segoe UI",12)); dot.pack(side="right", padx=8)
            self.member_rows[m["username"]] = row

    # ---------- voice ----------
    def toggle_voice(self):
        if self.voice: self.stop_voice()
        else: self.start_voice()

    def start_voice(self):
        if not self.sc:
            messagebox.showwarning("SIKKORD", "Önce bir sunucuya gir."); return
        if not self.voice_ready.wait(timeout=8):
            # Re-open the media socket if the first one was not ready yet.
            if self.loop:
                asyncio.run_coroutine_threadsafe(self.connect_media("voice"), self.loop)
            if not self.voice_ready.wait(timeout=8):
                messagebox.showerror("Ses bağlantısı", "Ses sunucusuna bağlanılamadı. Render servisi uyanıyor olabilir; birkaç saniye sonra tekrar deneyin.")
                return
        try:
            devices=sd.query_devices()
            if not devices: raise RuntimeError("Windows'ta kullanılabilir ses cihazı bulunamadı.")
            default_in,default_out=sd.default.device
            in_dev=self.input_device if self.input_device is not None else default_in
            out_dev=self.output_device if self.output_device is not None else default_out
            if in_dev is None or in_dev<0: raise RuntimeError("Windows varsayılan mikrofonu bulunamadı. Ayarlar > Ses cihazlarından bir mikrofon seçin.")
            if out_dev is None or out_dev<0: raise RuntimeError("Windows varsayılan hoparlörü bulunamadı. Ayarlar > Ses cihazlarından bir çıkış seçin.")
            # Use a common rate supported by both devices. 16 kHz is light enough for internet voice.
            rate=16000
            for candidate in (16000,48000,44100,32000):
                try:
                    sd.check_input_settings(device=in_dev,channels=1,samplerate=candidate,dtype="float32")
                    sd.check_output_settings(device=out_dev,channels=1,samplerate=candidate,dtype="int16")
                    rate=candidate; break
                except Exception: continue
            block=max(80,int(rate*0.02))
            self.audio=queue.Queue(maxsize=6)
            self.voice_rate=rate; self.voice_block=block
            def inp(data,frames,_time,status):
                if not self.voice or self.muted: return
                try:
                    pcm=(np.clip(data[:,0],-1,1)*32767).astype(np.int16).tobytes()
                    self.raw_media("voice",b"V"+pcm)
                except Exception: pass
            def out(data,frames,_time,status):
                data.fill(0)
                try:
                    raw=self.audio.get_nowait(); arr=np.frombuffer(raw,dtype=np.int16)
                    n=min(frames,len(arr)); data[:n,0]=arr[:n]
                except Exception: pass
            self.ins=sd.InputStream(device=in_dev,samplerate=rate,channels=1,dtype="float32",blocksize=block,latency="low",callback=inp)
            self.outs=sd.OutputStream(device=out_dev,samplerate=rate,channels=1,dtype="int16",blocksize=block,latency="low",callback=out)
            self.ins.start(); self.outs.start()
            self.voice=True; self.muted=False
            self.vb.configure(text="🔴 Sesten Ayrıl"); self.mb.configure(text="🎙 Mikrofon Açık")
            self.send({"action":"voice","on":True}); self.send({"action":"mic_mute","muted":False})
            self.add_system("🎙 SEN: SES KANALINDASIN")
        except Exception as e:
            self.stop_voice(False); messagebox.showerror("Ses cihazı hatası",str(e))

    def stop_voice(self, notify=True):
        self.voice=False; self.muted=False
        for stream in (self.ins,self.outs):
            try: stream.stop(); stream.close()
            except Exception: pass
        self.ins=self.outs=None
        if hasattr(self,"vb"): self.vb.configure(text="🎙 Sese Katıl")
        if hasattr(self,"mb"): self.mb.configure(text="🔇 Mikrofon Kapalı")
        if notify and self.sc: self.send({"action":"voice","on":False})
        if notify: self.add_system("⚪ SEN: SES KANALINDAN AYRILDIN")

    def toggle_mute(self):
        if not self.voice:
            self.start_voice(); return
        self.muted = not self.muted
        self.mb.configure(text="🔇 Mikrofon Sessiz" if self.muted else "🎙 Mikrofon Açık")
        self.send({"action":"mic_mute","muted":self.muted})

    # ---------- calls ----------
    def call(self):
        if not self.sc:
            messagebox.showwarning("SIKKORD", "Önce bir sunucu seç."); return
        if not self.voice:
            self.start_voice()
        if self.voice:
            self.send({"action":"call_start"})

    def incoming(self, username):
        win = tk.Toplevel(self.r); win.title("Gelen grup araması"); win.geometry("420x260"); win.configure(bg=PANEL); win.grab_set()
        tk.Label(win,text="📞",bg=PANEL,fg=RED2,font=("Segoe UI",40)).pack(pady=10)
        tk.Label(win,text=f"{username} grup araması başlattı",bg=PANEL,fg=TEXT,font=("Segoe UI",13,"bold")).pack()
        tk.Label(win,text="Kabul ettiğinde mikrofon ve ses kanalı açılır.",bg=PANEL,fg=MUTED,font=("Segoe UI",9)).pack(pady=5)
        f=tk.Frame(win,bg=PANEL); f.pack(pady=20)
        ttk.Button(f,text="✔ KABUL ET",style="Red.TButton",command=lambda:self.accept_call(win)).pack(side="left",padx=8)
        ttk.Button(f,text="✖ REDDET",command=lambda:[self.send({"action":"call_reject"}),win.destroy()]).pack(side="left",padx=8)

    def accept_call(self, win):
        self.start_voice()
        if self.voice:
            self.send({"action":"call_answer"})
        try: win.destroy()
        except Exception: pass

    # ---------- screen ----------
    def toggle_screen(self):
        if self.screen: self.stop_screen()
        else: self.start_screen()

    def start_screen(self):
        if not self.sc: messagebox.showwarning("SIKKORD","Önce bir sunucuya gir."); return
        self.screen=True; self.sb.configure(text="🔴 Paylaşımı Durdur"); self.send({"action":"screen","on":True}); self.open_video(); threading.Thread(target=self.screen_loop,daemon=True).start()

    def screen_loop(self):
        # 1080p source, capped at 6 FPS, latest-frame-only. This avoids old frames
        # stacking behind the current frame and making the share feel delayed.
        while self.screen:
            started=time.perf_counter()
            try:
                im=ImageGrab.grab()
                im.thumbnail((1920,1080), Image.Resampling.LANCZOS)
                b=io.BytesIO(); im.convert("RGB").save(b,"JPEG",quality=72,optimize=True,progressive=True)
                self.raw_media("screen", b"S"+b.getvalue())
            except Exception: break
            time.sleep(max(0, 1/6-(time.perf_counter()-started)))

    def stop_screen(self):
        self.screen=False; self.send({"action":"screen","on":False}); self.sb.configure(text="🖥 Ekran Paylaş"); self.close_video()

    def open_video(self):
        if self.vwin and self.vwin.winfo_exists(): return
        self.vwin=tk.Toplevel(self.r); self.vwin.title("SIKKORD • Ekran Paylaşımı • 1080p"); self.vwin.geometry("1180x720"); self.vwin.configure(bg="#050608")
        self.vlabel=tk.Label(self.vwin,text="Ekran bekleniyor...",bg="#050608",fg="white"); self.vlabel.pack(fill="both",expand=True); self.vwin.protocol("WM_DELETE_WINDOW",self.close_video); self.update_video()

    def update_video(self):
        if not self.vwin or not self.vwin.winfo_exists(): return
        try:
            raw=self.video.get_nowait(); im=Image.open(io.BytesIO(raw)); im.thumbnail((1140,670),Image.Resampling.LANCZOS); p=ImageTk.PhotoImage(im); self.vlabel.configure(image=p,text=""); self.vlabel.image=p
        except Exception: pass
        self.vwin.after(30,self.update_video)

    def close_video(self):
        try:self.vwin.destroy()
        except Exception:pass
        self.vwin=None;self.vlabel=None

    # ---------- settings ----------
    def settings(self):
        win=tk.Toplevel(self.r); win.title("SIKKORD • Ayarlar"); win.geometry("620x560"); win.configure(bg=PANEL); win.grab_set()
        tk.Label(win,text="Ayarlar",bg=PANEL,fg=TEXT,font=("Segoe UI",22,"bold")).pack(anchor="w",padx=28,pady=(25,5))
        tk.Label(win,text="Ses, profil ve uygulama ayarları",bg=PANEL,fg=MUTED,font=("Segoe UI",9)).pack(anchor="w",padx=28,pady=(0,20))
        prof=tk.Frame(win,bg=PANEL2,padx=18,pady=18); prof.pack(fill="x",padx=24)
        tk.Label(prof,text="Profil fotoğrafı",bg=PANEL2,fg=TEXT,font=("Segoe UI",11,"bold")).pack(side="left")
        ttk.Button(prof,text="📷 Fotoğraf seç",command=lambda:self.choose_avatar(win)).pack(side="right")
        tk.Label(win,text="Mikrofon",bg=PANEL,fg=TEXT,font=("Segoe UI",10,"bold")).pack(anchor="w",padx=28,pady=(24,5))
        self.in_combo=ttk.Combobox(win,state="readonly",width=65); self.in_combo.pack(padx=28,fill="x")
        tk.Label(win,text="Hoparlör / Kulaklık",bg=PANEL,fg=TEXT,font=("Segoe UI",10,"bold")).pack(anchor="w",padx=28,pady=(18,5))
        self.out_combo=ttk.Combobox(win,state="readonly",width=65); self.out_combo.pack(padx=28,fill="x")
        self.populate_devices()
        ttk.Button(win,text="Kaydet ve Kapat",style="Red.TButton",command=win.destroy).pack(pady=30)

    def populate_devices(self):
        try: devices=sd.query_devices()
        except Exception: devices=[]
        ins=[]; outs=[]
        for i,d in enumerate(devices):
            if d.get("max_input_channels",0)>0: ins.append((i,d["name"]))
            if d.get("max_output_channels",0)>0: outs.append((i,d["name"]))
        self._in_devices=ins; self._out_devices=outs
        self.in_combo["values"]=[f"{i}: {n}" for i,n in ins]
        self.out_combo["values"]=[f"{i}: {n}" for i,n in outs]
        if ins:
            idx=next((k for k,(i,_) in enumerate(ins) if i==self.input_device),0); self.in_combo.current(idx)
            self.input_device=ins[idx][0]
        if outs:
            idx=next((k for k,(i,_) in enumerate(outs) if i==self.output_device),0); self.out_combo.current(idx)
            self.output_device=outs[idx][0]
        self.in_combo.bind("<<ComboboxSelected>>",lambda e:self.select_device(True))
        self.out_combo.bind("<<ComboboxSelected>>",lambda e:self.select_device(False))

    def select_device(self, inp):
        arr=self._in_devices if inp else self._out_devices; combo=self.in_combo if inp else self.out_combo
        if combo.current()>=0: (setattr(self,"input_device",arr[combo.current()][0]) if inp else setattr(self,"output_device",arr[combo.current()][0]))

    def choose_avatar(self, parent):
        path=filedialog.askopenfilename(parent=parent,title="Profil fotoğrafı seç",filetypes=[("Resim","*.png *.jpg *.jpeg *.webp")])
        if not path:return
        try:
            im=Image.open(path).convert("RGB"); im.thumbnail((512,512),Image.Resampling.LANCZOS); b=io.BytesIO(); im.save(b,"JPEG",quality=82,optimize=True)
            self.avatar=base64.b64encode(b.getvalue()).decode("ascii"); self.send({"action":"profile","avatar":self.avatar}); messagebox.showinfo("SIKKORD","Profil fotoğrafın kaydedildi.",parent=parent)
        except Exception as e: messagebox.showerror("Profil fotoğrafı",str(e),parent=parent)

    # ---------- message / event handler ----------
    def handle(self,m):
        t=m.get("type")
        if t=="login":
            if m.get("ok"):
                self.user=m["username"]; self.token=m.get("token",""); self.avatar=m.get("avatar"); self.ui(self.main_ui); self.send({"action":"servers"}); self.flush_pending();
                # Media sockets are opened only after successful authentication.
                asyncio.run_coroutine_threadsafe(self.connect_media("voice"),self.loop)
                asyncio.run_coroutine_threadsafe(self.connect_media("screen"),self.loop)
            else:self.ui(lambda:messagebox.showerror("SIKKORD",m.get("message","Giriş başarısız.")))
        elif t=="register": self.ui(lambda:messagebox.showinfo("SIKKORD",m.get("message","Kayıt tamamlandı.")))
        elif t=="servers": self.servers=m.get("items",[]); self.ui(self.refresh_servers)
        elif t=="created": self.ui(lambda:messagebox.showinfo("Sunucu oluşturuldu",f'{m["name"]}\n\nDavet kodu:\n{m["code"]}')); self.send({"action":"servers"}); self.send({"action":"enter","code":m["code"]})
        elif t=="joined":
            if m.get("ok"): self.send({"action":"enter","code":m["code"]})
            else:self.ui(lambda:messagebox.showerror("SIKKORD","Davet kodu geçersiz."))
        elif t=="entered":
            self.sc=m["code"]; self.sn=m["name"]; self.invite=self.sc; self.members=m.get("members",[])
            self.ui(lambda:[self.server_title.configure(text=self.sn),self.invite_label.configure(text=f"Davet: {self.invite}"),self.refresh_servers(),self.render_members(),self.load(m.get("history",[]))])
        elif t=="members": self.members=m.get("items",[]); self.ui(self.render_members)
        elif t=="chat": self.ui(lambda:self.add_chat(m["username"],m["message"],m["id"],False,m.get("avatar")))
        elif t=="message_deleted": self.ui(lambda:self.mark_deleted(m["id"]))
        elif t=="presence": self.ui(lambda:self.add_system(f'● {m["username"]} {"çevrimiçi oldu" if m["online"] else "ayrıldı"}'))
        elif t=="voice": self.ui(lambda:self.add_system(f'🎙 {m["username"]} {"ses kanalına katıldı" if m["on"] else "ses kanalından ayrıldı"}'))
        elif t=="mic_mute": self.ui(lambda:self.add_system(f'🔇 {m["username"]} mikrofonunu {"kapattı" if m["muted"] else "açtı"}'))
        elif t=="incoming_call": self.ui(lambda:self.incoming(m["from"]))
        elif t=="error": self.ui(lambda:messagebox.showerror("SIKKORD",m.get("message","Bilinmeyen hata.")))
        # Members are authoritative; refresh after every important state change.

    def reload_current(self):
        self.rebuild_chat()

    def close(self):
        self.reconnect=False
        self.stop_voice(False)
        self.screen=False
        try:self.r.destroy()
        except Exception:pass


if __name__=="__main__":
    root=tk.Tk(); app=Sikkord(root); root.mainloop()
