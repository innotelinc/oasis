# Oasis Authentik bootstrap

Authentik is the central identity provider for Oasis. The Compose stack starts
Authentik with PostgreSQL and Redis, but intentionally does not create an admin
account or OAuth clients automatically. This avoids placing administrator
credentials or client secrets in version control.

## Initial setup

1. Copy `.env.example` to `.env` and replace every `replace-with-` value with a
   unique random secret.
2. Set `AUTH_DOMAIN` to the public DNS name for Authentik.
3. Put a valid certificate at `certs/fullchain.pem` and `certs/privkey.pem`.
4. Start the infrastructure:

   ```bash
   docker compose up -d postgres redis authentik-server authentik-worker reverse-proxy
   ```

5. Open `https://auth.oasis.innotel.us/if/initial-setup/` and complete the
   Authentik administrator setup. The trailing slash is required by Authentik.
6. Enable MFA for administrators, create an `Oasis Administrators` group, and
   assign administrative users only as needed.

Do not use the sample domain or sample secrets in production.

## Recommended tenant model

Create one Authentik `Organization` per Oasis tenant, or use groups when a
single organization owns multiple domains. Keep platform operators separate
from tenant administrators. Use group membership and Authentik policies for
RBAC; applications should enforce authorization using the claims they receive
and their own tenant ownership checks.

## Provider creation

The files in this directory are templates, not importable secrets. For each
Oasis service, create an Authentik OAuth2/OpenID Provider and an application in
the Authentik admin UI, or translate the values into Authentik's API.

Use:

- Authorization flow: `default-provider-authorization-implicit-consent`
- Signing key: an Authentik-managed RSA key
- Client type: `confidential` for server-side applications; `public` only for
  browser or native clients using PKCE
- Redirect URIs: exact HTTPS URIs only; do not use wildcards in production
- Scopes: `openid profile email groups oasis_tenant`

Store generated client secrets in a secrets manager or an ignored `.env` file.
Never commit them.
