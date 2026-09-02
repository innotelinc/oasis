# Oasis reverse proxy

## Local development

Local mode is HTTP-only and publishes the proxy on port `8080` by default:

```bash
docker compose --profile local up -d
# Open http://localhost:8080
```

Do not use local mode on a publicly reachable host. Local HTTP is intended for
isolated development only and should not be used for real credentials.

## Production HTTPS

Production mode uses Nginx plus Certbot with the ACME HTTP-01 challenge. Ensure
that DNS for `AUTH_DOMAIN` points to the server and inbound TCP ports 80 and 443
are reachable.

Set these values in `.env`:

```dotenv
AUTH_DOMAIN=auth.oasis.innotel.us
ACME_EMAIL=admin@oasis.innotel.us
HTTP_BIND=80
HTTPS_BIND=443
```

Start the proxy and request the initial certificate:

```bash
docker compose --profile production up -d
sudo ACME_EMAIL=admin@oasis.innotel.us \
  AUTH_DOMAIN=auth.oasis.innotel.us \
  scripts/init-certificates.sh
```

The helper is idempotent and does not replace an existing certificate. The
`certbot` service renews certificates every 12 hours using the shared webroot.
After renewal, reload Nginx so it reads the new certificate:

```bash
docker compose exec reverse-proxy nginx -s reload
```

For multiple Oasis hostnames, add each hostname to the production Nginx
`server_name` and issue the certificate with additional `-d` arguments. DNS
challenge automation should be used instead when port 80 cannot be exposed;
provider credentials must be supplied through Docker secrets or an external
secret manager, never committed to this repository.
