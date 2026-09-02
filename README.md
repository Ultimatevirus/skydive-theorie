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

### Option 1: Run with Docker Compose and Traefik

Docker Compose starts both the Flask application and Traefik. The application is not exposed directly; Traefik receives the request and forwards it to Gunicorn over the internal Docker network. HTTP traffic is redirected to HTTPS. Let’s Encrypt is enabled only when `TRAEFIK_CERT_RESOLVER=letsencrypt` is configured for a real public domain.

For a real public domain, point DNS at the server, copy `.env.example` to `.env`, and set the hostname and Let's Encrypt email in `.env`:

```powershell
Copy-Item .env.example .env
notepad .env
docker compose up --build -d
```

The production defaults are ports 80 and 443. For local testing, the defaults use `skydive-theorie.localhost` and Traefik’s development certificate, so your browser will show a certificate warning:

```powershell
Copy-Item .env.example .env
$env:TRAEFIK_HOST = "skydive-theorie.localhost"
$env:TRAEFIK_HTTP_PORT = "8080"
$env:TRAEFIK_HTTPS_PORT = "8443"
$env:TRAEFIK_CERT_RESOLVER = ""
docker compose up --build -d
```

Open `https://skydive-theorie.localhost:8443` in your browser. The Docker socket is mounted read-only so Traefik can discover the application container. The Traefik image and Docker API version are configured for current Docker Desktop releases. Certificates are stored in the named `letsencrypt` volume and survive container recreation.

The SQLite database is mounted from `vragen.db`, so the existing question data remains available when containers are recreated.

### KNMI METAR configuration

The `/metar` page calls KNMI from the Flask server. Configure the credential and endpoint through deployment environment variables; the API key is never sent to the browser:

```powershell
$env:KNMI_API_KEY = "your-rotated-knmi-key"
$env:KNMI_METAR_URL = "https://api.dataportal.nl/v1/knmi/metar/{airport}"
docker compose up --build -d
```

The endpoint must include `{airport}`, which is replaced with the validated Dutch ICAO code. METAR data is cached per airport and UTC day. Do not commit API keys to `.env` or source files, and rotate the key included in any public request.

### Option 2: Build the container and run locally

1. Clone the repository
2. run docker build . -t skydive-theorie
3. Run docker run -p 5000:5000 skydive-theorie

### Option 2: Visit the website and use the currently live build
1. visit www.skydive-theorie.nl




