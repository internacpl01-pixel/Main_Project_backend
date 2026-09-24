"""
Email one-time-code login — admin.login_otps.

Two steps, mirroring password login's own shape: request a code (like typing
a username), then verify it (like typing a password). Neither step here knows
or cares whether the email belongs to a real account -- routers.auth checks
that separately, on both ends, for the same reason /auth/login's "wrong
username or password" never says which of the two was wrong: telling a
stranger "no account uses that email" is a way to fish for which addresses
are registered here at all.
"""
import hashlib
import random
import secrets
from datetime import datetime, timedelta, timezone

import config

CODE_LENGTH = 6


def _hash(code: str, email: str) -> str:
    # email folded in so the same 6-digit code sent to two different
    # addresses never hashes to the same row -- sha256 rather than bcrypt
    # since this hides a short-lived, single-use, server-generated random
    # value rather than a user-chosen secret asked to resist offline guessing
    # forever; bcrypt's cost would only slow down this endpoint's own request,
    # not an attacker's.
    return hashlib.sha256(f"{code}:{email.lower()}".encode("utf-8")).hexdigest()


async def request_code(conn, email: str) -> None:
    """Generate a code, store its hash, email it. Raises Cooldown if one was
    already sent to this address too recently.

    Silent about whether the address has an account -- routers.auth checks
    that before calling this at all and returns the same generic response
    either way, so this function is never reached for an address the caller
    has already decided not to help enumerate.
    """
    email = email.strip().lower()

    recent = await conn.fetchval(
        """
        SELECT created_at FROM admin.login_otps
         WHERE lower(email) = $1
         ORDER BY created_at DESC
         LIMIT 1
        """,
        email,
    )
    if recent is not None:
        elapsed = (datetime.now(timezone.utc) - recent).total_seconds()
        wait = config.OTP_RESEND_COOLDOWN_SECONDS - elapsed
        if wait > 0:
            raise Cooldown(int(wait) + 1)

    # secrets.choice, not random.randint -- this is a login credential, however
    # short-lived, and random.* is not cryptographically strong.
    code = "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=config.OTP_EXPIRE_MINUTES)

    await conn.execute(
        """
        INSERT INTO admin.login_otps (email, code_hash, expires_at)
        VALUES ($1, $2, $3)
        """,
        email, _hash(code, email), expires_at,
    )

    from services.email import send_email
    send_email(
        to=email,
        subject="Your sign-in code",
        body=(
            f"Your sign-in code is {code}\n\n"
            f"It expires in {config.OTP_EXPIRE_MINUTES} minutes. If you did not "
            f"request this, you can ignore this email."
        ),
    )


async def verify_code(conn, email: str, code: str) -> bool:
    """True and consumes the row if this is the current, unexpired,
    unconsumed code for this email. False otherwise -- never raises for a
    wrong code, since a wrong guess is an ordinary, expected outcome here,
    the same as a wrong password.
    """
    email = email.strip().lower()
    code = (code or "").strip()
    if not code:
        return False

    row = await conn.fetchrow(
        """
        SELECT id FROM admin.login_otps
         WHERE lower(email) = $1
           AND code_hash = $2
           AND consumed_at IS NULL
           AND expires_at > now()
        """,
        email, _hash(code, email),
    )
    if row is None:
        return False

    # Consumed rather than deleted: keeps a record of which codes were
    # actually used, the same reason nothing here ever deletes a used row.
    await conn.execute(
        "UPDATE admin.login_otps SET consumed_at = now() WHERE id = $1", row["id"]
    )
    return True


class Cooldown(Exception):
    """A code was already sent to this address too recently. Carries how many
    more seconds the caller must wait.
    """
    def __init__(self, seconds_left: int):
        self.seconds_left = seconds_left
        super().__init__(f"Wait {seconds_left}s before requesting another code.")
