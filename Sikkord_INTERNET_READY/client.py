
import asyncio,json,threading,queue,time,io
import tkinter as tk
from tkinter import ttk,messagebox,simpledialog
import websockets,numpy as np,sounddevice as sd
from PIL import Image,ImageTk,ImageGrab

SERVER_URL="ws://127.0.0.1:8765";RATE=16000;BLOCK=640

class Sikkord:
 def __init__(self,r):
  self.r=r;self.r.title("SIKKORD");self.r.geometry("1180x760");self.r.configure(bg="#111318")
  self.ws=None;self.loop=None;self.connected=False;self.user="";self.sc="";self.sn=""
  self.voice=False;self.screen=False;self.audio=queue.Queue(maxsize=50);self.video=queue.Queue(maxsize=3)
  self.ins=None;self.outs=None;self.vwin=None;self.vlabel=None;self.servers=[]
  self.style=ttk.Style();self.style.theme_use("clam");self.style.configure("TButton",font=("Segoe UI",10),padding=9)
  self.style.configure("TEntry",padding=8);self.style.configure("TLabel",background="#111318",foreground="#E8EAF0")
  self.r.protocol("WM_DELETE_WINDOW",self.close);self.login_ui();threading.Thread(target=self.net,daemon=True).start()

 def net(self):
  self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop);self.loop.run_until_complete(self.net_run())
 async def net_run(self):
  try:
   async with websockets.connect(SERVER_URL,max_size=8_000_000,ping_interval=20,ping_timeout=20) as w:
    self.ws=w;self.connected=True
    async for x in w:
     if isinstance(x,bytes):
      try:
       if x[:1]==b"V" and not self.audio.full():self.audio.put_nowait(x[1:])
       elif x[:1]==b"S" and not self.video.full():self.video.put_nowait(x[1:])
      except:pass
     else:
      try:self.handle(json.loads(x))
      except:pass
  except:self.connected=False

 def send(self,x):
  if self.loop and self.ws and self.connected:asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(x,ensure_ascii=False)),self.loop)
 def raw(self,x):
  if self.loop and self.ws and self.connected:asyncio.run_coroutine_threadsafe(self.ws.send(x),self.loop)
 def ui(self,f):
  try:self.r.after(0,f)
  except:pass

 def clear(self):
  for w in self.r.winfo_children():w.destroy()
 def login_ui(self):
  self.clear();f=tk.Frame(self.r,bg="#111318");f.pack(fill="both",expand=True)
  tk.Label(f,text="SIKKORD",bg="#111318",fg="#7C5CFF",font=("Segoe UI",38,"bold")).pack(pady=(130,5))
  tk.Label(f,text="Arkadaşlarınla konuş. Yazış. Ekranını paylaş.",bg="#111318",fg="#AEB4C2",font=("Segoe UI",12)).pack(pady=(0,35))
  c=tk.Frame(f,bg="#191C23",padx=30,pady=30);c.pack()
  self.eu=ttk.Entry(c,width=38);self.eu.pack(pady=7);self.eu.insert(0,"Kullanıcı adı")
  self.ep=ttk.Entry(c,width=38,show="*");self.ep.pack(pady=7)
  ttk.Button(c,text="GİRİŞ YAP",command=lambda:self.send({"action":"login","username":self.eu.get().strip(),"password":self.ep.get()})).pack(fill="x",pady=5)
  ttk.Button(c,text="KAYIT OL",command=lambda:self.send({"action":"register","username":self.eu.get().strip(),"password":self.ep.get()})).pack(fill="x",pady=5)
  tk.Label(f,text="Bağlantı: "+("hazır" if self.connected else "sunucu bekleniyor"),bg="#111318",fg="#777D8C").pack(pady=18)

 def handle(self,m):
  t=m.get("type")
  if t=="login":
   if m["ok"]:self.user=m["username"];self.ui(self.main_ui);self.send({"action":"servers"})
   else:self.ui(lambda:messagebox.showerror("SIKKORD",m["message"]))
  elif t=="register":self.ui(lambda:messagebox.showinfo("SIKKORD",m["message"]))
  elif t=="servers":self.servers=m["items"];self.ui(self.refresh_servers)
  elif t=="created":
   self.ui(lambda:messagebox.showinfo("Sunucu oluşturuldu",f'{m["name"]}\n\nDavet kodu:\n{m["code"]}'));self.send({"action":"servers"})
  elif t=="joined":
   if m["ok"]:self.sc=m["code"];self.sn=m["name"];self.send({"action":"enter","code":self.sc})
   else:self.ui(lambda:messagebox.showerror("SIKKORD","Davet kodu geçersiz."))
  elif t=="entered":
   self.sc=m["code"];self.sn=m["name"];self.ui(lambda:self.server_title.configure(text=self.sn));self.ui(lambda:self.load(m["history"]))
  elif t=="chat":self.ui(lambda:self.add_chat(m["username"],m["message"]))
  elif t=="presence":self.ui(lambda:self.add_system(f'● {m["username"]} {"çevrimiçi" if m["online"] else "ayrıldı"}'))
  elif t=="voice":
   self.ui(lambda:self.voice_state(m["username"],m["on"]))
  elif t=="screen":
   if m["on"]:self.ui(self.open_video)
   else:self.ui(self.close_video)
  elif t=="incoming_call":self.ui(lambda:self.incoming(m["from"]))

 def main_ui(self):
  self.clear()
  top=tk.Frame(self.r,bg="#181B22",height=65);top.pack(fill="x");top.pack_propagate(False)
  tk.Label(top,text="SIKKORD",bg="#181B22",fg="#8B6CFF",font=("Segoe UI",22,"bold")).pack(side="left",padx=18)
  tk.Label(top,text=self.user,bg="#181B22",fg="#D9DCE5",font=("Segoe UI",10)).pack(side="left")
  ttk.Button(top,text="＋ Sunucu",command=self.create).pack(side="right",padx=8,pady=13)
  ttk.Button(top,text="🔗 Katıl",command=self.join).pack(side="right",padx=2)
  body=tk.Frame(self.r,bg="#111318");body.pack(fill="both",expand=True)
  left=tk.Frame(body,bg="#151820",width=220);left.pack(side="left",fill="y");left.pack_propagate(False)
  tk.Label(left,text="  SUNUCULAR",bg="#151820",fg="#8D93A1",font=("Segoe UI",9,"bold"),anchor="w").pack(fill="x",pady=15)
  self.lb=tk.Listbox(left,bg="#151820",fg="#DDE0E8",selectbackground="#5F46C8",border=0,highlightthickness=0,font=("Segoe UI",11))
  self.lb.pack(fill="both",expand=True,padx=8);self.lb.bind("<Double-1>",self.pick)
  center=tk.Frame(body,bg="#111318");center.pack(side="left",fill="both",expand=True)
  head=tk.Frame(center,bg="#181B22",height=58);head.pack(fill="x");head.pack_propagate(False)
  self.server_title=tk.Label(head,text="Bir sunucu seç",bg="#181B22",fg="#F2F3F7",font=("Segoe UI",16,"bold"));self.server_title.pack(side="left",padx=18,pady=15)
  ttk.Button(head,text="📞 Grup Ara",command=self.call).pack(side="right",padx=10,pady=10)
  self.chat=tk.Text(center,bg="#111318",fg="#E5E7ED",insertbackground="white",border=0,font=("Segoe UI",10),wrap="word",state="disabled",padx=18,pady=15)
  self.chat.pack(fill="both",expand=True)
  controls=tk.Frame(center,bg="#181B22",height=105);controls.pack(fill="x");controls.pack_propagate(False)
  self.msg=ttk.Entry(controls);self.msg.pack(side="left",fill="x",expand=True,padx=12,pady=13);self.msg.bind("<Return>",lambda e:self.chat_send())
  ttk.Button(controls,text="Gönder",command=self.chat_send).pack(side="left",padx=5)
  self.vb=ttk.Button(controls,text="🎙  Sese Katıl",command=self.toggle_voice);self.vb.pack(side="left",padx=5)
  self.sb=ttk.Button(controls,text="🖥  Ekran Paylaş",command=self.toggle_screen);self.sb.pack(side="left",padx=(5,12))
  self.refresh_servers()

 def refresh_servers(self):
  if not hasattr(self,"lb"):return
  self.lb.delete(0,"end")
  for s in self.servers:self.lb.insert("end",f'  {s["name"]}')
 def pick(self,e=None):
  i=self.lb.curselection()
  if i:self.sc=self.servers[i[0]]["code"];self.sn=self.servers[i[0]]["name"];self.send({"action":"enter","code":self.sc})
 def create(self):
  n=simpledialog.askstring("Sunucu oluştur","Sunucu adı:",initialvalue="Arkadaşlarım",parent=self.r)
  if n:self.send({"action":"create","name":n})
 def join(self):
  c=simpledialog.askstring("Sunucuya katıl","Davet kodu:",parent=self.r)
  if c:self.send({"action":"join","code":c})
 def load(self,h):
  self.chat.configure(state="normal");self.chat.delete("1.0","end");self.chat.configure(state="disabled")
  for x in h:self.add_chat(x["username"],x["message"])
 def add_chat(self,u,m):
  self.chat.configure(state="normal");self.chat.insert("end",f"{u}\n",("u",));self.chat.insert("end",f"{m}\n\n");self.chat.see("end");self.chat.configure(state="disabled")
 def add_system(self,m):
  self.chat.configure(state="normal");self.chat.insert("end",m+"\n\n");self.chat.see("end");self.chat.configure(state="disabled")
 def chat_send(self):
  m=self.msg.get().strip()
  if m and self.sc:self.send({"action":"chat","message":m});self.msg.delete(0,"end")

 def voice_state(self,u,on):
  self.add_system(f"🎙 {u} {'ses kanalına katıldı' if on else 'ses kanalından ayrıldı'}")
 def toggle_voice(self):
  if self.voice:self.stop_voice()
  else:self.start_voice()
 def start_voice(self):
  try:
   def inp(d,f,t,s):self.raw(b"V"+(d[:,0]*32767).astype(np.int16).tobytes())
   def out(d,f,t,s):
    d.fill(0)
    try:
     a=np.frombuffer(self.audio.get_nowait(),dtype=np.int16);d[:min(f,len(a)),0]=a[:f]
    except:pass
   self.ins=sd.InputStream(samplerate=RATE,channels=1,dtype="float32",blocksize=BLOCK,callback=inp)
   self.outs=sd.OutputStream(samplerate=RATE,channels=1,dtype="int16",blocksize=BLOCK,callback=out)
   self.ins.start();self.outs.start();self.voice=True;self.vb.configure(text="🔴  Sesten Ayrıl");self.send({"action":"voice","on":True});self.add_system("🎙 SEN: SES KANALINDASIN")
  except Exception as e:messagebox.showerror("Mikrofon hatası",str(e))
 def stop_voice(self):
  self.voice=False
  for x in (self.ins,self.outs):
   try:x.stop();x.close()
   except:pass
  self.ins=self.outs=None;self.send({"action":"voice","on":False});self.vb.configure(text="🎙  Sese Katıl");self.add_system("⚪ SEN: SES KANALINDAN AYRILDIN")

 def call(self):self.send({"action":"call_start"});messagebox.showinfo("📞 Grup araması","Grup üyelerine arama bildirimi gönderildi.")
 def incoming(self,u):
  win=tk.Toplevel(self.r);win.title("Gelen arama");win.geometry("380x220");win.grab_set()
  tk.Label(win,text="📞",font=("Segoe UI",36),fg="#6C54E8").pack(pady=10)
  tk.Label(win,text=f"{u} grup araması başlattı",font=("Segoe UI",13,"bold")).pack()
  f=tk.Frame(win);f.pack(pady=20)
  ttk.Button(f,text="✔ KABUL ET",command=lambda:[self.send({"action":"call_answer"}),win.destroy(),self.start_voice()]).pack(side="left",padx=8)
  ttk.Button(f,text="✖ REDDET",command=lambda:[self.send({"action":"call_reject"}),win.destroy()]).pack(side="left",padx=8)

 def toggle_screen(self):
  if self.screen:self.stop_screen()
  else:self.start_screen()
 def start_screen(self):
  self.screen=True;self.sb.configure(text="🔴  Paylaşımı Durdur");self.send({"action":"screen","on":True});self.open_video();threading.Thread(target=self.screen_loop,daemon=True).start()
 def screen_loop(self):
  while self.screen:
   try:
    im=ImageGrab.grab();im.thumbnail((1280,720));b=io.BytesIO();im.save(b,"JPEG",quality=50,optimize=True);self.raw(b"S"+b.getvalue());time.sleep(.15)
   except:break
 def stop_screen(self):
  self.screen=False;self.send({"action":"screen","on":False});self.sb.configure(text="🖥  Ekran Paylaş");self.close_video()
 def open_video(self):
  if self.vwin and self.vwin.winfo_exists():return
  self.vwin=tk.Toplevel(self.r);self.vwin.title("Sikkord • Ekran Paylaşımı");self.vwin.geometry("1000x650");self.vlabel=tk.Label(self.vwin,text="Ekran bekleniyor...",bg="#08090C",fg="white");self.vlabel.pack(fill="both",expand=True);self.vwin.protocol("WM_DELETE_WINDOW",self.close_video);self.update_video()
 def update_video(self):
  if not self.vwin or not self.vwin.winfo_exists():return
  try:
   x=self.video.get_nowait();im=Image.open(io.BytesIO(x));im.thumbnail((980,620));p=ImageTk.PhotoImage(im);self.vlabel.configure(image=p,text="");self.vlabel.image=p
  except:pass
  self.vwin.after(70,self.update_video)
 def close_video(self):
  try:self.vwin.destroy()
  except:pass
  self.vwin=None;self.vlabel=None
 def close(self):
  self.voice=False;self.screen=False
  try:self.r.destroy()
  except:pass

if __name__=="__main__":
 r=tk.Tk();Sikkord(r);r.mainloop()
