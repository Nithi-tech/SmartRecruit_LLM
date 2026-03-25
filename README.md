# Station S SmartRecruit

Station S SmartRecruit is an AI-assisted recruitment platform that helps teams manage job postings, evaluate candidates, and run structured interview workflows with automated scoring and reporting.

## Overview

The application supports two core user roles:

- Recruiters can create job posts, review applicants, run AI-assisted interview rounds, and analyze candidate performance.
- Applicants can discover roles, submit applications, answer interview questions, and receive feedback.

The platform combines Flask-based web workflows with NLP-powered components for interview generation and response evaluation.

## Key Features

- Role-based authentication and dashboards
- Job posting and lifecycle management
- CV upload and candidate profile processing
- AI-generated interview questions aligned to role requirements
- Candidate response review and round-based scoring
- Shortlist reporting and recruiter insights

## Tech Stack

- Backend: Flask, Flask-SQLAlchemy, Flask-Session, Flask-Migrate
- Data: SQLite (default), MongoDB (application data)
- AI/NLP: Transformers, Sentence Transformers, Hugging Face Inference API
- Frontend: HTML, CSS, JavaScript (Jinja templates)

## Project Structure

```text
SmartRecruit_LLM/
  app/
    templates/
    static/
    routes.py
    models.py
    pipeline.py
  create_db.py
  run.py
  requirements.txt
```

## Prerequisites

- Python 3.11+
- MongoDB running locally (default URI: `mongodb://localhost:27017/`)
- `pip` for dependency installation

## Quick Start

1. Clone the repository:

```bash
git clone https://github.com/Nithi-tech/SmartRecruit_LLM.git
cd SmartRecruit_LLM
```

2. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Configure environment variables in a `.env` file:

```env
SECRET_KEY=change-me
DATABASE_URL=sqlite:///site.db
API_TOKEN=your_huggingface_token
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@example.com
SMTP_PASSWORD=your_password_or_app_password
SMTP_FROM_EMAIL=your_email@example.com
SMTP_USE_TLS=true
```

5. Initialize the database schema (optional helper script):

```bash
python create_db.py
```

6. Run the application:

```bash
python run.py
```

The app will be available at `http://127.0.0.1:5000`.

## Development Notes

- Uploaded files are stored under `app/static/uploads/`.
- Session files are stored in `flask_session/` during local execution.
- Default database is SQLite, but can be changed via `DATABASE_URL`.

## Contributing

1. Fork the repository.
2. Create a feature branch.
3. Commit your changes with clear messages.
4. Open a pull request.

## License

This repository is distributed under the license terms defined by the project maintainers.