import asyncio, hashlib, json, os, secrets, sqlite3, time
from pathlib import Path
import websockets

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
DB = Path(__file__).with_name("sikkord.db")
MAX_AVATAR = 700_000
clients = {}          # websocket -> state
sessions = {}         # token -> main websocket
media_rooms = {}      # code -> {"voice": set(), "screen": set()}


def conn():
    c = sqlite3.connect(DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,avatar TEXT,created_at INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,code TEXT UNIQUE NOT NULL,owner TEXT NOT NULL,created_at INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS members(username TEXT NOT NULL,server_code TEXT NOT NULL,UNIQUE(username,server_code))")
    c.execute("CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,username TEXT NOT NULL,server_code TEXT NOT NULL,message TEXT NOT NULL,created_at INTEGER,deleted INTEGER DEFAULT 0)")
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
    if "avatar" not in cols: c.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
    cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
    if "deleted" not in cols: c.execute("ALTER TABLE messages ADD COLUMN deleted INTEGER DEFAULT 0")
    c.commit(); return c


def hp(p):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(p.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def vp(p, value):
    try:
        salt, digest = value.split(":")
        actual = hashlib.scrypt(p.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return secrets.compare_digest(actual, bytes.fromhex(digest))
    except Exception: return False


def invite_code():
    a = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(a) for _ in range(4)) for _ in range(2))


def register(u, p):
    if not 3 <= len(u) <= 24: return False, "Kullanıcı adı 3-24 karakter olmalı."
    if not 6 <= len(p) <= 128: return False, "Şifre en az 6 karakter olmalı."
    c = conn()
    try:
        c.execute("INSERT INTO users(username,password_hash,avatar,created_at) VALUES(?,?,?,?)", (u,hp(p),None,int(time.time())))
        c.commit(); return True, "Kayıt başarılı. Şimdi giriş yapabilirsin."
    except sqlite3.IntegrityError: return False, "Bu kullanıcı adı zaten kullanılıyor."
    finally: c.close()


def login(u, p):
    c = conn()
    try:
        row = c.execute("SELECT password_hash,avatar FROM users WHERE username=?", (u,)).fetchone()
        return bool(row and vp(p,row[0])), (row[1] if row else None)
    finally: c.close()


def create_server(n,u):
    n=n.strip()[:40] or "Yeni Sunucu"; c=conn(); cc=invite_code()
    try:
        while c.execute("SELECT 1 FROM servers WHERE code=?",(cc,)).fetchone(): cc=invite_code()
        c.execute("INSERT INTO servers(name,code,owner,created_at) VALUES(?,?,?,?)",(n,cc,u,int(time.time())))
        c.execute("INSERT INTO members(username,server_code) VALUES(?,?)",(u,cc)); c.commit(); return cc
    finally:c.close()


def join_server(u,cc):
    c=conn()
    try:
        r=c.execute("SELECT name FROM servers WHERE code=?",(cc,)).fetchone()
        if not r:return None
        c.execute("INSERT OR IGNORE INTO members(username,server_code) VALUES(?,?)",(u,cc)); c.commit(); return r[0]
    finally:c.close()


def user_servers(u):
    c=conn()
    try:return c.execute("SELECT s.name,s.code FROM servers s JOIN members m ON m.server_code=s.code WHERE m.username=? ORDER BY s.id",(u,)).fetchall()
    finally:c.close()


def history(cc):
    c=conn()
    try:
        rows=c.execute("SELECT id,username,message,created_at,deleted FROM messages WHERE server_code=? ORDER BY id DESC LIMIT 150",(cc,)).fetchall()
        out=[]
        for i,u,m,t,d in reversed(rows):
            av=c.execute("SELECT avatar FROM users WHERE username=?",(u,)).fetchone()
            out.append({"id":i,"username":u,"avatar":(av[0] if av else None),"message":"[mesaj silindi]" if d else m,"created_at":t,"deleted":bool(d)})
        return out
    finally:c.close()


def save_message(u,cc,m):
    c=conn()
    try:
        cur=c.execute("INSERT INTO messages(username,server_code,message,created_at,deleted) VALUES(?,?,?,?,0)",(u,cc,m,int(time.time())))
        c.commit(); return cur.lastrowid
    finally:c.close()


def delete_message(u,cc,mid):
    c=conn()
    try:
        row=c.execute("SELECT username FROM messages WHERE id=? AND server_code=?",(mid,cc)).fetchone()
        if not row or row[0]!=u:return False
        c.execute("UPDATE messages SET deleted=1,message='' WHERE id=?",(mid,)); c.commit(); return True
    finally:c.close()


def server_members(cc):
    c=conn()
    try: rows=c.execute("SELECT m.username,COALESCE(u.avatar,'') FROM members m LEFT JOIN users u ON u.username=m.username WHERE m.server_code=? ORDER BY m.username COLLATE NOCASE",(cc,)).fetchall()
    finally:c.close()
    online={clients[w].get("u") for w in clients if clients[w].get("kind")=="main" and clients[w].get("s")==cc and clients[w].get("u")}
    result=[]
    for u,a in rows:
        voice=any(clients.get(w,{}).get("kind")=="main" and clients.get(w,{}).get("s")==cc and clients.get(w,{}).get("u")==u and clients.get(w,{}).get("voice") for w in clients)
        muted=any(clients.get(w,{}).get("kind")=="main" and clients.get(w,{}).get("s")==cc and clients.get(w,{}).get("u")==u and clients.get(w,{}).get("muted") for w in clients)
        result.append({"username":u,"avatar":a or None,"online":u in online,"voice":voice,"muted":muted})
    return result


async def send(ws,payload):
    try: await ws.send(json.dumps(payload,ensure_ascii=False))
    except Exception: pass


async def broadcast_main(cc,payload,exclude=None):
    await asyncio.gather(*(send(w,payload) for w,s in list(clients.items()) if s.get("kind")=="main" and s.get("s")==cc and w!=exclude),return_exceptions=True)


async def broadcast_media(cc,kind,data,exclude=None):
    pool=media_rooms.get(cc,{}).get(kind,set())
    async def one(w):
        if w==exclude:return
        try: await w.send(data)
        except Exception: pass
    await asyncio.gather(*(one(w) for w in list(pool)),return_exceptions=True)


async def push_members(cc):
    if cc: await broadcast_main(cc,{"type":"members","items":server_members(cc)})


async def move_media(ws,code):
    s=clients.get(ws,{})
    kind=s.get("media_kind")
    if not kind:return
    old=s.get("s")
    if old: media_rooms.get(old,{}).get(kind,set()).discard(ws)
    s["s"]=code
    if code: media_rooms.setdefault(code,{"voice":set(),"screen":set()}).setdefault(kind,set()).add(ws)


async def handler(ws):
    clients[ws]={"kind":"unknown","u":None,"s":None,"voice":False,"muted":False,"screen":False,"token":None,"media_kind":None}
    try:
        async for incoming in ws:
            state=clients[ws]
            # First JSON packet may authenticate a media-only connection.
            if state["kind"]=="unknown":
                if isinstance(incoming,bytes): continue
                try:first=json.loads(incoming)
                except Exception: continue
                if first.get("action")=="media_auth":
                    token=str(first.get("token","")); main=sessions.get(token); kind=str(first.get("kind",""))
                    if main not in clients or kind not in ("voice","screen"):
                        await send(ws,{"type":"media_auth","ok":False}); await ws.close(); return
                    ms=clients[main]
                    state.update({"kind":"media","u":ms["u"],"s":ms["s"],"token":token,"media_kind":kind})
                    await move_media(ws,ms["s"])
                    await send(ws,{"type":"media_auth","ok":True,"kind":kind})
                    continue
                else:
                    state["kind"]="main"
                    # Process this packet below.

            if state["kind"]=="media":
                if isinstance(incoming,bytes) and state.get("s"):
                    prefix=b"V" if state["media_kind"]=="voice" else b"S"
                    if incoming.startswith(prefix): await broadcast_media(state["s"],state["media_kind"],incoming,ws)
                continue

            if isinstance(incoming,bytes): continue
            try:m=json.loads(incoming)
            except Exception:continue
            action=m.get("action")
            if action=="register":
                ok,msg=register(str(m.get("username","")).strip(),str(m.get("password",""))); await send(ws,{"type":"register","ok":ok,"message":msg}); continue
            if action=="login":
                u=str(m.get("username","")).strip(); ok,avatar=login(u,str(m.get("password","")))
                if ok:
                    token=secrets.token_urlsafe(32); sessions[token]=ws
                    ctmp=conn()
                    try:
                        row=ctmp.execute("SELECT avatar FROM users WHERE username=?",(u,)).fetchone()
                    finally:
                        ctmp.close()
                    state.update({"u":u,"token":token,"avatar":(row[0] if row else avatar)})
                    await send(ws,{"type":"login","ok":True,"username":u,"avatar":state.get("avatar"),"token":token})
                else: await send(ws,{"type":"login","ok":False,"message":"Kullanıcı adı veya şifre yanlış."})
                continue
            if not state["u"]:
                await send(ws,{"type":"error","message":"Önce giriş yap."}); continue

            if action=="servers": await send(ws,{"type":"servers","items":[{"name":n,"code":c} for n,c in user_servers(state["u"]) ]})
            elif action=="create":
                name=str(m.get("name","Yeni Sunucu")); cc=create_server(name,state["u"]); await send(ws,{"type":"created","name":name[:40],"code":cc})
            elif action=="join":
                cc=str(m.get("code","")).upper().strip(); name=join_server(state["u"],cc); await send(ws,{"type":"joined","ok":bool(name),"name":name,"code":cc})
            elif action=="enter":
                cc=str(m.get("code","")).upper().strip(); name=join_server(state["u"],cc)
                if not name: await send(ws,{"type":"error","message":"Sunucu bulunamadı."}); continue
                old=state["s"]
                if old: await broadcast_main(old,{"type":"presence","username":state["u"],"online":False},ws)
                state.update({"s":cc,"voice":False,"muted":False,"screen":False})
                # Move the two media sockets belonging to this session.
                for mw,ms in list(clients.items()):
                    if ms.get("kind")=="media" and ms.get("token")==state["token"]: await move_media(mw,cc)
                await send(ws,{"type":"entered","name":name,"code":cc,"history":history(cc),"members":server_members(cc)})
                await broadcast_main(cc,{"type":"presence","username":state["u"],"online":True},ws); await push_members(cc)
            elif action=="chat":
                cc=state["s"]; text=str(m.get("message","")).strip()
                if cc and text and len(text)<=2000:
                    mid=save_message(state["u"],cc,text)
                    av=state.get("avatar")
                    if not av:
                        ctmp=conn()
                        try:
                            row=ctmp.execute("SELECT avatar FROM users WHERE username=?",(state["u"],)).fetchone()
                            av=row[0] if row else None
                        finally:
                            ctmp.close()
                    await broadcast_main(cc,{"type":"chat","id":mid,"username":state["u"],"avatar":av,"message":text,"created_at":int(time.time()),"deleted":False})
            elif action=="delete_message":
                cc=state["s"]
                try:mid=int(m.get("id"))
                except:mid=0
                if cc and delete_message(state["u"],cc,mid): await broadcast_main(cc,{"type":"message_deleted","id":mid,"username":state["u"]})
            elif action=="voice":
                state["voice"]=bool(m.get("on")); await broadcast_main(state["s"],{"type":"voice","username":state["u"],"on":state["voice"]}); await push_members(state["s"])
            elif action=="mic_mute":
                state["muted"]=bool(m.get("muted")); await broadcast_main(state["s"],{"type":"mic_mute","username":state["u"],"muted":state["muted"]}); await push_members(state["s"])
            elif action=="screen":
                state["screen"]=bool(m.get("on")); await broadcast_main(state["s"],{"type":"screen","username":state["u"],"on":state["screen"]})
            elif action=="call_start":
                state["voice"]=True; state["muted"]=False
                await broadcast_main(state["s"],{"type":"incoming_call","from":state["u"]},ws); await broadcast_main(state["s"],{"type":"voice","username":state["u"],"on":True}); await push_members(state["s"])
            elif action=="call_answer":
                state["voice"]=True; state["muted"]=False
                await broadcast_main(state["s"],{"type":"call_state","user":state["u"],"state":"accepted"}); await broadcast_main(state["s"],{"type":"voice","username":state["u"],"on":True}); await push_members(state["s"])
            elif action=="call_reject": await broadcast_main(state["s"],{"type":"call_state","user":state["u"],"state":"rejected"})
            elif action=="call_end":
                state["voice"]=False; state["muted"]=False; await broadcast_main(state["s"],{"type":"call_state","user":state["u"],"state":"ended"}); await broadcast_main(state["s"],{"type":"voice","username":state["u"],"on":False}); await push_members(state["s"])
            elif action=="profile":
                avatar=str(m.get("avatar",""))
                if len(avatar)>MAX_AVATAR: await send(ws,{"type":"profile","ok":False,"message":"Profil fotoğrafı çok büyük."})
                else:
                    c=conn(); c.execute("UPDATE users SET avatar=? WHERE username=?",(avatar or None,state["u"])); c.commit(); c.close(); state["avatar"]=avatar or None
                    await send(ws,{"type":"profile","ok":True,"avatar":avatar or None}); await push_members(state["s"])
    except websockets.exceptions.ConnectionClosed: pass
    finally:
        state=clients.pop(ws,None)
        if not state:return
        if state.get("kind")=="media":
            code=state.get("s"); kind=state.get("media_kind")
            if code: media_rooms.get(code,{}).get(kind,set()).discard(ws)
            return
        token=state.get("token")
        if token and sessions.get(token)==ws: sessions.pop(token,None)
        code=state.get("s")
        if code:
            await broadcast_main(code,{"type":"presence","username":state["u"],"online":False}); await broadcast_main(code,{"type":"voice","username":state["u"],"on":False}); await push_members(code)
        # Close media sockets belonging to this session.
        for mw,ms in list(clients.items()):
            if ms.get("kind")=="media" and ms.get("token")==token:
                try: await mw.close()
                except Exception: pass


async def main():
    conn().close(); print(f"SIKKORD BACKEND -> 0.0.0.0:{PORT}")
    async with websockets.serve(handler,HOST,PORT,max_size=12_000_000,ping_interval=15,ping_timeout=15): await asyncio.Future()

if __name__=="__main__": asyncio.run(main())
