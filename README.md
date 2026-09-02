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
- **Database**: SQLite (`vragen.db`)
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

The SQLite `vragen.db` file is immutable versioned application content and is baked into each image. Do not bind-mount a host database over `/data/vragen.db`; release a new image when question content changes.

### KNMI METAR configuration

The `/metar` page calls KNMI from the Flask server. Configure the credential and endpoint through deployment environment variables; the API key is never sent to the browser:

```powershell
$env:KNMI_API_KEY = "your-rotated-knmi-key"
$env:KNMI_METAR_URL = "https://api.dataportal.nl/v1/knmi/metar/{airport}"
docker compose up --build -d
```

The endpoint must include `{airport}`, which is replaced with the validated Dutch ICAO code. METAR data is cached per airport and UTC day. Do not commit API keys to `.env` or source files, and rotate the key included in any public request.

### Build and run the image directly

1. Clone the repository
2. run docker build . -t skydive-theorie
3. Run docker run -p 5000:5000 skydive-theorie

### Visit the currently live build
1. visit www.skydive-theorie.nl




