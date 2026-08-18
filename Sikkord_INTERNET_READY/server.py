import asyncio
import hashlib
import json
import os
import secrets
import time

import psycopg
import websockets
from psycopg.errors import UniqueViolation

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))

# Render'da PostgreSQL Environment Variable olarak DATABASE_URL tanımlı olmalı.
# Lokal kullanımda da aynı değişkeni ayarlayabilirsin.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

MAX_AVATAR = 700_000
clients = {}          # websocket -> state
sessions = {}         # token -> main websocket
media_rooms = {}      # code -> {"voice": set(), "screen": set()}


def db_url():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL bulunamadı. Render > Environment içinde "
            "DATABASE_URL ekleyip PostgreSQL Internal Database URL'yi seç."
        )
    return DATABASE_URL


def conn():
    return psycopg.connect(db_url(), connect_timeout=10)


def init_db():
    """PostgreSQL tablolarını ilk çalıştırmada güvenli şekilde oluşturur."""
    c = conn()
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                avatar TEXT,
                created_at BIGINT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                server_code TEXT NOT NULL,
                UNIQUE(username, server_code)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                server_code TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0
            )
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_server_id
            ON messages(server_code, id)
        """)

        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_members_server
            ON members(server_code)
        """)

        c.commit()
    finally:
        c.close()


def hp(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
    )
    return salt.hex() + ":" + digest.hex()


def vp(password, value):
    try:
        salt, digest = value.split(":")
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=2**14,
            r=8,
            p=1,
        )
        return secrets.compare_digest(actual, bytes.fromhex(digest))
    except Exception:
        return False


def invite_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join(
        "".join(secrets.choice(alphabet) for _ in range(4))
        for _ in range(2)
    )


def register(username, password):
    username = username.strip()

    if not 3 <= len(username) <= 24:
        return False, "Kullanıcı adı 3-24 karakter olmalı."

    if not 6 <= len(password) <= 128:
        return False, "Şifre en az 6 karakter olmalı."

    c = conn()
    try:
        c.execute(
            """
            INSERT INTO users(username, password_hash, avatar, created_at)
            VALUES(%s, %s, %s, %s)
            """,
            (username, hp(password), None, int(time.time())),
        )
        c.commit()
        return True, "Kayıt başarılı. Şimdi giriş yapabilirsin."
    except UniqueViolation:
        c.rollback()
        return False, "Bu kullanıcı adı zaten kullanılıyor."
    finally:
        c.close()


def login(username, password):
    c = conn()
    try:
        row = c.execute(
            """
            SELECT password_hash, avatar
            FROM users
            WHERE username = %s
            """,
            (username,),
        ).fetchone()

        return bool(row and vp(password, row[0])), (row[1] if row else None)
    finally:
        c.close()


def create_server(name, username):
    name = name.strip()[:40] or "Yeni Sunucu"
    c = conn()
    code = invite_code()

    try:
        while c.execute(
            "SELECT 1 FROM servers WHERE code = %s",
            (code,),
        ).fetchone():
            code = invite_code()

        c.execute(
            """
            INSERT INTO servers(name, code, owner, created_at)
            VALUES(%s, %s, %s, %s)
            """,
            (name, code, username, int(time.time())),
        )

        c.execute(
            """
            INSERT INTO members(username, server_code)
            VALUES(%s, %s)
            ON CONFLICT(username, server_code) DO NOTHING
            """,
            (username, code),
        )

        c.commit()
        return code
    finally:
        c.close()


def join_server(username, code):
    code = code.upper().strip()
    c = conn()

    try:
        row = c.execute(
            "SELECT name FROM servers WHERE code = %s",
            (code,),
        ).fetchone()

        if not row:
            return None

        c.execute(
            """
            INSERT INTO members(username, server_code)
            VALUES(%s, %s)
            ON CONFLICT(username, server_code) DO NOTHING
            """,
            (username, code),
        )

        c.commit()
        return row[0]
    finally:
        c.close()


def user_servers(username):
    c = conn()
    try:
        return c.execute(
            """
            SELECT s.name, s.code
            FROM servers s
            JOIN members m ON m.server_code = s.code
            WHERE m.username = %s
            ORDER BY s.id
            """,
            (username,),
        ).fetchall()
    finally:
        c.close()


def history(code):
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT id, username, message, created_at, deleted
            FROM messages
            WHERE server_code = %s
            ORDER BY id DESC
            LIMIT 150
            """,
            (code,),
        ).fetchall()

        out = []

        for message_id, username, message, created_at, deleted in reversed(rows):
            av = c.execute(
                "SELECT avatar FROM users WHERE username = %s",
                (username,),
            ).fetchone()

            out.append(
                {
                    "id": message_id,
                    "username": username,
                    "avatar": av[0] if av else None,
                    "message": "[mesaj silindi]" if deleted else message,
                    "created_at": created_at,
                    "deleted": bool(deleted),
                }
            )

        return out
    finally:
        c.close()


def save_message(username, code, message):
    c = conn()
    try:
        cur = c.execute(
            """
            INSERT INTO messages(
                username, server_code, message, created_at, deleted
            )
            VALUES(%s, %s, %s, %s, 0)
            RETURNING id
            """,
            (username, code, message, int(time.time())),
        )

        message_id = cur.fetchone()[0]
        c.commit()
        return message_id
    finally:
        c.close()


def delete_message(username, code, message_id):
    c = conn()
    try:
        row = c.execute(
            """
            SELECT username
            FROM messages
            WHERE id = %s AND server_code = %s
            """,
            (message_id, code),
        ).fetchone()

        if not row or row[0] != username:
            return False

        c.execute(
            """
            UPDATE messages
            SET deleted = 1, message = ''
            WHERE id = %s
            """,
            (message_id,),
        )

        c.commit()
        return True
    finally:
        c.close()


def server_members(code):
    c = conn()
    try:
        rows = c.execute(
            """
            SELECT m.username, COALESCE(u.avatar, '')
            FROM members m
            LEFT JOIN users u ON u.username = m.username
            WHERE m.server_code = %s
            ORDER BY LOWER(m.username)
            """,
            (code,),
        ).fetchall()
    finally:
        c.close()

    online = {
        clients[w].get("u")
        for w in clients
        if clients[w].get("kind") == "main"
        and clients[w].get("s") == code
        and clients[w].get("u")
    }

    result = []

    for username, avatar in rows:
        voice = any(
            clients.get(w, {}).get("kind") == "main"
            and clients.get(w, {}).get("s") == code
            and clients.get(w, {}).get("u") == username
            and clients.get(w, {}).get("voice")
            for w in clients
        )

        muted = any(
            clients.get(w, {}).get("kind") == "main"
            and clients.get(w, {}).get("s") == code
            and clients.get(w, {}).get("u") == username
            and clients.get(w, {}).get("muted")
            for w in clients
        )

        result.append(
            {
                "username": username,
                "avatar": avatar or None,
                "online": username in online,
                "voice": voice,
                "muted": muted,
            }
        )

    return result


async def send(ws, payload):
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def broadcast_main(code, payload, exclude=None):
    await asyncio.gather(
        *(
            send(w, payload)
            for w, state in list(clients.items())
            if state.get("kind") == "main"
            and state.get("s") == code
            and w != exclude
        ),
        return_exceptions=True,
    )


async def broadcast_media(code, kind, data, exclude=None):
    pool = media_rooms.get(code, {}).get(kind, set())

    async def one(ws):
        if ws == exclude:
            return

        try:
            await ws.send(data)
        except Exception:
            pass

    await asyncio.gather(
        *(one(w) for w in list(pool)),
        return_exceptions=True,
    )


async def push_members(code):
    if code:
        await broadcast_main(
            code,
            {"type": "members", "items": server_members(code)},
        )


async def move_media(ws, code):
    state = clients.get(ws, {})
    kind = state.get("media_kind")

    if not kind:
        return

    old = state.get("s")

    if old:
        media_rooms.get(old, {}).get(kind, set()).discard(ws)

    state["s"] = code

    if code:
        media_rooms.setdefault(
            code,
            {"voice": set(), "screen": set()},
        ).setdefault(kind, set()).add(ws)


async def handler(ws):
    clients[ws] = {
        "kind": "unknown",
        "u": None,
        "s": None,
        "voice": False,
        "muted": False,
        "screen": False,
        "token": None,
        "media_kind": None,
        "avatar": None,
    }

    try:
        async for incoming in ws:
            state = clients[ws]

            # İlk paket medya bağlantısının kimlik doğrulaması olabilir.
            if state["kind"] == "unknown":
                if isinstance(incoming, bytes):
                    continue

                try:
                    first = json.loads(incoming)
                except Exception:
                    continue

                if first.get("action") == "media_auth":
                    token = str(first.get("token", ""))
                    main = sessions.get(token)
                    kind = str(first.get("kind", ""))

                    if main not in clients or kind not in ("voice", "screen"):
                        await send(ws, {"type": "media_auth", "ok": False})
                        await ws.close()
                        return

                    main_state = clients[main]

                    state.update(
                        {
                            "kind": "media",
                            "u": main_state["u"],
                            "s": main_state["s"],
                            "token": token,
                            "media_kind": kind,
                        }
                    )

                    await move_media(ws, main_state["s"])

                    await send(
                        ws,
                        {
                            "type": "media_auth",
                            "ok": True,
                            "kind": kind,
                        },
                    )
                    continue

                state["kind"] = "main"

            if state["kind"] == "media":
                if isinstance(incoming, bytes) and state.get("s"):
                    prefix = b"V" if state["media_kind"] == "voice" else b"S"

                    if incoming.startswith(prefix):
                        await broadcast_media(
                            state["s"],
                            state["media_kind"],
                            incoming,
                            ws,
                        )

                continue

            if isinstance(incoming, bytes):
                continue

            try:
                message = json.loads(incoming)
            except Exception:
                continue

            action = message.get("action")

            # ---------------- AUTH ----------------

            if action == "register":
                ok, msg = register(
                    str(message.get("username", "")).strip(),
                    str(message.get("password", "")),
                )

                await send(
                    ws,
                    {
                        "type": "register",
                        "ok": ok,
                        "message": msg,
                    },
                )
                continue

            if action == "login":
                username = str(message.get("username", "")).strip()
                ok, avatar = login(
                    username,
                    str(message.get("password", "")),
                )

                if ok:
                    token = secrets.token_urlsafe(32)
                    sessions[token] = ws

                    state.update(
                        {
                            "u": username,
                            "token": token,
                            "avatar": avatar,
                        }
                    )

                    await send(
                        ws,
                        {
                            "type": "login",
                            "ok": True,
                            "username": username,
                            "avatar": avatar,
                            "token": token,
                        },
                    )
                else:
                    await send(
                        ws,
                        {
                            "type": "login",
                            "ok": False,
                            "message": "Kullanıcı adı veya şifre yanlış.",
                        },
                    )

                continue

            if not state["u"]:
                await send(
                    ws,
                    {
                        "type": "error",
                        "message": "Önce giriş yap.",
                    },
                )
                continue

            # ---------------- SERVERS ----------------

            if action == "servers":
                await send(
                    ws,
                    {
                        "type": "servers",
                        "items": [
                            {"name": name, "code": code}
                            for name, code in user_servers(state["u"])
                        ],
                    },
                )

            elif action == "create":
                name = str(message.get("name", "Yeni Sunucu"))
                code = create_server(name, state["u"])

                await send(
                    ws,
                    {
                        "type": "created",
                        "name": name[:40],
                        "code": code,
                    },
                )

            elif action == "join":
                code = str(message.get("code", "")).upper().strip()
                name = join_server(state["u"], code)

                await send(
                    ws,
                    {
                        "type": "joined",
                        "ok": bool(name),
                        "name": name,
                        "code": code,
                    },
                )

            elif action == "enter":
                code = str(message.get("code", "")).upper().strip()
                name = join_server(state["u"], code)

                if not name:
                    await send(
                        ws,
                        {
                            "type": "error",
                            "message": "Sunucu bulunamadı.",
                        },
                    )
                    continue

                old = state["s"]

                if old:
                    await broadcast_main(
                        old,
                        {
                            "type": "presence",
                            "username": state["u"],
                            "online": False,
                        },
                        ws,
                    )

                state.update(
                    {
                        "s": code,
                        "voice": False,
                        "muted": False,
                        "screen": False,
                    }
                )

                # Bu oturuma bağlı medya bağlantılarını yeni sunucuya taşı.
                for media_ws, media_state in list(clients.items()):
                    if (
                        media_state.get("kind") == "media"
                        and media_state.get("token") == state["token"]
                    ):
                        await move_media(media_ws, code)

                await send(
                    ws,
                    {
                        "type": "entered",
                        "name": name,
                        "code": code,
                        "history": history(code),
                        "members": server_members(code),
                    },
                )

                await broadcast_main(
                    code,
                    {
                        "type": "presence",
                        "username": state["u"],
                        "online": True,
                    },
                    ws,
                )

                await push_members(code)

            # ---------------- CHAT ----------------

            elif action == "chat":
                code = state["s"]
                text = str(message.get("message", "")).strip()

                if code and text and len(text) <= 2000:
                    message_id = save_message(
                        state["u"],
                        code,
                        text,
                    )

                    avatar = state.get("avatar")

                    if not avatar:
                        ctmp = conn()
                        try:
                            row = ctmp.execute(
                                """
                                SELECT avatar
                                FROM users
                                WHERE username = %s
                                """,
                                (state["u"],),
                            ).fetchone()

                            avatar = row[0] if row else None
                        finally:
                            ctmp.close()

                    await broadcast_main(
                        code,
                        {
                            "type": "chat",
                            "id": message_id,
                            "username": state["u"],
                            "avatar": avatar,
                            "message": text,
                            "created_at": int(time.time()),
                            "deleted": False,
                        },
                    )

            elif action == "delete_message":
                code = state["s"]

                try:
                    message_id = int(message.get("id"))
                except Exception:
                    message_id = 0

                if (
                    code
                    and delete_message(
                        state["u"],
                        code,
                        message_id,
                    )
                ):
                    await broadcast_main(
                        code,
                        {
                            "type": "message_deleted",
                            "id": message_id,
                            "username": state["u"],
                        },
                    )

            # ---------------- VOICE / SCREEN ----------------

            elif action == "voice":
                state["voice"] = bool(message.get("on"))

                await broadcast_main(
                    state["s"],
                    {
                        "type": "voice",
                        "username": state["u"],
                        "on": state["voice"],
                    },
                )

                await push_members(state["s"])

            elif action == "mic_mute":
                state["muted"] = bool(message.get("muted"))

                await broadcast_main(
                    state["s"],
                    {
                        "type": "mic_mute",
                        "username": state["u"],
                        "muted": state["muted"],
                    },
                )

                await push_members(state["s"])

            elif action == "screen":
                state["screen"] = bool(message.get("on"))

                await broadcast_main(
                    state["s"],
                    {
                        "type": "screen",
                        "username": state["u"],
                        "on": state["screen"],
                    },
                )

            # ---------------- CALL ----------------

            elif action == "call_start":
                state["voice"] = True
                state["muted"] = False

                await broadcast_main(
                    state["s"],
                    {
                        "type": "incoming_call",
                        "from": state["u"],
                    },
                    ws,
                )

                await broadcast_main(
                    state["s"],
                    {
                        "type": "voice",
                        "username": state["u"],
                        "on": True,
                    },
                )

                await push_members(state["s"])

            elif action == "call_answer":
                state["voice"] = True
                state["muted"] = False

                await broadcast_main(
                    state["s"],
                    {
                        "type": "call_state",
                        "user": state["u"],
                        "state": "accepted",
                    },
                )

                await broadcast_main(
                    state["s"],
                    {
                        "type": "voice",
                        "username": state["u"],
                        "on": True,
                    },
                )

                await push_members(state["s"])

            elif action == "call_reject":
                await broadcast_main(
                    state["s"],
                    {
                        "type": "call_state",
                        "user": state["u"],
                        "state": "rejected",
                    },
                )

            elif action == "call_end":
                state["voice"] = False
                state["muted"] = False

                await broadcast_main(
                    state["s"],
                    {
                        "type": "call_state",
                        "user": state["u"],
                        "state": "ended",
                    },
                )

                await broadcast_main(
                    state["s"],
                    {
                        "type": "voice",
                        "username": state["u"],
                        "on": False,
                    },
                )

                await push_members(state["s"])

            # ---------------- PROFILE ----------------

            elif action == "profile":
                avatar = str(message.get("avatar", ""))

                if len(avatar) > MAX_AVATAR:
                    await send(
                        ws,
                        {
                            "type": "profile",
                            "ok": False,
                            "message": "Profil fotoğrafı çok büyük.",
                        },
                    )
                else:
                    c = conn()

                    try:
                        c.execute(
                            """
                            UPDATE users
                            SET avatar = %s
                            WHERE username = %s
                            """,
                            (avatar or None, state["u"]),
                        )

                        c.commit()
                    finally:
                        c.close()

                    state["avatar"] = avatar or None

                    await send(
                        ws,
                        {
                            "type": "profile",
                            "ok": True,
                            "avatar": avatar or None,
                        },
                    )

                    await push_members(state["s"])

    except websockets.exceptions.ConnectionClosed:
        pass

    except Exception as exc:
        print("HANDLER ERROR:", repr(exc))

    finally:
        state = clients.pop(ws, None)

        if not state:
            return

        if state.get("kind") == "media":
            code = state.get("s")
            kind = state.get("media_kind")

            if code:
                media_rooms.get(code, {}).get(kind, set()).discard(ws)

            return

        token = state.get("token")

        if token and sessions.get(token) == ws:
            sessions.pop(token, None)

        code = state.get("s")

        if code:
            await broadcast_main(
                code,
                {
                    "type": "presence",
                    "username": state["u"],
                    "online": False,
                },
            )

            await broadcast_main(
                code,
                {
                    "type": "voice",
                    "username": state["u"],
                    "on": False,
                },
            )

            await push_members(code)

        # Bu oturuma bağlı medya soketlerini kapat.
        for media_ws, media_state in list(clients.items()):
            if (
                media_state.get("kind") == "media"
                and media_state.get("token") == token
            ):
                try:
                    await media_ws.close()
                except Exception:
                    pass


async def main():
    print("SIKKORD PostgreSQL backend başlatılıyor...")

    # Uygulama açılırken tabloları oluştur.
    init_db()

    print(f"SIKKORD BACKEND -> {HOST}:{PORT}")
    print("PostgreSQL bağlantısı başarılı.")

    async with websockets.serve(
        handler,
        HOST,
        PORT,
        max_size=12_000_000,
        ping_interval=15,
        ping_timeout=15,
        compression=None,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
