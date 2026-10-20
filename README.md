# skydive-theorie
Containerized e-learning website for the Dutch KNVVL A &amp; B theoretical exam.

## Overview
This website helps you prepare for the KNVVL theory exams required to obtain your skydiving license (A or B brevet). It provides:

- Practice exams with randomized questions
- Topic and subtopic filtering for focused study
- Instant feedback on answers and overall performance
- Learning resources page
- Contact form for inquiries
- Dynamic practice categories are available at `/oefenen`; the METAR/TAF exercises are under `/oefenen/meteo/METAR-TAF`

## ⚠️ Important Disclaimer

**These are NOT official KNVVL practice questions!**

The questions on this site are created independently for educational purposes only. If you have questions about exam content, please contact a KNVVL-approved para-centrum for accurate and up-to-date information.

## ✨ Features

- 🚀 **Quick Practice Sessions** - Start an exam immediately with a streamlined flow
- 📊 **Immediate Results** - View your score, progress, and topic breakdown instantly
- 🎯 **Theory-Focused** - Train on topics relevant to your KNVVL theory exam (A or B level)
- 🌙 **Dark/Light Mode** - Toggle between light and dark themes with theme preference saved
- 📱 **Responsive Design** - Works on desktop and mobile devices

## 🛠️ Technology Stack

- **Backend**: Flask (Python 3.14)
- **Database**: SQLite (`data.db`)
- **Containerization**: Docker
- **Production Server**: Gunicorn
- **Frontend**: Plain HTML/CSS/JavaScript with Google Fonts

## 📦 Installation & Setup

### Local testing with Docker Compose

The default Compose file builds the application locally and starts Flask behind Traefik. The application is not exposed directly; Traefik receives the request and forwards it over the internal Docker network. Local defaults use ports `8080` and `8443` with Traefik's development certificate.

```powershell
Copy-Item .env.example .env
$env:TRAEFIK_HOST = "skydive-theorie.localhost"
$env:TRAEFIK_HTTP_PORT = "8080"
$env:TRAEFIK_HTTPS_PORT = "8443"
docker compose up --build -d
```

Open `https://skydive-theorie.localhost:8443`. Your browser may show a certificate warning because this is local TLS. View logs or stop the stack with:

```powershell
docker compose logs -f
docker compose down
```

### Production deployment with Docker Compose, Portainer, and Traefik

Point DNS at the server and deploy `/home/runner/work/skydive-theorie/skydive-theorie/compose.production.yml` as the production stack. The file is standalone: it includes the app, Traefik, network, and volume definitions, and it does not expose Gunicorn directly on port `5000`.

When Portainer builds from the checked-out repository on the VPS, `APP_IMAGE` is not used. The standalone production stack builds from source by default.

Create a protected `.env` containing a long random `SECRET_KEY`, the public `TRAEFIK_HOST`, `ACME_EMAIL`, and the access gate settings:

```dotenv
SECRET_KEY=replace-with-a-long-random-secret
TRAEFIK_HOST=www.theorie.example.com
TRAEFIK_APEX_HOST=theorie.example.com
ACME_EMAIL=admin@example.com
ACCESS_GATE_ENABLED=1
ACCESS_CODE=replace-with-a-long-random-access-code
KNMI_API_KEY=your-rotated-knmi-key
KNMI_NOTIFICATION_API_KEY=your-notification-service-key
KNMI_OPEN_DATA_URL=https://api.dataplatform.knmi.nl/open-data
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=mailer@example.com
SMTP_PASSWORD=replace-with-an-smtp-password
SMTP_FROM_EMAIL=mailer@example.com
CONTACT_EMAIL=owner@example.com
SMTP_USE_TLS=1
SMTP_USE_SSL=0
```

The contact form requires `SMTP_HOST` and `CONTACT_EMAIL`. SMTP authentication is
used when `SMTP_USERNAME` is set. Use `SMTP_USE_SSL=1` for an SSL connection;
otherwise the default is STARTTLS on port `587`.

`TRAEFIK_HOST` is the canonical hostname the app is served on; `TRAEFIK_APEX_HOST` is the bare domain, which Traefik redirects to `TRAEFIK_HOST`. Make sure DNS records exist for both hostnames and point at the server.

If you want to publish the site immediately without the gate, set `ACCESS_GATE_ENABLED=0` and leave `ACCESS_CODE` empty.

Start the production stack with:

```powershell
docker compose -f compose.production.yml up -d --build --remove-orphans
docker compose -f compose.production.yml ps
```

The production stack also runs a separate log rotation container. It rotates Traefik logs daily and retains the latest 30 rotations in the shared `traefik-logs` volume.

If you prefer immutable image deployments instead of on-host builds, set `APP_IMAGE` and use the checked-in image override:

```powershell
docker compose -f compose.production.yml -f compose.production.image.yml pull
docker compose -f compose.production.yml -f compose.production.image.yml up -d --remove-orphans
```

Only Traefik publishes ports `80` and `443`; Gunicorn remains private to the Docker network. HTTP redirects to HTTPS, and Let's Encrypt state is stored in the named `letsencrypt` volume. Production session cookies are marked `Secure`, `HttpOnly`, and `SameSite=Lax`.

Concurrency: production runs 2 `app` replicas (2 Gunicorn workers x 6 threads each, tunable with `GUNICORN_PROCESSES`/`GUNICORN_THREADS`), sized for a 4-core host. Traefik compresses responses and routes `/static/` to a small `static` nginx container, so neither consumes Gunicorn threads. The `static` service is always built from the checked-out repository, even with `compose.production.image.yml`. Repeated wrong access-gate codes are throttled per client IP (5 per minute, tracked in the shared SQLite database so it holds across workers and replicas) instead of sleeping the request thread. The client IP comes from `X-Forwarded-For` via `ProxyFix`; in production one trusted proxy hop (Traefik) is assumed, adjustable with `TRUSTED_PROXY_COUNT`.

The SQLite `data.db` file contains versioned application content and the daily METAR cache. The image contains the latest seed database, while `/data/data.db` is stored in the persistent `app-data` volume. Both the local/test and production Compose app containers run the same `docker-entrypoint.sh`, which synchronizes the seed database before starting Gunicorn. On startup, questions and image-source metadata are synchronized from the image seed; the image-key foreign key is recreated with the updated question schema, while unrelated runtime data is preserved. Rebuild and deploy the app image with the normal Compose commands above to apply database schema/content changes; no manual volume migration is required.

### KNMI METAR configuration

The Gunicorn master starts a background MQTT listener for `dataplatform/file/v1/metar/1.0/created`. It uses the KNMI Notification Service over TLS WebSockets on port 443, with QoS 1 and a persistent client ID per app container. METARs are refreshed only when KNMI publishes a new file notification; when the event identifies an airport, only that airport is refreshed. The listener does not perform a timer-based refresh. Failed updates are logged and can be retried when a later file notification arrives.

Configure both server-side credentials through deployment environment variables; neither key is sent to the browser. `KNMI_NOTIFICATION_API_KEY` must be an API key with access to the Notification Service:

```powershell
$env:KNMI_API_KEY = "your-rotated-knmi-key"
$env:KNMI_NOTIFICATION_API_KEY = "your-notification-service-key"
$env:KNMI_OPEN_DATA_URL = "https://api.dataplatform.knmi.nl/open-data"
docker compose up --build -d
```

The app uses the `metar` dataset, version `1.0`, and retrieves the newest XML file for the notified airport on the current UTC day. METAR data is stored in the SQLite `metar` table; same-day fetch claims prevent duplicate API calls across Gunicorn processes and replicas. The `/data/data.db` volume persists this cache across deployments. Do not commit API keys to `.env` or source files, and rotate any key included in a public request.

KNMI Open Data requests use a descriptive User-Agent, are limited to 900 requests per rolling hour across the shared database, and retry rate-limit/server errors with exponential backoff (respecting `Retry-After`). METAR and TAF requests share this account-level quota; notifications are used instead of timer-based polling. See KNMI's [fair use policy](https://developer.dataplatform.knmi.nl/fair-use).

### IT systems status page

The public status page is available at `/it-systemen` and is not linked in the normal site navigation. A Gunicorn master process checks SMTP, KNMI Notification Service (MQTT), and KNMI Open Data in the background and stores the latest results in the shared SQLite database, so page views do not wait for provider connections. SMTP checks the configured connection, TLS, and authentication without sending a message.

The MQTT status uses a received notification as evidence of activity and only probes the broker and subscription when no notification has arrived in the last two hours. The Open Data check first looks for a METAR row dated today; when one exists, it does not call the API. If no row exists, it makes a lightweight authenticated files-list request no more than once every 15 minutes, subject to the shared KNMI API quota. Missing credentials are shown as not configured, and provider error details and credentials are not exposed on the page.

### Build and run the image directly

1. Clone the repository
2. run docker build . -t skydive-theorie
3. Run docker run -p 5000:5000 skydive-theorie

### Visit the currently live build
1. visit www.skydive-theorie.nl
