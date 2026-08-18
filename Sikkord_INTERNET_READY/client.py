
import asyncio
import base64
import io
import json
import mimetypes
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import numpy as np
import sounddevice as sd
import websockets
from PIL import Image, ImageDraw, ImageGrab, ImageOps, ImageTk

SERVER_URL = "wss://sikkord-jrbh.onrender.com"

APP_DIR = Path(os.getenv("APPDATA", Path.home())) / "Sikkord"
APP_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = APP_DIR / "session.json"

RATE = 16000
BG = "#090a0c"
PANEL = "#101216"
PANEL2 = "#17191e"
PANEL3 = "#1d2026"
RED = "#e32636"
RED2 = "#ff4050"
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
        self.user=""; self.token=""; self.avatar=None
        self.servers=[]; self.sc=""; self.sn=""; self.members=[]
        self.friends=[]; self.friend_requests=[]; self.current_dm=None
        self.current_mode="server"  # server | friends | dm
        self.current_history=[]
        self.reply_to=None
        self.voice=False; self.muted=False; self.screen=False
        self.audio=queue.Queue(maxsize=6); self.video=queue.Queue(maxsize=2)
        self.ins=None; self.outs=None
        self.input_device=None; self.output_device=None
        self.voice_ready=threading.Event(); self.screen_ready=threading.Event()
        self.chat_images={}; self.message_map={}
        self.vwin=None; self.vlabel=None
        self.pending=queue.Queue()

        self.style=ttk.Style(); self.style.theme_use("clam")
        self.style.configure("TButton",background=PANEL2,foreground=TEXT,borderwidth=0,padding=(11,8),font=("Segoe UI",10,"bold"))
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

    async def connect_media(self,kind):
        ready=self.voice_ready if kind=="voice" else self.screen_ready
        ready.clear()
        try:
            w=await websockets.connect(SERVER_URL,max_size=12_000_000,ping_interval=15,ping_timeout=15,compression=None)
            await w.send(json.dumps({"action":"media_auth","token":self.token,"kind":kind}))
            auth=await asyncio.wait_for(w.recv(),timeout=8)
            if isinstance(auth,bytes) or not json.loads(auth).get("ok"):
                await w.close(); return
            if kind=="voice": self.voice_ws=w
            else:self.screen_ws=w
            ready.set()
            async for raw in w:
                if kind=="voice" and isinstance(raw,bytes) and raw[:1]==b"V":
                    self.put_audio(raw[1:])
                elif kind=="screen" and isinstance(raw,bytes) and raw[:1]==b"S":
                    self.put_video(raw[1:])
        except Exception:pass
        finally:
            ready.clear()
            if kind=="voice":self.voice_ws=None
            else:self.screen_ws=None

    def raw_media(self,kind,data):
        w=self.voice_ws if kind=="voice" else self.screen_ws
        if self.loop and w:
            asyncio.run_coroutine_threadsafe(w.send(data),self.loop)

    def put_audio(self,data):
        try:
            if self.audio.full(): self.audio.get_nowait()
            self.audio.put_nowait(data)
        except:pass

    def put_video(self,data):
        try:
            if self.video.full(): self.video.get_nowait()
            self.video.put_nowait(data)
        except:pass

    def ui(self,fn):
        try:self.r.after(0,fn)
        except:pass

    # ---------- avatar ----------
    def avatar_img(self,data,size=40):
        if not data:return None
        try:
            im=Image.open(io.BytesIO(base64.b64decode(data))).convert("RGBA")
            im=ImageOps.fit(im,(size,size),Image.Resampling.LANCZOS)
            mask=Image.new("L",(size,size),0)
            ImageDraw.Draw(mask).ellipse((0,0,size-1,size-1),fill=255)
            im.putalpha(mask)
            return ImageTk.PhotoImage(im)
        except:return None

    def avatar_widget(self,parent,username,data,size=38,bg=PANEL2):
        img=self.avatar_img(data,size)
        if img:
            l=tk.Label(parent,image=img,bg=parent.cget("bg")); l.image=img; return l
        c=tk.Canvas(parent,width=size,height=size,bg=parent.cget("bg"),highlightthickness=0)
        c.create_oval(1,1,size-1,size-1,fill="#3a2024",outline="")
        c.create_text(size/2,size/2,text=(username[:1] or "?").upper(),fill="white",font=("Segoe UI",max(9,size//3),"bold"))
        return c

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
        tk.Label(top,text="SIKKORD",bg="#0d0f12",fg=RED2,font=("Segoe UI",22,"bold")).pack(side="left",padx=18)
        self.status_label=tk.Label(top,text="● BAĞLI",bg="#0d0f12",fg=GREEN,font=("Segoe UI",9,"bold"));self.status_label.pack(side="left")
        ttk.Button(top,text="⚙ Ayarlar",command=self.settings).pack(side="right",padx=8,pady=12)
        ttk.Button(top,text="＋ Sunucu",command=self.create_server).pack(side="right",padx=2)
        ttk.Button(top,text="🔗 Katıl",command=self.join_server).pack(side="right",padx=2)
        self.invite_label=tk.Label(top,text="Davet: —",bg="#0d0f12",fg="#d9dce3",font=("Segoe UI",10,"bold"));self.invite_label.pack(side="right",padx=18)

        body=tk.Frame(self.r,bg=BG);body.pack(fill="both",expand=True)

        # far left navigation
        nav=tk.Frame(body,bg="#0c0e11",width=76);nav.pack(side="left",fill="y");nav.pack_propagate(False)
        ttk.Button(nav,text="💬",command=self.show_friends).pack(fill="x",padx=8,pady=(12,6))
        ttk.Button(nav,text="👥",command=self.show_friends).pack(fill="x",padx=8,pady=6)
        ttk.Button(nav,text="＋",command=self.create_server).pack(fill="x",padx=8,pady=6)

        # left pane
        self.left=tk.Frame(body,bg="#111419",width=255);self.left.pack(side="left",fill="y");self.left.pack_propagate(False)
        self.left_title=tk.Label(self.left,text="SUNUCULAR",bg="#111419",fg=MUTED,font=("Segoe UI",9,"bold"),anchor="w")
        self.left_title.pack(fill="x",padx=14,pady=(14,8))
        self.left_list=tk.Frame(self.left,bg="#111419");self.left_list.pack(fill="both",expand=True,padx=8)
        self.userbar=tk.Frame(self.left,bg="#0e1115",height=62);self.userbar.pack(fill="x");self.userbar.pack_propagate(False)
        av=self.avatar_widget(self.userbar,self.user,self.avatar,38,bg=PANEL2);av.pack(side="left",padx=9,pady=11)
        tk.Label(self.userbar,text=self.user,bg="#0e1115",fg=TEXT,font=("Segoe UI",10,"bold")).pack(side="left")

        center=tk.Frame(body,bg=BG);center.pack(side="left",fill="both",expand=True)
        head=tk.Frame(center,bg=PANEL,height=60);head.pack(fill="x");head.pack_propagate(False)
        self.title_label=tk.Label(head,text="Bir sunucu veya arkadaş seç",bg=PANEL,fg=TEXT,font=("Segoe UI",16,"bold"));self.title_label.pack(side="left",padx=18)
        self.call_btn=ttk.Button(head,text="📞 Ara",style="Red.TButton",command=self.call);self.call_btn.pack(side="right",padx=10,pady=10)

        self.chat=tk.Text(center,bg=BG,fg=TEXT,insertbackground="white",border=0,font=("Segoe UI",10),wrap="word",state="disabled",padx=18,pady=15,cursor="arrow")
        self.chat.pack(fill="both",expand=True)
        self.chat.tag_configure("user",foreground="#ff7b85",font=("Segoe UI",10,"bold"))
        self.chat.tag_configure("meta",foreground=MUTED,font=("Segoe UI",8))
        self.chat.tag_configure("deleted",foreground=GRAY,font=("Segoe UI",9,"italic"))
        self.chat.bind("<Button-3>",self.message_menu)

        controls=tk.Frame(center,bg=PANEL,height=112);controls.pack(fill="x");controls.pack_propagate(False)
        self.reply_label=tk.Label(controls,text="",bg=PANEL,fg="#ff9aa2",font=("Segoe UI",8))
        self.reply_label.pack(anchor="w",padx=14)
        self.msg=tk.Entry(controls,bg="#0f1115",fg=TEXT,insertbackground="white",relief="flat",font=("Segoe UI",11))
        self.msg.pack(side="left",fill="x",expand=True,padx=(12,6),pady=14,ipady=9)
        self.msg.bind("<Return>",lambda e:(self.send_message(),"break")[1])
        self.msg.bind("<Control-v>",self.ctrl_v_image)
        ttk.Button(controls,text="📎",command=self.attach_file).pack(side="left",padx=4)
        ttk.Button(controls,text="Gönder",style="Red.TButton",command=self.send_message).pack(side="left",padx=4)
        self.vb=ttk.Button(controls,text="🎙 Sese Katıl",command=self.toggle_voice);self.vb.pack(side="left",padx=4)
        self.mb=ttk.Button(controls,text="🔇 Mikrofon",command=self.toggle_mute);self.mb.pack(side="left",padx=4)
        self.sb=ttk.Button(controls,text="🖥 Ekran",command=self.toggle_screen);self.sb.pack(side="left",padx=(4,10))

        self.right=tk.Frame(body,bg="#111419",width=270);self.right.pack(side="right",fill="y");self.right.pack_propagate(False)
        self.right_title=tk.Label(self.right,text="SUNUCUDAKİLER",bg="#111419",fg=MUTED,font=("Segoe UI",9,"bold"),anchor="w")
        self.right_title.pack(fill="x",padx=14,pady=(14,8))
        self.right_list=tk.Frame(self.right,bg="#111419");self.right_list.pack(fill="both",expand=True,padx=8)

        self.show_servers()
        self.send({"action":"servers"}); self.send({"action":"friends"})

    # ---------- left modes ----------
    def clear_frame(self,frame):
        for w in frame.winfo_children():w.destroy()

    def show_servers(self):
        self.current_mode="server"
        self.left_title.config(text="SUNUCULAR")
        self.clear_frame(self.left_list)
        for s in self.servers:
            b=tk.Button(self.left_list,text="●  "+s["name"],bg="#111419",fg=TEXT,activebackground="#2a171a",activeforeground="white",bd=0,anchor="w",font=("Segoe UI",10,"bold"),command=lambda c=s["code"]:self.enter_server(c))
            b.pack(fill="x",pady=2,ipady=7)
        self.render_members()

    def show_friends(self):
        self.current_mode="friends"; self.current_dm=None
        self.left_title.config(text="ARKADAŞLAR")
        self.title_label.config(text="Arkadaşlar")
        self.invite_label.config(text="")
        self.clear_frame(self.left_list); self.clear_chat(); self.clear_frame(self.right_list)
        ttk.Button(self.left_list,text="＋ Arkadaş Ekle",style="Red.TButton",command=self.add_friend).pack(fill="x",pady=(0,8))
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
            b=tk.Button(row,text=txt,bg="#111419",fg=TEXT if f["online"] else "#7e8590",activebackground="#21191b",activeforeground="white",bd=0,anchor="w",justify="left",font=("Segoe UI",9),command=lambda u=f["username"]:self.open_dm(u))
            b.pack(side="left",fill="x",expand=True,ipady=5)

    def add_friend(self):
        u=simpledialog.askstring("Arkadaş Ekle","Kullanıcı adı:",parent=self.r)
        if u:self.send({"action":"friend_request","username":u})

    def respond_friend(self,rid,accept):
        self.send({"action":"friend_respond","id":rid,"accept":accept})
        self.r.after(500,lambda:self.send({"action":"friends"}))

    def open_dm(self,username):
        self.current_mode="dm"; self.current_dm=username; self.reply_to=None
        self.title_label.config(text="@" + username); self.invite_label.config(text="Özel Sohbet")
        self.right_title.config(text="KİŞİ")
        self.clear_frame(self.right_list)
        f=next((x for x in self.friends if x["username"]==username),None)
        if f:
            av=self.avatar_widget(self.right_list,username,f.get("avatar"),72);av.pack(pady=(18,8))
            tk.Label(self.right_list,text=username,bg="#111419",fg=TEXT,font=("Segoe UI",14,"bold")).pack()
            tk.Label(self.right_list,text="Çevrimiçi" if f["online"] else "Son görülme: "+fmt_seen(f.get("last_seen")),bg="#111419",fg=GREEN if f["online"] else MUTED,font=("Segoe UI",9)).pack(pady=5)
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
        self.current_mode="server";self.current_dm=None;self.send({"action":"enter","code":code})

    def copy_invite(self):
        if not self.sc:return
        link=f"sikkord://join/{self.sc}"
        self.r.clipboard_clear();self.r.clipboard_append(link)
        messagebox.showinfo("Davet bağlantısı",f"Kopyalandı:\n{link}")

    def render_members(self):
        if not hasattr(self,"right_list"):return
        self.right_title.config(text="SUNUCUDAKİLER")
        self.clear_frame(self.right_list)
        if self.current_mode!="server":return
        for m in self.members:
            row=tk.Frame(self.right_list,bg="#111419",height=50);row.pack(fill="x",pady=2);row.pack_propagate(False)
            av=self.avatar_widget(row,m["username"],m.get("avatar"),34);av.pack(side="left",padx=(5,8),pady=7)
            mid=tk.Frame(row,bg="#111419");mid.pack(side="left",fill="both",expand=True)
            tk.Label(mid,text=m["username"],bg="#111419",fg=TEXT if m["online"] else "#686f79",anchor="w",font=("Segoe UI",9,"bold")).pack(fill="x",pady=(6,0))
            st="🎙 Seste" if m.get("voice") else ("Çevrimiçi" if m["online"] else "Son: "+fmt_seen(m.get("last_seen")))
            if m.get("muted") and m.get("voice"):st="🔇 Sessiz"
            tk.Label(mid,text=st,bg="#111419",fg=GREEN if m["online"] else "#5d646e",anchor="w",font=("Segoe UI",8)).pack(fill="x")

    # ---------- chat ----------
    def clear_chat(self):
        self.current_history=[]; self.message_map={}; self.chat_images={}
        self.chat.config(state="normal");self.chat.delete("1.0","end");self.chat.config(state="disabled")

    def load_history(self,msgs):
        self.current_history=list(msgs);self.rebuild_chat()

    def rebuild_chat(self):
        self.chat.config(state="normal");self.chat.delete("1.0","end");self.chat.config(state="disabled")
        self.message_map={};self.chat_images={}
        for x in self.current_history:self.add_message_widget(x,record=False)

    def add_message_widget(self,x,record=True):
        self.chat.config(state="normal")
        start=self.chat.index("end-1c")
        who=x.get("username") or x.get("sender") or "?"
        av=self.avatar_img(x.get("avatar"),30)
        if av:self.chat.image_create("end",image=av,padx=2,pady=2);self.chat_images[x["id"]]=av
        else:self.chat.insert("end"," ● ",("user",))
        self.chat.insert("end",who+"  ",("user",))
        self.chat.insert("end",time.strftime("%H:%M",time.localtime(x.get("created_at",time.time())))+"\n",("meta",))
        if x.get("reply_to"):self.chat.insert("end",f"↪ Yanıt: #{x['reply_to']}\n",("meta",))
        if x.get("deleted"):self.chat.insert("end","[mesaj silindi]\n\n",("deleted",))
        else:
            if x.get("text"):self.chat.insert("end",x["text"]+"\n")
            if x.get("attachment_data"):
                mime=x.get("attachment_mime") or ""
                if mime.startswith("image/"):
                    try:
                        im=Image.open(io.BytesIO(base64.b64decode(x["attachment_data"]))).convert("RGB")
                        im.thumbnail((420,300),Image.Resampling.LANCZOS);photo=ImageTk.PhotoImage(im)
                        self.chat.image_create("end",image=photo,pady=6);self.chat_images[f"a{x['id']}"]=photo
                        self.chat.insert("end","\n")
                    except:self.chat.insert("end","[Resim görüntülenemedi]\n")
                else:self.chat.insert("end",f"📎 {x.get('attachment_name','Dosya')}\n",("meta",))
            # DM delivery/read
            if self.current_mode=="dm" and x.get("sender")==self.user:
                mark="✓"
                if x.get("delivered_at"):mark="✓✓"
                if x.get("read_at"):mark="✓✓ Okundu"
                self.chat.insert("end",mark+"\n",("meta",))
            self.chat.insert("end","\n")
        self.message_map[x["id"]]={"start":start,"username":who}
        self.chat.see("end");self.chat.config(state="disabled")
        if record and not any(i.get("id")==x.get("id") for i in self.current_history):self.current_history.append(x)

    def send_message(self):
        text=self.msg.get().strip()
        if not text and not getattr(self,"pending_attachment",None):return
        att=getattr(self,"pending_attachment",None) or {}
        payload={"text":text,"reply_to":self.reply_to,**att}
        if self.current_mode=="dm" and self.current_dm:
            self.send({"action":"dm_send","to":self.current_dm,**payload})
        elif self.current_mode=="server" and self.sc:
            self.send({"action":"server_chat",**payload})
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
        menu.add_command(label="↩ Yanıtla",command=lambda:self.set_reply(mid))
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
        if self.voice:self.stop_voice()
        else:self.start_voice()

    def start_voice(self):
        if not self.sc:messagebox.showwarning("SIKKORD","Önce bir sunucuya gir.");return
        if not self.voice_ready.wait(timeout=3):
            if self.loop:asyncio.run_coroutine_threadsafe(self.connect_media("voice"),self.loop)
            if not self.voice_ready.wait(timeout=8):
                messagebox.showerror("Ses","Ses sunucusuna bağlanılamadı.");return
        try:
            devices=sd.query_devices()
            default_in,default_out=sd.default.device
            indev=self.input_device if self.input_device is not None else default_in
            outdev=self.output_device if self.output_device is not None else default_out
            if indev is None or indev<0: raise RuntimeError("Varsayılan mikrofon bulunamadı.")
            if outdev is None or outdev<0: raise RuntimeError("Varsayılan hoparlör bulunamadı.")
            rate=16000
            for cand in (16000,48000,44100,32000):
                try:
                    sd.check_input_settings(device=indev,channels=1,samplerate=cand,dtype="float32")
                    sd.check_output_settings(device=outdev,channels=1,samplerate=cand,dtype="int16")
                    rate=cand;break
                except:pass
            block=max(80,int(rate*.02));self.audio=queue.Queue(maxsize=6)
            def inp(data,frames,t,status):
                if not self.voice or self.muted:return
                pcm=(np.clip(data[:,0],-1,1)*32767).astype(np.int16).tobytes()
                self.raw_media("voice",b"V"+pcm)
            def out(data,frames,t,status):
                data.fill(0)
                try:
                    arr=np.frombuffer(self.audio.get_nowait(),dtype=np.int16);n=min(frames,len(arr));data[:n,0]=arr[:n]
                except:pass
            self.ins=sd.InputStream(device=indev,samplerate=rate,channels=1,dtype="float32",blocksize=block,latency="low",callback=inp)
            self.outs=sd.OutputStream(device=outdev,samplerate=rate,channels=1,dtype="int16",blocksize=block,latency="low",callback=out)
            self.ins.start();self.outs.start();self.voice=True;self.muted=False
            self.vb.config(text="🔴 Sesten Ayrıl");self.mb.config(text="🎙 Mikrofon Açık")
            self.send({"action":"voice","on":True})
        except Exception as e:messagebox.showerror("Ses cihazı",str(e))

    def stop_voice(self):
        self.voice=False
        for s in (self.ins,self.outs):
            try:s.stop();s.close()
            except:pass
        self.ins=self.outs=None
        if hasattr(self,"vb"):self.vb.config(text="🎙 Sese Katıl")
        self.send({"action":"voice","on":False})

    def toggle_mute(self):
        if not self.voice:self.start_voice();return
        self.muted=not self.muted;self.mb.config(text="🔇 Sessiz" if self.muted else "🎙 Mikrofon Açık")
        self.send({"action":"mic_mute","muted":self.muted})

    # ---------- call ----------
    def call(self):
        if self.current_mode!="server" or not self.sc:
            messagebox.showinfo("Arama","Grup araması sunucu içindeyken kullanılabilir.");return
        if not self.voice:self.start_voice()
        if self.voice:self.send({"action":"call_start"})

    def incoming(self,username):
        try:self.r.bell()
        except:pass
        w=tk.Toplevel(self.r);w.title("Gelen arama");w.geometry("430x260");w.configure(bg=PANEL);w.grab_set()
        tk.Label(w,text="📞",bg=PANEL,fg=RED2,font=("Segoe UI",42)).pack(pady=10)
        tk.Label(w,text=f"{username} arıyor",bg=PANEL,fg=TEXT,font=("Segoe UI",15,"bold")).pack()
        f=tk.Frame(w,bg=PANEL);f.pack(pady=25)
        ttk.Button(f,text="✔ KABUL ET",style="Red.TButton",command=lambda:[self.start_voice(),self.send({"action":"call_answer"}),w.destroy()]).pack(side="left",padx=8)
        ttk.Button(f,text="✕ REDDET",command=lambda:[self.send({"action":"call_reject"}),w.destroy()]).pack(side="left",padx=8)

    # ---------- screen ----------
    def toggle_screen(self):
        if self.screen:self.stop_screen()
        else:self.start_screen()

    def start_screen(self):
        if not self.sc:return
        if not self.screen_ready.wait(timeout=2):
            if self.loop:asyncio.run_coroutine_threadsafe(self.connect_media("screen"),self.loop)
            if not self.screen_ready.wait(timeout=8):messagebox.showerror("Ekran","Ekran paylaşım sunucusuna bağlanılamadı.");return
        self.screen=True;self.sb.config(text="🔴 Ekranı Durdur");self.send({"action":"screen","on":True})
        threading.Thread(target=self.screen_loop,daemon=True).start();self.open_video()

    def screen_loop(self):
        while self.screen:
            st=time.perf_counter()
            try:
                im=ImageGrab.grab().convert("RGB");im.thumbnail((1920,1080),Image.Resampling.LANCZOS)
                b=io.BytesIO();im.save(b,"JPEG",quality=80,optimize=True)
                self.raw_media("screen",b"S"+b.getvalue())
            except:break
            time.sleep(max(0,1/8-(time.perf_counter()-st)))

    def stop_screen(self):
        self.screen=False;self.sb.config(text="🖥 Ekran");self.send({"action":"screen","on":False});self.close_video()

    def open_video(self):
        if self.vwin and self.vwin.winfo_exists():return
        self.vwin=tk.Toplevel(self.r);self.vwin.title("SIKKORD • Ekran Paylaşımı");self.vwin.geometry("1200x740");self.vwin.configure(bg="#050608")
        self.vlabel=tk.Label(self.vwin,text="Ekran bekleniyor...",bg="#050608",fg="white");self.vlabel.pack(fill="both",expand=True)
        self.update_video()

    def update_video(self):
        if not self.vwin or not self.vwin.winfo_exists():return
        try:
            raw=self.video.get_nowait();im=Image.open(io.BytesIO(raw));im.thumbnail((1160,690),Image.Resampling.LANCZOS)
            p=ImageTk.PhotoImage(im);self.vlabel.config(image=p,text="");self.vlabel.image=p
        except:pass
        self.vwin.after(40,self.update_video)

    def close_video(self):
        try:self.vwin.destroy()
        except:pass
        self.vwin=None

    # ---------- settings/profile ----------
    def settings(self):
        w=tk.Toplevel(self.r);w.title("SIKKORD • Ayarlar");w.geometry("760x700");w.configure(bg=PANEL);w.grab_set()
        tk.Label(w,text="Ayarlar",bg=PANEL,fg=TEXT,font=("Segoe UI",24,"bold")).pack(anchor="w",padx=30,pady=(24,4))
        tk.Label(w,text="Profil • Ses • Gizlilik • Oturum",bg=PANEL,fg=MUTED,font=("Segoe UI",9)).pack(anchor="w",padx=30,pady=(0,18))
        sec=tk.Frame(w,bg=PANEL2,padx=18,pady=18);sec.pack(fill="x",padx=28,pady=6)
        av=self.avatar_widget(sec,self.user,self.avatar,64);av.pack(side="left",padx=(0,15))
        ttk.Button(sec,text="📷 Profil Fotoğrafı Değiştir",command=self.choose_avatar).pack(side="left")
        ttk.Button(sec,text="Oturumu Kapat",command=lambda:[self.logout(),w.destroy()]).pack(side="right")
        tk.Label(w,text="Mikrofon",bg=PANEL,fg=TEXT,font=("Segoe UI",10,"bold")).pack(anchor="w",padx=30,pady=(20,5))
        self.in_combo=ttk.Combobox(w,state="readonly");self.in_combo.pack(fill="x",padx=30)
        tk.Label(w,text="Hoparlör / Kulaklık",bg=PANEL,fg=TEXT,font=("Segoe UI",10,"bold")).pack(anchor="w",padx=30,pady=(16,5))
        self.out_combo=ttk.Combobox(w,state="readonly");self.out_combo.pack(fill="x",padx=30)
        self.populate_devices()
        self.noise_var=tk.BooleanVar(value=True)
        ttk.Checkbutton(w,text="Gürültü engelleme (temel seviye)",variable=self.noise_var).pack(anchor="w",padx=30,pady=18)
        tk.Label(w,text="Oturum 90 gün saklanır; uygulamayı kapatınca hesabın silinmez.",bg=PANEL,fg=MUTED,font=("Segoe UI",9)).pack(anchor="w",padx=30,pady=8)

    def populate_devices(self):
        try:dev=sd.query_devices()
        except:dev=[]
        ins=[(i,d["name"]) for i,d in enumerate(dev) if d.get("max_input_channels",0)>0]
        outs=[(i,d["name"]) for i,d in enumerate(dev) if d.get("max_output_channels",0)>0]
        self._ins=ins;self._outs=outs
        self.in_combo["values"]=[f"{i}: {n}" for i,n in ins];self.out_combo["values"]=[f"{i}: {n}" for i,n in outs]
        din,dout=sd.default.device
        if ins:
            k=next((k for k,(i,n) in enumerate(ins) if i==din),0);self.in_combo.current(k);self.input_device=ins[k][0]
        if outs:
            k=next((k for k,(i,n) in enumerate(outs) if i==dout),0);self.out_combo.current(k);self.output_device=outs[k][0]
        self.in_combo.bind("<<ComboboxSelected>>",lambda e:setattr(self,"input_device",ins[self.in_combo.current()][0]))
        self.out_combo.bind("<<ComboboxSelected>>",lambda e:setattr(self,"output_device",outs[self.out_combo.current()][0]))

    def choose_avatar(self):
        p=filedialog.askopenfilename(parent=self.r,title="Profil fotoğrafı seç",filetypes=[("Resim","*.png *.jpg *.jpeg *.webp")])
        if not p:return
        try:
            im=Image.open(p).convert("RGB");im=ImageOps.fit(im,(512,512),Image.Resampling.LANCZOS)
            b=io.BytesIO();im.save(b,"JPEG",quality=84,optimize=True)
            self.avatar=base64.b64encode(b.getvalue()).decode("ascii");self.send({"action":"profile","avatar":self.avatar})
        except Exception as e:messagebox.showerror("Profil",str(e))

    def logout(self):
        self.clear_token();self.user="";self.token="";self.avatar=None;self.sc="";self.current_dm=None
        self.login_ui()

    # ---------- events ----------
    def handle(self,m):
        t=m.get("type")
        if t=="login":
            if m.get("ok"):
                self.user=m["username"];self.avatar=m.get("avatar");self.token=m.get("token","");self.save_token(self.token)
                self.ui(self.main_ui)
                asyncio.run_coroutine_threadsafe(self.connect_media("voice"),self.loop)
                asyncio.run_coroutine_threadsafe(self.connect_media("screen"),self.loop)
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
            self.ui(lambda:[self.title_label.config(text=self.sn),self.invite_label.config(text=f"Davet: {self.sc}  (tıkla-kopyala)"),self.invite_label.bind("<Button-1>",lambda e:self.copy_invite()),self.load_history(m.get("history",[])),self.render_members(),self.show_servers()])
        elif t=="members":self.members=m.get("items",[]);self.ui(self.render_members)
        elif t=="server_chat":
            self.ui(lambda:self.add_message_widget(m))
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
            if self.current_dm==m.get("peer"):
                self.ui(lambda:self.load_history(m.get("messages",[])))
                self.send({"action":"dm_read","peer":self.current_dm})
        elif t=="dm_message":
            if self.current_mode=="dm" and self.current_dm in (m.get("sender"),m.get("receiver")):
                self.ui(lambda:self.add_message_widget(m))
                if m.get("sender")==self.current_dm:self.send({"action":"dm_read","peer":self.current_dm})
            elif m.get("receiver")==self.user:
                self.ui(lambda:self.r.bell())
        elif t=="dm_read":
            ids=set(m.get("ids",[]))
            for x in self.current_history:
                if x.get("id") in ids:x["read_at"]=m.get("read_at")
            self.ui(self.rebuild_chat)
        elif t=="dm_deleted":self.ui(lambda:self.mark_deleted(m["id"]))
        elif t=="incoming_call":self.ui(lambda:self.incoming(m["from"]))
        elif t=="profile":
            if m.get("ok"):self.avatar=m.get("avatar");self.ui(self.main_ui)
        elif t=="error":self.ui(lambda:messagebox.showerror("SIKKORD",m.get("message","Hata")))

    def close(self):
        self.reconnect=False
        try:self.stop_voice()
        except:pass
        self.screen=False
        try:self.r.destroy()
        except:pass


if __name__=="__main__":
    root=tk.Tk();Sikkord(root);root.mainloop()
