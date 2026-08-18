
import asyncio,json,secrets,sqlite3,hashlib,time
from pathlib import Path
import websockets

import os
HOST="0.0.0.0"; PORT=int(os.environ.get("PORT","8765")); DB=Path(__file__).with_name("sikkord.db")
clients={}; rooms={}; calls={}

def conn():
    c=sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,created_at INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,code TEXT UNIQUE NOT NULL,owner TEXT NOT NULL,created_at INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS members(username TEXT NOT NULL,server_code TEXT NOT NULL,UNIQUE(username,server_code))")
    c.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,username TEXT NOT NULL,server_code TEXT NOT NULL,message TEXT NOT NULL,created_at INTEGER)")
    c.commit(); return c

def hp(p):
    s=secrets.token_bytes(16); d=hashlib.scrypt(p.encode(),salt=s,n=2**14,r=8,p=1)
    return s.hex()+":"+d.hex()
def vp(p,x):
    try:
        s,d=x.split(":"); a=hashlib.scrypt(p.encode(),salt=bytes.fromhex(s),n=2**14,r=8,p=1)
        return secrets.compare_digest(a,bytes.fromhex(d))
    except:return False
def code():
    a="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(2))

def register(u,p):
    if not 3<=len(u)<=24:return False,"Kullanıcı adı 3-24 karakter olmalı."
    if not 6<=len(p)<=128:return False,"Şifre en az 6 karakter olmalı."
    c=conn()
    try:c.execute("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",(u,hp(p),int(time.time())));c.commit();return True,"Kayıt başarılı."
    except sqlite3.IntegrityError:return False,"Bu kullanıcı adı zaten kullanılıyor."
    finally:c.close()
def login(u,p):
    c=conn()
    try:r=c.execute("SELECT password_hash FROM users WHERE username=?",(u,)).fetchone();return bool(r and vp(p,r[0]))
    finally:c.close()
def create_server(n,u):
    c=conn(); cc=code()
    try:
        while c.execute("SELECT 1 FROM servers WHERE code=?",(cc,)).fetchone():cc=code()
        c.execute("INSERT INTO servers(name,code,owner,created_at) VALUES(?,?,?,?)",(n[:40],cc,u,int(time.time())))
        c.execute("INSERT INTO members(username,server_code) VALUES(?,?)",(u,cc));c.commit();return cc
    finally:c.close()
def join(u,cc):
    c=conn()
    try:
        r=c.execute("SELECT name FROM servers WHERE code=?",(cc,)).fetchone()
        if not r:return None
        c.execute("INSERT OR IGNORE INTO members(username,server_code) VALUES(?,?)",(u,cc));c.commit();return r[0]
    finally:c.close()
def servers(u):
    c=conn()
    try:return c.execute("SELECT s.name,s.code FROM servers s JOIN members m ON m.server_code=s.code WHERE m.username=? ORDER BY s.id",(u,)).fetchall()
    finally:c.close()
def hist(cc):
    c=conn()
    try:
        r=c.execute("SELECT username,message,created_at FROM messages WHERE server_code=? ORDER BY id DESC LIMIT 100",(cc,)).fetchall()
        return [{"username":a,"message":b,"created_at":d} for a,b,d in reversed(r)]
    finally:c.close()
def save(u,cc,m):
    c=conn();c.execute("INSERT INTO messages(username,server_code,message,created_at) VALUES(?,?,?,?)",(u,cc,m,int(time.time())));c.commit();c.close()

async def send(w,x):
    try:await w.send(json.dumps(x,ensure_ascii=False))
    except:pass
async def bc(cc,x,exc=None):
    await asyncio.gather(*(send(w,x) for w in list(rooms.get(cc,set())) if w!=exc),return_exceptions=True)
async def relay(pool,b,exc):
    await asyncio.gather(*(raw(w,b) for w in list(pool) if w!=exc),return_exceptions=True)
async def raw(w,b):
    try:await w.send(b)
    except:pass

async def handler(w):
    clients[w]={"u":None,"s":None,"voice":False,"screen":False,"call":None}
    try:
        async for rawdata in w:
            s=clients[w]
            if isinstance(rawdata,bytes):
                if not s["s"] or not rawdata:continue
                if rawdata[:1]==b"V" and s["voice"]:await relay(rooms.get(s["s"],set()),rawdata,w)
                elif rawdata[:1]==b"S" and s["screen"]:await relay(rooms.get(s["s"],set()),rawdata,w)
                continue
            try:m=json.loads(rawdata)
            except:continue
            a=m.get("action")
            if a=="register":
                ok,msg=register(str(m.get("username","")).strip(),str(m.get("password","")));await send(w,{"type":"register","ok":ok,"message":msg});continue
            if a=="login":
                u=str(m.get("username","")).strip()
                if login(u,str(m.get("password",""))):
                    s["u"]=u;await send(w,{"type":"login","ok":True,"username":u})
                else:await send(w,{"type":"login","ok":False,"message":"Kullanıcı adı veya şifre yanlış."})
                continue
            if not s["u"]:
                await send(w,{"type":"error","message":"Önce giriş yap."})
                continue

            if a=="servers":await send(w,{"type":"servers","items":[{"name":n,"code":c} for n,c in servers(s["u"])]})
            elif a=="create":
                n=str(m.get("name","Yeni Sunucu"));cc=create_server(n,s["u"]);await send(w,{"type":"created","name":n[:40],"code":cc})
            elif a=="join":
                cc=str(m.get("code","")).upper().strip();n=join(s["u"],cc);await send(w,{"type":"joined","ok":bool(n),"name":n,"code":cc})
            elif a=="enter":
                cc=str(m.get("code","")).upper().strip();n=join(s["u"],cc)
                if not n:await send(w,{"type":"error","message":"Sunucu bulunamadı."});continue
                old=s["s"]
                if old:rooms.get(old,set()).discard(w)
                s["s"]=cc;rooms.setdefault(cc,set()).add(w)
                await send(w,{"type":"entered","name":n,"code":cc,"history":hist(cc)})
                await bc(cc,{"type":"presence","username":s["u"],"online":True},w)
            elif a=="chat":
                cc=s["s"];mtext=str(m.get("message","")).strip()
                if cc and mtext and len(mtext)<=2000:
                    save(s["u"],cc,mtext)
                    x={"type":"chat","username":s["u"],"message":mtext,"created_at":int(time.time())}
                    await bc(cc,x);await send(w,x)
            elif a=="voice":
                s["voice"]=bool(m.get("on"));await bc(s["s"],{"type":"voice","username":s["u"],"on":s["voice"]})
            elif a=="screen":
                s["screen"]=bool(m.get("on"));await bc(s["s"],{"type":"screen","username":s["u"],"on":s["screen"]})
            elif a=="call_start":
                cc=s["s"];calls[cc]=s["u"];await bc(cc,{"type":"incoming_call","from":s["u"]},w)
            elif a=="call_answer":await bc(s["s"],{"type":"call_state","user":s["u"],"state":"accepted"},w)
            elif a=="call_reject":await bc(s["s"],{"type":"call_state","user":s["u"],"state":"rejected"},w)
            elif a=="call_end":await bc(s["s"],{"type":"call_state","user":s["u"],"state":"ended"},w)
    except websockets.exceptions.ConnectionClosed:pass
    finally:
        s=clients.pop(w,None)
        if s and s.get("s"):
            rooms.get(s["s"],set()).discard(w)
            await bc(s["s"],{"type":"presence","username":s["u"],"online":False})

async def main():
    conn().close();print("SIKKORD FINAL BACKEND -> ws://0.0.0.0:8765")
    async with websockets.serve(handler,HOST,PORT,max_size=8_000_000,ping_interval=20,ping_timeout=20):
        await asyncio.Future()
if __name__=="__main__":asyncio.run(main())
