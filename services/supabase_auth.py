"""
Email magic-link login, delivered and verified by Supabase Auth (GoTrue) --
not by this app. Supabase generates the link, emails it (its own default
mailer -- no SMTP setup needed on either side, confirmed with the user as the
whole point of choosing this over a typed code), and verifies a click on it;
this module only calls its REST API and reports back what it found.

The relationship to routers.auth.google_login is the same shape: an external
identity provider proves someone controls an email address, and this app
trusts that proof rather than re-implementing it. There, the proof is a
signed Google ID token; here, it is a Supabase session access_token minted
only after that address's own inbox was clicked into.

Flow:
  1. send_link(email) -- POST /auth/v1/otp asks Supabase to email the link.
     redirect_to points it at this app's own frontend callback route
     (config.FRONTEND_URL + /auth/callback) rather than Supabase's own
     placeholder page.
  2. Clicking the link lands on that callback route with a Supabase session
     (access_token/refresh_token) in the URL fragment -- see
     MagicCallbackPage.jsx, which reads it and posts the access_token to
     POST /auth/otp/exchange.
  3. email_for_access_token(access_token) -- GET /auth/v1/user with that
     token as a Bearer credential. Supabase itself validates the token and
     returns the account it belongs to; this app never verifies the token's
     signature itself; it asks Supabase what the token is a session for and
     trusts that.
"""
import httpx

import config


class SupabaseAuthNotConfigured(Exception):
    """SUPABASE_URL or SUPABASE_ANON_KEY is blank -- the feature is disabled."""


class SupabaseAuthError(Exception):
    """Supabase itself refused the request -- its own message, shown as-is
    (its rate-limit and validation errors already read fine to an end user,
    the same way Google's own sign-in errors are surfaced verbatim).
    """


def _require_config() -> tuple[str, str]:
    if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
        raise SupabaseAuthNotConfigured(
            "Email sign-in is not set up on this server yet (SUPABASE_URL / "
            "SUPABASE_ANON_KEY blank in .env)."
        )
    return config.SUPABASE_URL.rstrip("/"), config.SUPABASE_ANON_KEY


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return "Could not send the sign-in link."
    return (body.get("msg") or body.get("error_description")
            or body.get("error") or "Could not send the sign-in link.")


async def send_link(email: str) -> None:
    """Ask Supabase to email a sign-in link to this address.

    create_user=True rather than False: this app's own caller
    (routers.auth.request_otp) already refused to reach here at all unless
    the email matches one of ITS OWN accounts, so letting Supabase silently
    keep its own shadow auth.users row for that email is harmless bookkeeping
    on Supabase's side, not a new way to enumerate anything -- the gate that
    matters already happened.

    redirect_to is a query param on the request itself (GoTrue's own
    convention for every magic-link-style endpoint), not a JSON body field --
    it must also be on that project's Authentication -> URL Configuration
    "Redirect URLs" allow-list, or Supabase refuses to honour it and falls
    back to the Site URL instead.
    """
    base_url, anon_key = _require_config()
    params = {}
    if config.FRONTEND_URL:
        params["redirect_to"] = f"{config.FRONTEND_URL.rstrip('/')}/auth/callback"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{base_url}/auth/v1/otp",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            params=params,
            json={"email": email, "create_user": True},
        )
    if resp.status_code >= 400:
        raise SupabaseAuthError(_error_message(resp))


async def email_for_access_token(access_token: str) -> str:
    """The email a Supabase session access_token belongs to, proving whoever
    holds it clicked a link Supabase itself sent to that inbox.

    Raises SupabaseAuthError for a token Supabase does not recognise (expired
    link, already used, tampered with) -- never returns a guess.
    """
    access_token = (access_token or "").strip()
    if not access_token:
        raise SupabaseAuthError("No sign-in token was provided.")

    base_url, anon_key = _require_config()
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{base_url}/auth/v1/user",
            headers={"apikey": anon_key, "Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code >= 400:
        raise SupabaseAuthError("That sign-in link is invalid or has expired.")

    email = resp.json().get("email")
    if not email:
        raise SupabaseAuthError("That sign-in link has no email attached to it.")
    return email
