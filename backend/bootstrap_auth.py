from __future__ import annotations

import os
import secrets

from backend import app as qa


def main() -> int:
    username = os.getenv("QA_ALERT_ADMIN_USERNAME", "").strip()
    password = os.getenv("QA_ALERT_ADMIN_PASSWORD", "")

    if not username or not password:
        print("[QA ALERT] Admin env credentials not configured; keeping existing login database.")
        return 0

    if len(username) < 3:
        print("[QA ALERT] QA_ALERT_ADMIN_USERNAME must be at least 3 characters.")
        return 1
    if len(password) < 8:
        print("[QA ALERT] QA_ALERT_ADMIN_PASSWORD must be at least 8 characters.")
        return 1

    salt = secrets.token_hex(16)
    digest = qa.password_digest(password, salt)

    with qa.db() as c:
        existing = c.execute(
            "SELECT id FROM users WHERE username=? COLLATE NOCASE",
            (username,),
        ).fetchone()

        if existing:
            user_id = existing["id"]
            c.execute(
                "UPDATE users SET password_hash=?,salt=? WHERE id=?",
                (digest, salt, user_id),
            )
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            action = "updated"
        else:
            cur = c.execute(
                "INSERT INTO users(username,password_hash,salt,created_at) VALUES (?,?,?,?)",
                (username, digest, salt, qa.utc_now()),
            )
            user_id = cur.lastrowid
            action = "created"

    print(f"[QA ALERT] Admin login {action}: {username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
