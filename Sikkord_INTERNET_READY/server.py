
import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone

import psycopg
from aiohttp import WSMsgType, web
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

MAX_AVATAR = 700_000
MAX_ATTACHMENT = 8_000_000  # base64 chars, roughly 6 MB raw
SESSION_DAYS = 90

clients = {}         # ws_adapter -> state
sessions = {}        # token -> ws_adapter (online session)
media_rooms = {}     # media_room -> {"voice": set(), "screen": set()}
DB_POOL = None


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


class PooledConnection:
    def __init__(self):
        if DB_POOL is None:
            raise RuntimeError("Veritabanı havuzu hazır değil.")
        self._conn = DB_POOL.getconn(timeout=10)
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if not self._closed:
            self._closed = True
            DB_POOL.putconn(self._conn)


def init_pool():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL bulunamadı.")
    if DB_POOL is None:
        DB_POOL = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=1,
            max_size=8,
            timeout=10,
            kwargs={"autocommit": False},
            open=True,
        )


def conn():
    return PooledConnection()


async def adb(fn, *args):
    # PostgreSQL calls never block the websocket/media event loop.
    return await asyncio.to_thread(fn, *args)


def init_db():
    c = conn()
    try:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id BIGSERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            avatar TEXT,
            display_color TEXT NOT NULL DEFAULT '#f4f5f7',
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
        CREATE TABLE IF NOT EXISTS server_message_reads(
            message_id BIGINT NOT NULL,
            username TEXT NOT NULL,
            read_at BIGINT NOT NULL,
            PRIMARY KEY(message_id, username)
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
        # --- Schema migrations for users upgrading from older SIKKORD versions ---
        # CREATE TABLE IF NOT EXISTS does not add new columns to an existing table.
        # These ALTER statements make old PostgreSQL databases compatible with V4.
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar TEXT")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_color TEXT NOT NULL DEFAULT '#f4f5f7'")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at BIGINT")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen BIGINT NOT NULL DEFAULT 0")
        c.execute("UPDATE users SET created_at = COALESCE(created_at, %s)", (now(),))
        c.execute("UPDATE users SET last_seen = COALESCE(last_seen, 0)")

        c.execute("CREATE INDEX IF NOT EXISTS idx_server_msg ON server_messages(server_code,id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_server_reads_msg ON server_message_reads(message_id)")
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


def get_profile(username):
    c=conn()
    try:
        row=c.execute("SELECT username,avatar,display_color,last_seen FROM users WHERE username=%s",(username,)).fetchone()
        if not row:return None
        return {"username":row[0],"avatar":row[1],"color":row[2] or "#f4f5f7","last_seen":row[3] or 0}
    finally:c.close()


def get_avatar(username):
    p=get_profile(username)
    return p.get("avatar") if p else None


def update_profile(username, color=None):
    if color is not None:
        color=str(color).strip()
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}",color):
            return False,"Geçersiz renk."
    c=conn()
    try:
        if color is not None:
            c.execute("UPDATE users SET display_color=%s WHERE username=%s",(color,username))
        c.commit();return True,"Profil güncellendi."
    finally:c.close()


def rename_user(old_username,new_username):
    new_username=str(new_username).strip()
    if not 3<=len(new_username)<=24:
        return False,"Kullanıcı adı 3-24 karakter olmalı.",old_username
    if not re.fullmatch(r"[0-9A-Za-z_ÇĞİÖŞÜçğıöşü .-]+",new_username):
        return False,"Kullanıcı adında geçersiz karakter var.",old_username
    if old_username==new_username:
        return True,"İsim değişmedi.",old_username
    c=conn()
    try:
        exists=c.execute("SELECT username FROM users WHERE LOWER(username)=LOWER(%s) AND username<>%s",(new_username,old_username)).fetchone()
        if exists:return False,"Bu kullanıcı adı zaten kullanılıyor.",old_username
        # Username is referenced by older Sikkord tables as text; update all in one transaction.
        c.execute("UPDATE users SET username=%s WHERE username=%s",(new_username,old_username))
        c.execute("UPDATE login_sessions SET username=%s WHERE username=%s",(new_username,old_username))
        c.execute("UPDATE servers SET owner=%s WHERE owner=%s",(new_username,old_username))
        c.execute("UPDATE members SET username=%s WHERE username=%s",(new_username,old_username))
        c.execute("UPDATE server_messages SET username=%s WHERE username=%s",(new_username,old_username))
        c.execute("UPDATE server_message_reads SET username=%s WHERE username=%s",(new_username,old_username))
        c.execute("UPDATE friend_requests SET sender=%s WHERE sender=%s",(new_username,old_username))
        c.execute("UPDATE friend_requests SET receiver=%s WHERE receiver=%s",(new_username,old_username))
        c.execute("UPDATE friendships SET user1=%s WHERE user1=%s",(new_username,old_username))
        c.execute("UPDATE friendships SET user2=%s WHERE user2=%s",(new_username,old_username))
        c.execute("UPDATE dm_messages SET sender=%s WHERE sender=%s",(new_username,old_username))
        c.execute("UPDATE dm_messages SET receiver=%s WHERE receiver=%s",(new_username,old_username))
        c.commit();return True,"Kullanıcı adı değiştirildi.",new_username
    except UniqueViolation:
        c.rollback();return False,"Bu kullanıcı adı kullanılamıyor.",old_username
    except Exception:
        c.rollback();raise
    finally:c.close()


def register(username, password):
    username = username.strip()
    if not 3 <= len(username) <= 24:
        return False, "Kullanıcı adı 3-24 karakter olmalı."
    if len(password) < 6:
        return False, "Şifre en az 6 karakter olmalı."
    c = conn()
    try:
        c.execute(
            "INSERT INTO users(username,password_hash,avatar,display_color,created_at,last_seen) VALUES(%s,%s,%s,%s,%s,%s)",
            (username, hash_password(password), None, "#f4f5f7", now(), now()),
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
        row = c.execute("SELECT password_hash,avatar,display_color FROM users WHERE username=%s", (username,)).fetchone()
        if not row or not verify_password(password, row[0]):
            return None
        c.execute("UPDATE users SET last_seen=%s WHERE username=%s", (now(), username))
        c.commit()
        return {"username": username, "avatar": row[1], "color": row[2] or "#f4f5f7"}
    finally:
        c.close()


def touch_user(username):
    c=conn()
    try:
        c.execute("UPDATE users SET last_seen=%s WHERE username=%s",(now(),username))
        c.commit()
    finally:c.close()


def update_avatar(username, avatar):
    c=conn()
    try:
        c.execute("UPDATE users SET avatar=%s WHERE username=%s",(avatar or None,username))
        c.commit()
    finally:c.close()


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
            SELECT s.name,s.code,s.owner FROM servers s
            JOIN members m ON m.server_code=s.code
            WHERE m.username=%s ORDER BY s.id
        """, (username,)).fetchall()
        return [{"name": n, "code": code, "owner": owner} for n, code, owner in rows]
    finally:
        c.close()


def server_history(code, limit=45):
    """
    Fast history payload:
    - avatars aren't repeated for every message (members packet already has them)
    - attachment bytes are fetched only when the user opens the attachment
    """
    c = conn()
    try:
        rows = c.execute("""
            SELECT sm.id,sm.username,sm.text,sm.attachment_name,sm.attachment_mime,
                   sm.reply_to,sm.created_at,sm.deleted
            FROM server_messages sm
            WHERE sm.server_code=%s
            ORDER BY sm.id DESC LIMIT %s
        """, (code, limit)).fetchall()
        rows.reverse()
        return [{
            "id": r[0], "username": r[1], "avatar": None,
            "text": "[mesaj silindi]" if r[7] else r[2],
            "message": "[mesaj silindi]" if r[7] else r[2],
            "attachment_name": None if r[7] else r[3],
            "attachment_mime": None if r[7] else r[4],
            "attachment_data": None,
            "reply_to": r[5], "created_at": r[6], "deleted": bool(r[7]),
        } for r in rows]
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



def mark_server_read(username, code):
    """Only mark the latest 100 messages; avoids an ever-growing UPDATE/RETURNING payload."""
    c = conn()
    try:
        rows = c.execute("""
            WITH recent AS (
                SELECT id
                FROM server_messages
                WHERE server_code=%s AND deleted=0
                ORDER BY id DESC
                LIMIT 100
            )
            INSERT INTO server_message_reads(message_id,username,read_at)
            SELECT id,%s,%s FROM recent
            ON CONFLICT(message_id,username) DO UPDATE SET read_at=EXCLUDED.read_at
            RETURNING message_id
        """, (code, username, now())).fetchall()
        c.commit()
        return [r[0] for r in rows]
    finally:
        c.close()


def server_message_info(code, message_id):
    c = conn()
    try:
        msg = c.execute("""
            SELECT username,created_at FROM server_messages
            WHERE id=%s AND server_code=%s
        """, (message_id, code)).fetchone()
        if not msg:
            return None
        rows = c.execute("""
            SELECT r.username,r.read_at
            FROM server_message_reads r
            JOIN members m ON m.username=r.username AND m.server_code=%s
            WHERE r.message_id=%s
            ORDER BY r.read_at
        """, (code, message_id)).fetchall()
        return {
            "id": message_id,
            "author": msg[0],
            "created_at": msg[1],
            "read_by": [{"username":u,"read_at":ts} for u,ts in rows],
        }
    finally:
        c.close()


def leave_server(username, code):
    c = conn()
    try:
        row = c.execute("SELECT owner FROM servers WHERE code=%s", (code,)).fetchone()
        if not row:
            return False, "Sunucu bulunamadı."
        if row[0] == username:
            return False, "Sunucu sahibi ayrılamaz. Önce sunucuyu silmelisin."
        c.execute("DELETE FROM members WHERE username=%s AND server_code=%s", (username,code))
        c.commit()
        return True, "Sunucudan ayrıldın."
    finally:
        c.close()


def delete_server_db(username, code):
    c = conn()
    try:
        row = c.execute("SELECT owner FROM servers WHERE code=%s", (code,)).fetchone()
        if not row:
            return False, "Sunucu bulunamadı."
        if row[0] != username:
            return False, "Sadece sunucu sahibi sunucuyu silebilir."
        c.execute("""
            DELETE FROM server_message_reads
            WHERE message_id IN (SELECT id FROM server_messages WHERE server_code=%s)
        """, (code,))
        c.execute("DELETE FROM server_messages WHERE server_code=%s", (code,))
        c.execute("DELETE FROM members WHERE server_code=%s", (code,))
        c.execute("DELETE FROM servers WHERE code=%s", (code,))
        c.commit()
        return True, "Sunucu silindi."
    finally:
        c.close()


def dm_room(a, b):
    x, y = sorted((a,b), key=str.lower)
    return "dm:" + x.lower() + ":" + y.lower()


def get_server_attachment(username, code, message_id):
    c = conn()
    try:
        member = c.execute(
            "SELECT 1 FROM members WHERE username=%s AND server_code=%s",
            (username, code),
        ).fetchone()
        if not member:
            return None
        row = c.execute("""
            SELECT attachment_name,attachment_mime,attachment_data
            FROM server_messages
            WHERE id=%s AND server_code=%s AND deleted=0
        """, (message_id, code)).fetchone()
        if not row or not row[2]:
            return None
        return {"name":row[0],"mime":row[1],"data":row[2]}
    finally:
        c.close()


def get_dm_attachment(username, message_id):
    c = conn()
    try:
        row = c.execute("""
            SELECT sender,receiver,attachment_name,attachment_mime,attachment_data
            FROM dm_messages
            WHERE id=%s AND deleted=0
        """, (message_id,)).fetchone()
        if not row or username not in (row[0],row[1]) or not row[4]:
            return None
        return {"name":row[2],"mime":row[3],"data":row[4]}
    finally:
        c.close()



def friendship_key(a, b):
    return tuple(sorted((a, b), key=str.lower))


def is_friend(a, b):
    c = conn()
    try:
        return bool(c.execute(
            "SELECT 1 FROM friendships WHERE (user1=%s AND user2=%s) OR (user1=%s AND user2=%s)",
            (a,b,b,a),
        ).fetchone())
    finally:
        c.close()


def friend_list(username):
    c = conn()
    try:
        rows = c.execute("""
            SELECT CASE WHEN f.user1=%s THEN f.user2 ELSE f.user1 END AS friend,
                   u.avatar,u.last_seen,u.display_color
            FROM friendships f
            JOIN users u ON u.username=CASE WHEN f.user1=%s THEN f.user2 ELSE f.user1 END
            WHERE f.user1=%s OR f.user2=%s
            ORDER BY LOWER(CASE WHEN f.user1=%s THEN f.user2 ELSE f.user1 END)
        """, (username,username,username,username,username)).fetchall()
        online_users = {s.get("u") for s in list(clients.values()) if s.get("kind")=="main" and s.get("u")}
        return [{"username":u,"avatar":av,"online":u in online_users,"last_seen":seen or 0,"color":color or "#f4f5f7"}
                for u,av,seen,color in rows]
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


def dm_history(a, b, limit=55):
    """DM history without repeating large avatar / attachment base64 payloads."""
    c = conn()
    try:
        rows = c.execute("""
            SELECT dm.id,dm.sender,dm.receiver,dm.text,dm.attachment_name,dm.attachment_mime,
                   dm.reply_to,dm.created_at,dm.delivered_at,dm.read_at,dm.deleted
            FROM dm_messages dm
            WHERE (dm.sender=%s AND dm.receiver=%s) OR (dm.sender=%s AND dm.receiver=%s)
            ORDER BY dm.id DESC LIMIT %s
        """, (a,b,b,a,limit)).fetchall()
        rows.reverse()
        return [{
            "id":r[0],"sender":r[1],"receiver":r[2],"avatar":None,
            "text":"[mesaj silindi]" if r[10] else r[3],
            "attachment_name":None if r[10] else r[4],
            "attachment_mime":None if r[10] else r[5],
            "attachment_data":None,
            "reply_to":r[6],"created_at":r[7],"delivered_at":r[8],"read_at":r[9],
            "deleted":bool(r[10]),
        } for r in rows]
    finally:
        c.close()


def save_dm(sender, receiver, payload):
    c = conn()
    try:
        delivered = now() if any(s.get("u")==receiver and s.get("kind")=="main" for s in list(clients.values())) else None
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
            SELECT m.username,u.avatar,u.last_seen,u.display_color
            FROM members m LEFT JOIN users u ON u.username=m.username
            WHERE m.server_code=%s ORDER BY LOWER(m.username)
        """, (code,)).fetchall()
    finally:
        c.close()
    out=[]
    for username,avatar,last_seen,color in rows:
        st = next((s for s in list(clients.values()) if s.get("kind")=="main" and s.get("u")==username and s.get("s")==code), None)
        out.append({
            "username": username, "avatar": avatar, "color": color or "#f4f5f7",
            "online": bool(st), "last_seen": last_seen or 0,
            "voice": bool(st and st.get("voice")), "muted": bool(st and st.get("muted")),
            "screen": bool(st and st.get("screen"))
        })
    return out


async def send(ws, payload):
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False, separators=(",",":")))
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
        items = await adb(server_members, code)
        await broadcast_server(code, {"type":"members","items":items})


async def broadcast_media(code, kind, data, exclude=None):
    pool = media_rooms.get(code, {}).get(kind, set())
    async def one(w):
        if w == exclude: return
        try: await w.send(data)
        except Exception: pass
    await asyncio.gather(*(one(w) for w in list(pool)), return_exceptions=True)


async def move_media(ws, room):
    s = clients.get(ws,{})
    kind = s.get("media_kind")
    if not kind: return
    old=s.get("room")
    if old: media_rooms.get(old,{}).get(kind,set()).discard(ws)
    s["room"]=room
    if room:
        media_rooms.setdefault(room,{"voice":set(),"screen":set()}).setdefault(kind,set()).add(ws)


async def move_user_media(token, room, kind=None):
    for mw, ms in list(clients.items()):
        if ms.get("kind")=="media" and ms.get("token")==token and (kind is None or ms.get("media_kind")==kind):
            await move_media(mw, room)


def valid_attachment(payload):
    data = payload.get("attachment_data")
    if data and len(data) > MAX_ATTACHMENT:
        return False
    return True


async def handler(ws):
    clients[ws] = {
        "kind":"unknown","u":None,"s":None,"voice":False,"muted":False,
        "screen":False,"token":None,"media_kind":None,"avatar":None,"color":"#f4f5f7",
        "media_room":None,"screen_room":None,"call_peer":None,"room":None,
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
                    if kind=="voice" and (int(first.get("proto",0))!=2 or first.get("codec")!="pcm16"):
                        await send(ws,{"type":"media_auth","ok":False,"message":"V9 PCM16 gerekli"}); await ws.close(); return
                    if kind=="screen" and int(first.get("proto",0))!=2:
                        await send(ws,{"type":"media_auth","ok":False,"message":"V9 ekran protokolü gerekli"}); await ws.close(); return
                    ms=clients[main]
                    room=ms.get("media_room") if kind=="voice" else ms.get("screen_room")
                    state.update({"kind":"media","u":ms["u"],"s":ms["s"],"token":token,"media_kind":kind,"room":room})
                    await move_media(ws,room)
                    await send(ws,{"type":"media_auth","ok":True,"kind":kind,"proto":2,"codec":"pcm16" if kind=="voice" else "jpeg"})
                    continue
                state["kind"]="main"

            if state["kind"]=="media":
                if isinstance(incoming,bytes) and state.get("room"):
                    if state["media_kind"]=="voice" and incoming.startswith(b"P") and len(incoming)==1+640:
                        name=state.get("u","").encode("utf-8")[:63]
                        framed=b"P"+bytes([len(name)])+name+incoming[1:]
                        await broadcast_media(state["room"],"voice",framed,ws)
                    elif state["media_kind"]=="screen" and incoming.startswith(b"S"):
                        name=state.get("u","").encode("utf-8")[:63]
                        framed=b"S"+bytes([len(name)])+name+incoming[1:]
                        await broadcast_media(state["room"],"screen",framed,ws)
                continue

            if isinstance(incoming,bytes): continue
            try: m=json.loads(incoming)
            except Exception: continue
            a=m.get("action")

            if a=="register":
                ok,msg=await adb(register,str(m.get("username","")).strip(),str(m.get("password","")))
                await send(ws,{"type":"register","ok":ok,"message":msg}); continue

            if a=="login":
                u=str(m.get("username","")).strip()
                print(f"LOGIN ATTEMPT: {u}")
                user=await adb(login,u,str(m.get("password","")))
                if user:
                    token,expires=await adb(issue_token,u); sessions[token]=ws
                    state.update({"u":u,"token":token,"avatar":user["avatar"],"color":user.get("color","#f4f5f7")})
                    await send(ws,{"type":"login","ok":True,"username":u,"avatar":user["avatar"],"color":user.get("color","#f4f5f7"),"token":token,"expires_at":expires})
                    print(f"LOGIN OK: {u}")
                else:
                    await send(ws,{"type":"login","ok":False,"message":"Kullanıcı adı veya şifre yanlış."})
                continue

            if a=="token_login":
                token=str(m.get("token",""))
                u=await adb(validate_token,token)
                if u:
                    sessions[token]=ws
                    prof=await adb(get_profile,u)
                    av=prof.get("avatar") if prof else None
                    color=prof.get("color","#f4f5f7") if prof else "#f4f5f7"
                    state.update({"u":u,"token":token,"avatar":av,"color":color})
                    await adb(touch_user,u)
                    await send(ws,{"type":"login","ok":True,"username":u,"avatar":av,"color":color,"token":token,"expires_at":now()+SESSION_DAYS*86400,"auto":True})
                else:
                    await send(ws,{"type":"token_login","ok":False})
                continue

            if not state["u"]:
                await send(ws,{"type":"error","message":"Önce giriş yap."}); continue

            if a=="servers":
                await send(ws,{"type":"servers","items":await adb(user_servers,state["u"])})

            elif a=="create":
                code=await adb(create_server,str(m.get("name","Yeni Sunucu")),state["u"])
                await send(ws,{"type":"created","name":str(m.get("name","Yeni Sunucu"))[:40],"code":code})

            elif a=="join":
                code=str(m.get("code","")).upper().strip()
                name=await adb(join_server,state["u"],code)
                await send(ws,{"type":"joined","ok":bool(name),"name":name,"code":code})

            elif a=="enter":
                code=str(m.get("code","")).upper().strip()
                name=await adb(join_server,state["u"],code)
                if not name:
                    await send(ws,{"type":"error","message":"Sunucu bulunamadı."}); continue
                old=state.get("s")
                state["s"]=code
                state["screen_room"]="srv:"+code
                await move_user_media(state["token"],state["screen_room"],"screen")
                reads=await adb(mark_server_read,state["u"],code)
                history, members = await asyncio.gather(adb(server_history,code), adb(server_members,code))
                await send(ws,{"type":"entered","name":name,"code":code,"history":history,"members":members})
                if old and old!=code: await push_members(old)
                await push_members(code)
                if reads:
                    await broadcast_server(code,{"type":"server_read_update","username":state["u"],"ids":reads},ws)

            elif a in ("server_chat","chat"):
                if not state["s"] or not valid_attachment(m): continue
                payload={
                    "text":str(m.get("text",m.get("message","")))[:5000],
                    "attachment_name":m.get("attachment_name"),
                    "attachment_mime":m.get("attachment_mime"),
                    "attachment_data":m.get("attachment_data"),
                    "reply_to":m.get("reply_to")
                }
                if not payload["text"] and not payload["attachment_data"]: continue
                mid=await adb(save_server_message,state["u"],state["s"],payload)
                await adb(mark_server_read,state["u"],state["s"])
                await broadcast_server(state["s"],{
                    "type":"server_chat","id":mid,"username":state["u"],"avatar":state.get("avatar"),"color":state.get("color","#f4f5f7"),
                    "message":payload.get("text",""), **payload,"created_at":now(),"deleted":False
                })

            elif a=="delete_server_message":
                try: mid=int(m.get("id",0))
                except: mid=0
                if await adb(delete_server_message,state["u"],state["s"],mid):
                    await broadcast_server(state["s"],{"type":"server_message_deleted","id":mid})

            elif a=="friends":
                friends, reqs = await asyncio.gather(adb(friend_list,state["u"]), adb(pending_requests,state["u"]))
                await send(ws,{"type":"friends","items":friends,"requests":reqs})

            elif a=="friend_request":
                target=str(m.get("username","")).strip()
                ok,msg=await adb(send_friend_request,state["u"],target)
                await send(ws,{"type":"friend_request_result","ok":ok,"message":msg})
                if ok:
                    await send_to_user(target,{"type":"friend_request_notice","from":state["u"]})
                    friends, reqs = await asyncio.gather(adb(friend_list,state["u"]), adb(pending_requests,state["u"]))
                await send(ws,{"type":"friends","items":friends,"requests":reqs})

            elif a=="friend_respond":
                try: rid=int(m.get("id",0))
                except: rid=0
                if await adb(respond_friend_request,state["u"],rid,bool(m.get("accept"))):
                    friends, reqs = await asyncio.gather(adb(friend_list,state["u"]), adb(pending_requests,state["u"]))
                await send(ws,{"type":"friends","items":friends,"requests":reqs})

            elif a=="dm_open":
                peer=str(m.get("username","")).strip()
                if not await adb(is_friend,state["u"],peer):
                    await send(ws,{"type":"error","message":"Özel mesaj için arkadaş olmalısınız."}); continue
                ids,rt=await adb(mark_dm_read,state["u"],peer)
                await send(ws,{"type":"dm_history","peer":peer,"messages":await adb(dm_history,state["u"],peer)})
                if ids: await send_to_user(peer,{"type":"dm_read","ids":ids,"read_at":rt,"by":state["u"]})

            elif a=="dm_send":
                peer=str(m.get("to","")).strip()
                if not await adb(is_friend,state["u"],peer) or not valid_attachment(m): continue
                payload={
                    "text":str(m.get("text",""))[:5000],
                    "attachment_name":m.get("attachment_name"),
                    "attachment_mime":m.get("attachment_mime"),
                    "attachment_data":m.get("attachment_data"),
                    "reply_to":m.get("reply_to")
                }
                if not payload["text"] and not payload["attachment_data"]: continue
                mid,delivered=await adb(save_dm,state["u"],peer,payload)
                pkt={"type":"dm_message","id":mid,"sender":state["u"],"receiver":peer,"avatar":state.get("avatar"),"color":state.get("color","#f4f5f7"),**payload,
                     "created_at":now(),"delivered_at":delivered,"read_at":None,"deleted":False}
                await send(ws,pkt); await send_to_user(peer,pkt)

            elif a=="dm_read":
                peer=str(m.get("peer","")).strip()
                ids,rt=await adb(mark_dm_read,state["u"],peer)
                if ids: await send_to_user(peer,{"type":"dm_read","ids":ids,"read_at":rt,"by":state["u"]})

            elif a=="delete_dm":
                try: mid=int(m.get("id",0))
                except: mid=0
                if await adb(delete_dm,state["u"],mid):
                    await send(ws,{"type":"dm_deleted","id":mid})
                    # broadcast to all friend sessions; harmless if not relevant
                    for uname in [f["username"] for f in await adb(friend_list,state["u"])]:
                        await send_to_user(uname,{"type":"dm_deleted","id":mid})

            elif a=="attachment_get":
                try: mid=int(m.get("id",0))
                except: mid=0
                scope=str(m.get("scope","server"))
                if scope=="dm":
                    item=await adb(get_dm_attachment,state["u"],mid)
                else:
                    item=await adb(get_server_attachment,state["u"],state.get("s"),mid) if state.get("s") else None
                await send(ws,{
                    "type":"attachment_data","id":mid,"scope":scope,
                    "name":item.get("name") if item else None,
                    "mime":item.get("mime") if item else None,
                    "data":item.get("data") if item else None,
                })

            elif a=="profile_update":
                new_name=str(m.get("username",state["u"])).strip()
                color=m.get("color")
                old_name=state["u"]
                if new_name!=old_name:
                    ok,msg,actual=await adb(rename_user,old_name,new_name)
                    if not ok:
                        await send(ws,{"type":"profile_update","ok":False,"message":msg});continue
                    state["u"]=actual
                    # every online connection belonging to the renamed account follows the new name
                    for ow,os_ in list(clients.items()):
                        if os_.get("kind")=="main" and os_.get("u")==old_name:
                            os_["u"]=actual
                        if os_.get("kind")=="media" and os_.get("u")==old_name:
                            os_["u"]=actual
                ok,msg=await adb(update_profile,state["u"],color)
                prof=await adb(get_profile,state["u"])
                state["color"]=prof.get("color","#f4f5f7") if prof else "#f4f5f7"
                await send(ws,{"type":"profile_update","ok":ok,"message":msg,"username":state["u"],"color":state["color"]})
                # Tell connected clients so cached friend/member/message name styling can refresh.
                await asyncio.gather(*(send(ow,{"type":"profile_changed","old_username":old_name,"username":state["u"],"color":state["color"],"avatar":state.get("avatar")}) for ow,os_ in list(clients.items()) if os_.get("kind")=="main"),return_exceptions=True)
                if state.get("s"):await push_members(state["s"])

            elif a=="profile":
                avatar=str(m.get("avatar",""))
                if len(avatar)>MAX_AVATAR:
                    await send(ws,{"type":"profile","ok":False,"message":"Profil fotoğrafı çok büyük."})
                else:
                    await adb(update_avatar,state["u"],avatar)
                    state["avatar"]=avatar or None
                    await send(ws,{"type":"profile","ok":True,"avatar":state["avatar"]})
                    if state["s"]: await push_members(state["s"])

            elif a=="server_read":
                if state.get("s"):
                    ids=await adb(mark_server_read,state["u"],state["s"])
                    if ids:
                        await broadcast_server(state["s"],{"type":"server_read_update","username":state["u"],"ids":ids},ws)

            elif a=="message_info":
                try: mid=int(m.get("id",0))
                except: mid=0
                info=await adb(server_message_info,state.get("s"),mid) if state.get("s") else None
                await send(ws,{"type":"message_info","info":info})

            elif a=="leave_server":
                code=str(m.get("code",state.get("s") or "")).upper()
                ok,msg=await adb(leave_server,state["u"],code)
                if ok and state.get("s")==code: state["s"]=None
                await send(ws,{"type":"server_manage","ok":ok,"message":msg,"action":"leave","code":code})
                await send(ws,{"type":"servers","items":await adb(user_servers,state["u"])})

            elif a=="delete_server":
                code=str(m.get("code",state.get("s") or "")).upper()
                ok,msg=await adb(delete_server_db,state["u"],code)
                if ok and state.get("s")==code: state["s"]=None
                await send(ws,{"type":"server_manage","ok":ok,"message":msg,"action":"delete","code":code})
                await send(ws,{"type":"servers","items":await adb(user_servers,state["u"])})

            elif a=="voice":
                state["voice"]=bool(m.get("on"))
                if state["voice"] and state.get("s"):
                    state["call_peer"]=None
                    state["media_room"]="srv:"+state["s"]
                    await move_user_media(state["token"],state["media_room"],"voice")
                elif not state["voice"] and not state.get("call_peer"):
                    state["media_room"]=None
                    await move_user_media(state["token"],None,"voice")
                await push_members(state["s"])
                await broadcast_server(state["s"],{"type":"voice","username":state["u"],"on":state["voice"]})

            elif a=="mic_mute":
                state["muted"]=bool(m.get("muted")); await push_members(state["s"])

            elif a=="screen":
                state["screen"]=bool(m.get("on"))
                if state.get("s"):
                    state["screen_room"]="srv:"+state["s"]
                    await move_user_media(state["token"],state["screen_room"],"screen")
                await broadcast_server(state["s"],{"type":"screen","username":state["u"],"on":state["screen"]})
                await push_members(state["s"])

            elif a=="call_start":
                if not state.get("s"): continue
                state["voice"]=True
                state["call_peer"]=None
                state["media_room"]="srv:"+state["s"]
                await move_user_media(state["token"],state["media_room"],"voice")
                await broadcast_server(state["s"],{"type":"incoming_call","from":state["u"]},ws)
                await push_members(state["s"])

            elif a=="call_answer":
                if state.get("s"):
                    state["voice"]=True
                    state["call_peer"]=None
                    state["media_room"]="srv:"+state["s"]
                    await move_user_media(state["token"],state["media_room"],"voice")
                    await push_members(state["s"])

            elif a=="call_reject":
                await broadcast_server(state["s"],{"type":"call_state","user":state["u"],"state":"rejected"})

            elif a=="dm_call_start":
                peer=str(m.get("peer","")).strip()
                if not await adb(is_friend,state["u"],peer):
                    await send(ws,{"type":"error","message":"Özel arama için arkadaş olmalısınız."}); continue
                room=dm_room(state["u"],peer)
                state["call_peer"]=peer; state["media_room"]=room; state["voice"]=True
                await move_user_media(state["token"],room,"voice")
                await send_to_user(peer,{"type":"incoming_dm_call","from":state["u"]})
                await send(ws,{"type":"dm_call_state","peer":peer,"state":"ringing"})

            elif a=="dm_call_answer":
                peer=str(m.get("peer","")).strip()
                if not await adb(is_friend,state["u"],peer): continue
                room=dm_room(state["u"],peer)
                state["call_peer"]=peer; state["media_room"]=room; state["voice"]=True
                await move_user_media(state["token"],room,"voice")
                for ow,os_ in list(clients.items()):
                    if os_.get("kind")=="main" and os_.get("u")==peer:
                        os_["call_peer"]=state["u"]; os_["media_room"]=room; os_["voice"]=True
                        await move_user_media(os_.get("token"),room,"voice")
                await send_to_user(peer,{"type":"dm_call_state","peer":state["u"],"state":"accepted"})
                await send(ws,{"type":"dm_call_state","peer":peer,"state":"accepted"})

            elif a=="dm_call_reject":
                peer=str(m.get("peer","")).strip()
                await send_to_user(peer,{"type":"dm_call_state","peer":state["u"],"state":"rejected"})

            elif a=="dm_call_end":
                peer=state.get("call_peer") or str(m.get("peer","")).strip()
                old_room=state.get("media_room")
                state["call_peer"]=None; state["voice"]=False; state["media_room"]=None
                await move_user_media(state["token"],None,"voice")
                if peer:
                    for ow,os_ in list(clients.items()):
                        if os_.get("kind")=="main" and os_.get("u")==peer and os_.get("media_room")==old_room:
                            os_["call_peer"]=None; os_["voice"]=False; os_["media_room"]=None
                            await move_user_media(os_.get("token"),None,"voice")
                    await send_to_user(peer,{"type":"dm_call_state","peer":state["u"],"state":"ended"})
                await send(ws,{"type":"dm_call_state","peer":peer,"state":"ended"})

    except Exception as exc:
        print("HANDLER ERROR:", repr(exc))
    finally:
        state=clients.pop(ws,None)
        if not state: return
        if state.get("kind")=="media":
            room=state.get("room"); kind=state.get("media_kind")
            if room: media_rooms.get(room,{}).get(kind,set()).discard(ws)
            return
        token=state.get("token")
        if token and sessions.get(token)==ws: sessions.pop(token,None)
        if state.get("u"):
            await adb(touch_user,state["u"])
        if state.get("s"): await push_members(state["s"])
        if state.get("u"):
            # refresh friend online states
            for f in await adb(friend_list,state["u"]):
                await send_to_user(f["username"],{"type":"friend_presence","username":state["u"],"online":False,"last_seen":now()})


async def http_or_ws(request):
    if request.headers.get("Upgrade","").lower()!="websocket":
        return web.Response(text="SIKKORD backend aktif.\n",status=200)
    ws=web.WebSocketResponse(heartbeat=15,max_msg_size=12_000_000,compress=False)
    await ws.prepare(request)
    await handler(WSAdapter(ws))
    return ws


async def on_startup(app):
    init_pool()
    await asyncio.to_thread(init_db)
    print("PostgreSQL havuzu + bağlantısı başarılı.")
    print(f"SIKKORD ULTRA V6 -> {HOST}:{PORT}")


async def on_cleanup(app):
    global DB_POOL
    if DB_POOL is not None:
        await asyncio.to_thread(DB_POOL.close)
        DB_POOL=None


def make_app():
    app=web.Application(client_max_size=12_000_000)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*","/{tail:.*}",http_or_ws)
    return app


if __name__=="__main__":
    web.run_app(make_app(),host=HOST,port=PORT,access_log=None)
