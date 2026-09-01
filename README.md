# skydive-theorie
Containerized e-learning website for the Dutch KNVVL A &amp; B theoretical exam.

## Overview
This website helps you prepare for the KNVVL theory exams required to obtain your skydiving license (A or B brevet). It provides:

- Practice exams with randomized questions
- Topic and subtopic filtering for focused study
- Instant feedback on answers and overall performance
- Learning resources page
- Contact form for inquiries

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

### Option 1: Build the container and run locally

1. Clone the repository
2. run docker build . -t skydive-theorie
3. Run docker run -p 5000:5000 skydive-theorie

### Option 2: Visit the website and use the currently live build
1. visit www.skydive-theorie.nl




