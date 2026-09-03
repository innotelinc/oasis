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

## Nginx Proxy Manager edge

For deployments that terminate the public Oasis hostnames with Nginx Proxy
Manager (NPM), `scripts/npm-proxy-hosts.py` creates the proxy hosts for
`app`, `api`, `auth`, `mail`, `files`, and `admin` under `OASIS_BASE_DOMAIN`
through the NPM API. The same call provisions the HTTPS certificate:

- **Wildcard (default):** with `NPM_DNS_PROVIDER` and `NPM_DNS_CREDENTIALS`
  set, a `*.<domain>` Let's Encrypt certificate is requested via the DNS-01
  challenge, and `OASIS_WILDCARD_SSL=true` (default) attaches it to every
  host.
- **Per-hostname fallback:** without DNS credentials, one Let's Encrypt SAN
  certificate covering the selected hostnames is requested via HTTP-01
  (requires inbound port 80).

```dotenv
OASIS_BASE_DOMAIN=oasis.innotel.us
OASIS_WILDCARD_SSL=true
NPM_API_URL=http://127.0.0.1:81
NPM_UPSTREAM_HOST=host.docker.internal
NPM_API_TOKEN=...            # or NPM_ADMIN_EMAIL + NPM_ADMIN_PASSWORD
ACME_EMAIL=admin@oasis.innotel.us
NPM_DNS_PROVIDER=cloudflare  # wildcard DNS-01
NPM_DNS_CREDENTIALS=dns_cloudflare_api_token=...
```

Then either run the sync manually or let `scripts/setup-oasis.sh` do it in
production mode:

```bash
python3 scripts/npm-proxy-hosts.py --check   # read-only drift report
python3 scripts/npm-proxy-hosts.py           # apply, idempotent
python3 scripts/npm-proxy-hosts.py --no-prune
python3 scripts/npm-proxy-hosts.py --no-wildcard   # force HTTP-01 SAN cert
```

The compose proxy in this directory remains the Authentik stack gateway; NPM
hosts forward to the Oasis module ports (`app` 3000, `api` 8000, `auth` 9000,
`mail` 8080, `files` 8081, `admin` 3001). Both can coexist: NPM terminates
public hostnames while the compose proxy serves the stack internally.
