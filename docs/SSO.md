# Oasis SSO integration

Oasis uses Authentik as its OAuth 2.0 and OpenID Connect identity provider.
Applications should use Authorization Code with PKCE where possible and must
validate tokens locally or through the provider's standards-compliant endpoints.

## Provider discovery

The canonical issuer is configured by the Authentik application provider. With
the default deployment, the discovery URL is:

```text
https://auth.oasis.innotel.us/application/o/oasis/.well-known/openid-configuration
```

Applications should discover `authorization_endpoint`, `token_endpoint`,
`userinfo_endpoint`, `jwks_uri`, and supported scopes from this document rather
than hard-coding endpoint paths.

## Application configuration

Typical server-side settings are:

```dotenv
OIDC_ISSUER=https://auth.oasis.innotel.us/application/o/oasis/
OIDC_CLIENT_ID=oasis-service
OIDC_CLIENT_SECRET=stored-outside-source-control
OIDC_SCOPES=openid profile email groups oasis_tenant
OIDC_REDIRECT_URI=https://service.oasis.innotel.us/auth/callback
```

Public browser clients must use PKCE and must not contain a client secret.
Confidential clients must keep the secret in a secret manager or an ignored
runtime environment file.

## Required validation

Every Oasis service must:

- Validate the issuer, signature, expiration, `nonce`, and `state`.
- Validate the token audience and authorized-party claims.
- Require HTTPS outside explicitly isolated local development.
- Use exact registered redirect URIs.
- Map Authentik groups to application roles using an allowlisted mapping.
- Enforce tenant authorization server-side; a tenant claim is not sufficient by
  itself without checking resource ownership.
- Avoid logging access tokens, refresh tokens, authorization codes, or secrets.

Recommended role mapping:

| Authentik group | Oasis role |
|---|---|
| `oasis-platform-admins` | platform administrator |
| `oasis-tenant-admins` | tenant administrator |
| `oasis-users` | standard user |
| `oasis-auditors` | read-only compliance auditor |

## Logout

Use Authentik's `end_session_endpoint` from discovery. Applications should
clear their local session and redirect to the provider logout endpoint with a
validated `post_logout_redirect_uri`. Register that URI in Authentik before use.

## Local development

Local callback URIs may use `http://localhost` as shown in
`config/authentik/providers.yaml.example`. Do not register arbitrary hostnames,
wildcards, or non-local HTTP callbacks in a production provider.

## Bootstrap checklist

See [`config/authentik/README.md`](../config/authentik/README.md) for the first
administrator setup and provider creation guidance. After creating a provider,
record only its non-secret client ID and redirect URI in application deployment
configuration; keep the client secret private.
