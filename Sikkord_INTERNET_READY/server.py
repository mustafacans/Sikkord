
import asyncio
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone

import psycopg
from aiohttp import WSMsgType, web
from psycopg.errors import UniqueViolation

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

MAX_AVATAR = 700_000
MAX_ATTACHMENT = 8_000_000  # base64 chars, roughly 6 MB raw
SESSION_DAYS = 90

clients = {}         # ws_adapter -> state
sessions = {}        # token -> ws_adapter (online session)
media_rooms = {}     # server_code -> {"voice": set(), "screen": set()}


class WSAdapter:
    def __init__(self, ws):
        self.ws = ws

    async def send(self, data):
        if isinstance(data, bytes):
            await self.ws.send_bytes(data)
        else:
            await self.ws.send_str(data)

    async def close(self):
        await self.ws.close()

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            msg = await self.ws.receive()
            if msg.type == WSMsgType.TEXT:
                return msg.data
            if msg.type == WSMsgType.BINARY:
                return msg.data
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
                raise StopAsyncIteration


def now():
    return int(time.time())


def conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL bulunamadı.")
    return psycopg.connect(DATABASE_URL, connect_timeout=10)


def init_db():
    c = conn()
    try:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id BIGSERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            avatar TEXT,
            created_at BIGINT NOT NULL,
            last_seen BIGINT NOT NULL DEFAULT 0
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS login_sessions(
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            expires_at BIGINT NOT NULL,
            created_at BIGINT NOT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS servers(
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            owner TEXT NOT NULL,
            created_at BIGINT NOT NULL
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS members(
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            server_code TEXT NOT NULL,
            UNIQUE(username, server_code)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS server_messages(
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            server_code TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            attachment_name TEXT,
            attachment_mime TEXT,
            attachment_data TEXT,
            reply_to BIGINT,
            created_at BIGINT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS friend_requests(
            id BIGSERIAL PRIMARY KEY,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at BIGINT NOT NULL,
            UNIQUE(sender, receiver)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS friendships(
            id BIGSERIAL PRIMARY KEY,
            user1 TEXT NOT NULL,
            user2 TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            UNIQUE(user1, user2)
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS dm_messages(
            id BIGSERIAL PRIMARY KEY,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            attachment_name TEXT,
            attachment_mime TEXT,
            attachment_data TEXT,
            reply_to BIGINT,
            created_at BIGINT NOT NULL,
            delivered_at BIGINT,
            read_at BIGINT,
            deleted INTEGER NOT NULL DEFAULT 0
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_server_msg ON server_messages(server_code,id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_dm_pair ON dm_messages(sender,receiver,id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_members_code ON members(server_code)")
        c.commit()
    finally:
        c.close()


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def verify_password(password, stored):
    try:
        salt_hex, dig_hex = stored.split(":")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return secrets.compare_digest(actual, bytes.fromhex(dig_hex))
    except Exception:
        return False


def new_invite_code():
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(chars) for _ in range(4)) for _ in range(2))


def issue_token(username):
    token = secrets.token_urlsafe(32)
    expires = now() + SESSION_DAYS * 86400
    c = conn()
    try:
        c.execute("DELETE FROM login_sessions WHERE expires_at < %s", (now(),))
        c.execute(
            "INSERT INTO login_sessions(token,username,expires_at,created_at) VALUES(%s,%s,%s,%s)",
            (token, username, expires, now()),
        )
        c.commit()
    finally:
        c.close()
    return token, expires


def validate_token(token):
    if not token:
        return None
    c = conn()
    try:
        row = c.execute(
            "SELECT username,expires_at FROM login_sessions WHERE token=%s",
            (token,),
        ).fetchone()
        if not row or row[1] < now():
            return None
        return row[0]
    finally:
        c.close()


def get_avatar(username):
    c = conn()
    try:
        row = c.execute("SELECT avatar FROM users WHERE username=%s", (username,)).fetchone()
        return row[0] if row else None
    finally:
        c.close()


def register(username, password):
    username = username.strip()
    if not 3 <= len(username) <= 24:
        return False, "Kullanıcı adı 3-24 karakter olmalı."
    if len(password) < 6:
        return False, "Şifre en az 6 karakter olmalı."
    c = conn()
    try:
        c.execute(
            "INSERT INTO users(username,password_hash,avatar,created_at,last_seen) VALUES(%s,%s,%s,%s,%s)",
            (username, hash_password(password), None, now(), now()),
        )
        c.commit()
        return True, "Kayıt başarılı."
    except UniqueViolation:
        c.rollback()
        return False, "Bu kullanıcı adı zaten kullanılıyor."
    finally:
        c.close()


def login(username, password):
    c = conn()
    try:
        row = c.execute("SELECT password_hash,avatar FROM users WHERE username=%s", (username,)).fetchone()
        if not row or not verify_password(password, row[0]):
            return None
        c.execute("UPDATE users SET last_seen=%s WHERE username=%s", (now(), username))
        c.commit()
        return {"username": username, "avatar": row[1]}
    finally:
        c.close()


def create_server(name, owner):
    name = name.strip()[:40] or "Yeni Sunucu"
    code = new_invite_code()
    c = conn()
    try:
        while c.execute("SELECT 1 FROM servers WHERE code=%s", (code,)).fetchone():
            code = new_invite_code()
        c.execute(
            "INSERT INTO servers(name,code,owner,created_at) VALUES(%s,%s,%s,%s)",
            (name, code, owner, now()),
        )
        c.execute(
            "INSERT INTO members(username,server_code) VALUES(%s,%s) ON CONFLICT(username,server_code) DO NOTHING",
            (owner, code),
        )
        c.commit()
        return code
    finally:
        c.close()


def join_server(username, code):
    code = code.upper().strip()
    c = conn()
    try:
        row = c.execute("SELECT name FROM servers WHERE code=%s", (code,)).fetchone()
        if not row:
            return None
        c.execute(
            "INSERT INTO members(username,server_code) VALUES(%s,%s) ON CONFLICT(username,server_code) DO NOTHING",
            (username, code),
        )
        c.commit()
        return row[0]
    finally:
        c.close()


def user_servers(username):
    c = conn()
    try:
        rows = c.execute("""
            SELECT s.name,s.code FROM servers s
            JOIN members m ON m.server_code=s.code
            WHERE m.username=%s ORDER BY s.id
        """, (username,)).fetchall()
        return [{"name": n, "code": code} for n, code in rows]
    finally:
        c.close()


def server_history(code):
    c = conn()
    try:
        rows = c.execute("""
            SELECT id,username,text,attachment_name,attachment_mime,attachment_data,reply_to,created_at,deleted
            FROM server_messages WHERE server_code=%s ORDER BY id DESC LIMIT 150
        """, (code,)).fetchall()
        rows.reverse()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "username": r[1], "avatar": get_avatar(r[1]),
                "text": "[mesaj silindi]" if r[8] else r[2],
                "attachment_name": None if r[8] else r[3],
                "attachment_mime": None if r[8] else r[4],
                "attachment_data": None if r[8] else r[5],
                "reply_to": r[6], "created_at": r[7], "deleted": bool(r[8]),
            })
        return out
    finally:
        c.close()


def save_server_message(username, code, payload):
    c = conn()
    try:
        row = c.execute("""
            INSERT INTO server_messages(username,server_code,text,attachment_name,attachment_mime,attachment_data,reply_to,created_at,deleted)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0) RETURNING id
        """, (
            username, code, payload.get("text",""), payload.get("attachment_name"),
            payload.get("attachment_mime"), payload.get("attachment_data"),
            payload.get("reply_to"), now()
        )).fetchone()
        c.commit()
        return row[0]
    finally:
        c.close()


def delete_server_message(username, code, mid):
    c = conn()
    try:
        row = c.execute("SELECT username FROM server_messages WHERE id=%s AND server_code=%s", (mid, code)).fetchone()
        if not row or row[0] != username:
            return False
        c.execute("UPDATE server_messages SET deleted=1,text='',attachment_name=NULL,attachment_mime=NULL,attachment_data=NULL WHERE id=%s", (mid,))
        c.commit()
        return True
    finally:
        c.close()


def friendship_key(a, b):
    return tuple(sorted((a, b), key=str.lower))


def is_friend(a, b):
    u1, u2 = friendship_key(a, b)
    c = conn()
    try:
        return bool(c.execute("SELECT 1 FROM friendships WHERE user1=%s AND user2=%s", (u1, u2)).fetchone())
    finally:
        c.close()


def friend_list(username):
    c = conn()
    try:
        rows = c.execute("""
            SELECT CASE WHEN user1=%s THEN user2 ELSE user1 END AS friend
            FROM friendships WHERE user1=%s OR user2=%s ORDER BY friend
        """, (username, username, username)).fetchall()
        out = []
        online_users = {s.get("u") for s in clients.values() if s.get("kind")=="main" and s.get("u")}
        for (friend,) in rows:
            ur = c.execute("SELECT avatar,last_seen FROM users WHERE username=%s", (friend,)).fetchone()
            out.append({
                "username": friend,
                "avatar": ur[0] if ur else None,
                "online": friend in online_users,
                "last_seen": ur[1] if ur else 0,
            })
        return out
    finally:
        c.close()


def pending_requests(username):
    c = conn()
    try:
        rows = c.execute("""
            SELECT id,sender,created_at FROM friend_requests
            WHERE receiver=%s AND status='pending' ORDER BY id DESC
        """, (username,)).fetchall()
        return [{"id": r[0], "sender": r[1], "created_at": r[2]} for r in rows]
    finally:
        c.close()


def send_friend_request(sender, receiver):
    receiver = receiver.strip()
    if sender.lower() == receiver.lower():
        return False, "Kendini arkadaş ekleyemezsin."
    c = conn()
    try:
        if not c.execute("SELECT 1 FROM users WHERE username=%s", (receiver,)).fetchone():
            return False, "Kullanıcı bulunamadı."
        if is_friend(sender, receiver):
            return False, "Zaten arkadaşsınız."
        if c.execute("SELECT 1 FROM friend_requests WHERE sender=%s AND receiver=%s AND status='pending'", (sender, receiver)).fetchone():
            return False, "İstek zaten gönderilmiş."
        # reverse pending request -> auto accept
        reverse = c.execute("SELECT id FROM friend_requests WHERE sender=%s AND receiver=%s AND status='pending'", (receiver, sender)).fetchone()
        if reverse:
            u1, u2 = friendship_key(sender, receiver)
            c.execute("UPDATE friend_requests SET status='accepted' WHERE id=%s", (reverse[0],))
            c.execute("INSERT INTO friendships(user1,user2,created_at) VALUES(%s,%s,%s) ON CONFLICT(user1,user2) DO NOTHING", (u1,u2,now()))
            c.commit()
            return True, "Karşılıklı istek bulundu; arkadaş oldunuz."
        c.execute("INSERT INTO friend_requests(sender,receiver,status,created_at) VALUES(%s,%s,'pending',%s)", (sender,receiver,now()))
        c.commit()
        return True, "Arkadaşlık isteği gönderildi."
    finally:
        c.close()


def respond_friend_request(username, req_id, accept):
    c = conn()
    try:
        row = c.execute("SELECT sender,receiver,status FROM friend_requests WHERE id=%s", (req_id,)).fetchone()
        if not row or row[1] != username or row[2] != "pending":
            return False
        if accept:
            u1, u2 = friendship_key(row[0], row[1])
            c.execute("UPDATE friend_requests SET status='accepted' WHERE id=%s", (req_id,))
            c.execute("INSERT INTO friendships(user1,user2,created_at) VALUES(%s,%s,%s) ON CONFLICT(user1,user2) DO NOTHING", (u1,u2,now()))
        else:
            c.execute("UPDATE friend_requests SET status='rejected' WHERE id=%s", (req_id,))
        c.commit()
        return True
    finally:
        c.close()


def dm_history(a, b):
    c = conn()
    try:
        rows = c.execute("""
            SELECT id,sender,receiver,text,attachment_name,attachment_mime,attachment_data,reply_to,created_at,delivered_at,read_at,deleted
            FROM dm_messages
            WHERE (sender=%s AND receiver=%s) OR (sender=%s AND receiver=%s)
            ORDER BY id DESC LIMIT 200
        """, (a,b,b,a)).fetchall()
        rows.reverse()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "sender": r[1], "receiver": r[2],
                "avatar": get_avatar(r[1]),
                "text": "[mesaj silindi]" if r[11] else r[3],
                "attachment_name": None if r[11] else r[4],
                "attachment_mime": None if r[11] else r[5],
                "attachment_data": None if r[11] else r[6],
                "reply_to": r[7], "created_at": r[8],
                "delivered_at": r[9], "read_at": r[10], "deleted": bool(r[11]),
            })
        return out
    finally:
        c.close()


def save_dm(sender, receiver, payload):
    c = conn()
    try:
        delivered = now() if any(s.get("u")==receiver and s.get("kind")=="main" for s in clients.values()) else None
        row = c.execute("""
            INSERT INTO dm_messages(sender,receiver,text,attachment_name,attachment_mime,attachment_data,reply_to,created_at,delivered_at,read_at,deleted)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,0) RETURNING id
        """, (
            sender, receiver, payload.get("text",""), payload.get("attachment_name"),
            payload.get("attachment_mime"), payload.get("attachment_data"),
            payload.get("reply_to"), now(), delivered
        )).fetchone()
        c.commit()
        return row[0], delivered
    finally:
        c.close()


def mark_dm_read(reader, peer):
    t = now()
    c = conn()
    try:
        rows = c.execute("""
            UPDATE dm_messages SET read_at=%s, delivered_at=COALESCE(delivered_at,%s)
            WHERE sender=%s AND receiver=%s AND read_at IS NULL
            RETURNING id
        """, (t,t,peer,reader)).fetchall()
        c.commit()
        return [r[0] for r in rows], t
    finally:
        c.close()


def delete_dm(username, mid):
    c = conn()
    try:
        row = c.execute("SELECT sender FROM dm_messages WHERE id=%s", (mid,)).fetchone()
        if not row or row[0] != username:
            return False
        c.execute("UPDATE dm_messages SET deleted=1,text='',attachment_name=NULL,attachment_mime=NULL,attachment_data=NULL WHERE id=%s", (mid,))
        c.commit()
        return True
    finally:
        c.close()


def server_members(code):
    c = conn()
    try:
        rows = c.execute("""
            SELECT m.username,u.avatar,u.last_seen
            FROM members m LEFT JOIN users u ON u.username=m.username
            WHERE m.server_code=%s ORDER BY LOWER(m.username)
        """, (code,)).fetchall()
    finally:
        c.close()
    out=[]
    for username,avatar,last_seen in rows:
        st = next((s for s in clients.values() if s.get("kind")=="main" and s.get("u")==username and s.get("s")==code), None)
        out.append({
            "username": username, "avatar": avatar,
            "online": bool(st), "last_seen": last_seen or 0,
            "voice": bool(st and st.get("voice")), "muted": bool(st and st.get("muted"))
        })
    return out


async def send(ws, payload):
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def send_to_user(username, payload):
    targets = [w for w,s in clients.items() if s.get("kind")=="main" and s.get("u")==username]
    await asyncio.gather(*(send(w,payload) for w in targets), return_exceptions=True)


async def broadcast_server(code, payload, exclude=None):
    targets = [w for w,s in clients.items() if s.get("kind")=="main" and s.get("s")==code and w != exclude]
    await asyncio.gather(*(send(w,payload) for w in targets), return_exceptions=True)


async def push_members(code):
    if code:
        await broadcast_server(code, {"type":"members","items":server_members(code)})


async def broadcast_media(code, kind, data, exclude=None):
    pool = media_rooms.get(code, {}).get(kind, set())
    async def one(w):
        if w == exclude: return
        try: await w.send(data)
        except Exception: pass
    await asyncio.gather(*(one(w) for w in list(pool)), return_exceptions=True)


async def move_media(ws, code):
    s = clients.get(ws,{})
    kind = s.get("media_kind")
    if not kind: return
    old=s.get("s")
    if old: media_rooms.get(old,{}).get(kind,set()).discard(ws)
    s["s"]=code
    if code:
        media_rooms.setdefault(code,{"voice":set(),"screen":set()}).setdefault(kind,set()).add(ws)


def valid_attachment(payload):
    data = payload.get("attachment_data")
    if data and len(data) > MAX_ATTACHMENT:
        return False
    return True


async def handler(ws):
    clients[ws] = {
        "kind":"unknown","u":None,"s":None,"voice":False,"muted":False,
        "screen":False,"token":None,"media_kind":None,"avatar":None,
    }
    try:
        async for incoming in ws:
            state = clients[ws]

            if state["kind"]=="unknown":
                if isinstance(incoming, bytes): continue
                try: first=json.loads(incoming)
                except Exception: continue

                if first.get("action")=="media_auth":
                    token=str(first.get("token",""))
                    main=sessions.get(token)
                    kind=str(first.get("kind",""))
                    if main not in clients or kind not in ("voice","screen"):
                        await send(ws,{"type":"media_auth","ok":False}); await ws.close(); return
                    ms=clients[main]
                    state.update({"kind":"media","u":ms["u"],"s":ms["s"],"token":token,"media_kind":kind})
                    await move_media(ws,ms["s"])
                    await send(ws,{"type":"media_auth","ok":True,"kind":kind})
                    continue
                state["kind"]="main"

            if state["kind"]=="media":
                if isinstance(incoming,bytes) and state.get("s"):
                    prefix=b"V" if state["media_kind"]=="voice" else b"S"
                    if incoming.startswith(prefix):
                        await broadcast_media(state["s"],state["media_kind"],incoming,ws)
                continue

            if isinstance(incoming,bytes): continue
            try: m=json.loads(incoming)
            except Exception: continue
            a=m.get("action")

            if a=="register":
                ok,msg=register(str(m.get("username","")).strip(),str(m.get("password","")))
                await send(ws,{"type":"register","ok":ok,"message":msg}); continue

            if a=="login":
                u=str(m.get("username","")).strip()
                user=login(u,str(m.get("password","")))
                if user:
                    token,expires=issue_token(u); sessions[token]=ws
                    state.update({"u":u,"token":token,"avatar":user["avatar"]})
                    await send(ws,{"type":"login","ok":True,"username":u,"avatar":user["avatar"],"token":token,"expires_at":expires})
                else:
                    await send(ws,{"type":"login","ok":False,"message":"Kullanıcı adı veya şifre yanlış."})
                continue

            if a=="token_login":
                token=str(m.get("token",""))
                u=validate_token(token)
                if u:
                    sessions[token]=ws
                    av=get_avatar(u)
                    state.update({"u":u,"token":token,"avatar":av})
                    c=conn()
                    try:
                        c.execute("UPDATE users SET last_seen=%s WHERE username=%s",(now(),u)); c.commit()
                    finally:c.close()
                    await send(ws,{"type":"login","ok":True,"username":u,"avatar":av,"token":token,"expires_at":now()+SESSION_DAYS*86400,"auto":True})
                else:
                    await send(ws,{"type":"token_login","ok":False})
                continue

            if not state["u"]:
                await send(ws,{"type":"error","message":"Önce giriş yap."}); continue

            if a=="servers":
                await send(ws,{"type":"servers","items":user_servers(state["u"])})

            elif a=="create":
                code=create_server(str(m.get("name","Yeni Sunucu")),state["u"])
                await send(ws,{"type":"created","name":str(m.get("name","Yeni Sunucu"))[:40],"code":code})

            elif a=="join":
                code=str(m.get("code","")).upper().strip()
                name=join_server(state["u"],code)
                await send(ws,{"type":"joined","ok":bool(name),"name":name,"code":code})

            elif a=="enter":
                code=str(m.get("code","")).upper().strip()
                name=join_server(state["u"],code)
                if not name:
                    await send(ws,{"type":"error","message":"Sunucu bulunamadı."}); continue
                old=state.get("s")
                state.update({"s":code,"voice":False,"muted":False,"screen":False})
                for mw,ms in list(clients.items()):
                    if ms.get("kind")=="media" and ms.get("token")==state["token"]:
                        await move_media(mw,code)
                await send(ws,{"type":"entered","name":name,"code":code,"history":server_history(code),"members":server_members(code)})
                if old and old!=code: await push_members(old)
                await push_members(code)

            elif a=="server_chat":
                if not state["s"] or not valid_attachment(m): continue
                payload={
                    "text":str(m.get("text",""))[:5000],
                    "attachment_name":m.get("attachment_name"),
                    "attachment_mime":m.get("attachment_mime"),
                    "attachment_data":m.get("attachment_data"),
                    "reply_to":m.get("reply_to")
                }
                if not payload["text"] and not payload["attachment_data"]: continue
                mid=save_server_message(state["u"],state["s"],payload)
                await broadcast_server(state["s"],{
                    "type":"server_chat","id":mid,"username":state["u"],"avatar":state.get("avatar"),
                    **payload,"created_at":now(),"deleted":False
                })

            elif a=="delete_server_message":
                try: mid=int(m.get("id",0))
                except: mid=0
                if delete_server_message(state["u"],state["s"],mid):
                    await broadcast_server(state["s"],{"type":"server_message_deleted","id":mid})

            elif a=="friends":
                await send(ws,{"type":"friends","items":friend_list(state["u"]),"requests":pending_requests(state["u"])})

            elif a=="friend_request":
                target=str(m.get("username","")).strip()
                ok,msg=send_friend_request(state["u"],target)
                await send(ws,{"type":"friend_request_result","ok":ok,"message":msg})
                if ok:
                    await send_to_user(target,{"type":"friend_request_notice","from":state["u"]})
                    await send(ws,{"type":"friends","items":friend_list(state["u"]),"requests":pending_requests(state["u"])})

            elif a=="friend_respond":
                try: rid=int(m.get("id",0))
                except: rid=0
                if respond_friend_request(state["u"],rid,bool(m.get("accept"))):
                    await send(ws,{"type":"friends","items":friend_list(state["u"]),"requests":pending_requests(state["u"])})

            elif a=="dm_open":
                peer=str(m.get("username","")).strip()
                if not is_friend(state["u"],peer):
                    await send(ws,{"type":"error","message":"Özel mesaj için arkadaş olmalısınız."}); continue
                ids,rt=mark_dm_read(state["u"],peer)
                await send(ws,{"type":"dm_history","peer":peer,"messages":dm_history(state["u"],peer)})
                if ids: await send_to_user(peer,{"type":"dm_read","ids":ids,"read_at":rt,"by":state["u"]})

            elif a=="dm_send":
                peer=str(m.get("to","")).strip()
                if not is_friend(state["u"],peer) or not valid_attachment(m): continue
                payload={
                    "text":str(m.get("text",""))[:5000],
                    "attachment_name":m.get("attachment_name"),
                    "attachment_mime":m.get("attachment_mime"),
                    "attachment_data":m.get("attachment_data"),
                    "reply_to":m.get("reply_to")
                }
                if not payload["text"] and not payload["attachment_data"]: continue
                mid,delivered=save_dm(state["u"],peer,payload)
                pkt={"type":"dm_message","id":mid,"sender":state["u"],"receiver":peer,"avatar":state.get("avatar"),**payload,
                     "created_at":now(),"delivered_at":delivered,"read_at":None,"deleted":False}
                await send(ws,pkt); await send_to_user(peer,pkt)

            elif a=="dm_read":
                peer=str(m.get("peer","")).strip()
                ids,rt=mark_dm_read(state["u"],peer)
                if ids: await send_to_user(peer,{"type":"dm_read","ids":ids,"read_at":rt,"by":state["u"]})

            elif a=="delete_dm":
                try: mid=int(m.get("id",0))
                except: mid=0
                if delete_dm(state["u"],mid):
                    await send(ws,{"type":"dm_deleted","id":mid})
                    # broadcast to all friend sessions; harmless if not relevant
                    for uname in [f["username"] for f in friend_list(state["u"])]:
                        await send_to_user(uname,{"type":"dm_deleted","id":mid})

            elif a=="profile":
                avatar=str(m.get("avatar",""))
                if len(avatar)>MAX_AVATAR:
                    await send(ws,{"type":"profile","ok":False,"message":"Profil fotoğrafı çok büyük."})
                else:
                    c=conn()
                    try:c.execute("UPDATE users SET avatar=%s WHERE username=%s",(avatar or None,state["u"])); c.commit()
                    finally:c.close()
                    state["avatar"]=avatar or None
                    await send(ws,{"type":"profile","ok":True,"avatar":state["avatar"]})
                    if state["s"]: await push_members(state["s"])

            elif a=="voice":
                state["voice"]=bool(m.get("on")); await push_members(state["s"])
                await broadcast_server(state["s"],{"type":"voice","username":state["u"],"on":state["voice"]})

            elif a=="mic_mute":
                state["muted"]=bool(m.get("muted")); await push_members(state["s"])

            elif a=="screen":
                state["screen"]=bool(m.get("on")); await broadcast_server(state["s"],{"type":"screen","username":state["u"],"on":state["screen"]})

            elif a=="call_start":
                state["voice"]=True
                await broadcast_server(state["s"],{"type":"incoming_call","from":state["u"]},ws)
                await push_members(state["s"])

            elif a=="call_answer":
                state["voice"]=True; await push_members(state["s"])

            elif a=="call_reject":
                await broadcast_server(state["s"],{"type":"call_state","user":state["u"],"state":"rejected"})

    except Exception as exc:
        print("HANDLER ERROR:", repr(exc))
    finally:
        state=clients.pop(ws,None)
        if not state: return
        if state.get("kind")=="media":
            code=state.get("s"); kind=state.get("media_kind")
            if code: media_rooms.get(code,{}).get(kind,set()).discard(ws)
            return
        token=state.get("token")
        if token and sessions.get(token)==ws: sessions.pop(token,None)
        if state.get("u"):
            c=conn()
            try:c.execute("UPDATE users SET last_seen=%s WHERE username=%s",(now(),state["u"])); c.commit()
            finally:c.close()
        if state.get("s"): await push_members(state["s"])
        if state.get("u"):
            # refresh friend online states
            for f in friend_list(state["u"]):
                await send_to_user(f["username"],{"type":"friend_presence","username":state["u"],"online":False,"last_seen":now()})


async def http_or_ws(request):
    if request.headers.get("Upgrade","").lower()!="websocket":
        return web.Response(text="SIKKORD backend aktif.\n",status=200)
    ws=web.WebSocketResponse(heartbeat=15,max_msg_size=12_000_000,compress=False)
    await ws.prepare(request)
    await handler(WSAdapter(ws))
    return ws


async def on_startup(app):
    await asyncio.to_thread(init_db)
    print("PostgreSQL bağlantısı başarılı.")
    print(f"SIKKORD PRO V4 -> {HOST}:{PORT}")


def make_app():
    app=web.Application(client_max_size=12_000_000)
    app.on_startup.append(on_startup)
    app.router.add_route("*","/{tail:.*}",http_or_ws)
    return app


if __name__=="__main__":
    web.run_app(make_app(),host=HOST,port=PORT,access_log=None)
