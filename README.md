# skydive-theorie
Containerized e-learning website for the Dutch KNVVL A &amp; B theoretical exam.

## Overview
This website helps you prepare for the KNVVL theory exams required to obtain your skydiving license (A or B brevet). It provides:

- Practice exams with randomized questions
- Topic and subtopic filtering for focused study
- Instant feedback on answers and overall performance
- Learning resources page
- Contact form for inquiries
- Daily METAR practice using KNMI observations

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

### Production deployment with Docker Compose and Traefik

Point DNS at the server and install the checked-in `compose.yml` and `compose.production.yml` files in the deployment directory. Create a protected `.env` containing a long random `SECRET_KEY`, the public `TRAEFIK_HOST`, `ACME_EMAIL`, and an immutable image reference:

```dotenv
APP_IMAGE=ghcr.io/ultimatevirus/skydive-theorie:COMMIT_SHA
SECRET_KEY=replace-with-a-long-random-secret
TRAEFIK_HOST=theorie.example.com
ACME_EMAIL=admin@example.com
TRAEFIK_HTTP_PORT=80
TRAEFIK_HTTPS_PORT=443
```

Start the production stack with:

```powershell
docker compose -f compose.yml -f compose.production.yml pull
docker compose -f compose.yml -f compose.production.yml up -d --remove-orphans
docker compose -f compose.yml -f compose.production.yml ps
```

Only Traefik publishes ports `80` and `443`; Gunicorn remains private to the Docker network. HTTP redirects to HTTPS, and Let's Encrypt state is stored in the named `letsencrypt` volume. To roll back, change `APP_IMAGE` to a previous commit tag and rerun `pull` and `up`.

The SQLite `data.db` file contains versioned application content and the daily METAR cache. It is baked into each image. Do not bind-mount a host database over `/data/data.db`; release a new image when question content changes.

### KNMI METAR configuration

The application starts a background METAR refresh from the Gunicorn master. It fetches every unique Dutch airport immediately after startup and refreshes them again at each UTC calendar day, before a user opens the `/metar` page. The shared SQLite limiter allows at most 3 airport METAR fetches per rolling minute, including on-demand requests. Failed airports are retried by the next refresh pass.

Configure the credential and endpoint through deployment environment variables; the API key is never sent to the browser:

```powershell
$env:KNMI_API_KEY = "your-rotated-knmi-key"
$env:KNMI_OPEN_DATA_URL = "https://api.dataplatform.knmi.nl/open-data"
docker compose up --build -d
```

The app uses the `metar` dataset, version `1.0`, and retrieves the newest XML file for each airport on the current UTC day. METAR data is stored in the SQLite `metar` table and reused per airport and UTC day; same-day fetch claims prevent duplicate API calls across Gunicorn processes. The image contains the initial database, but `/data/data.db` is ephemeral unless a deployment explicitly mounts persistent storage. Do not commit API keys to `.env` or source files, and rotate any key included in a public request.

### Build and run the image directly

1. Clone the repository
2. run docker build . -t skydive-theorie
3. Run docker run -p 5000:5000 skydive-theorie

### Visit the currently live build
1. visit www.skydive-theorie.nl




