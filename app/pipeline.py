import json
import logging
import re
import smtplib
import zipfile
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import pdfplumber  # type: ignore

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

from . import db
from .models import (
    ATSResult,
    ApplicantPipelineState,
    Application,
    EmailEvent,
    InterviewRoundConfig,
    Job,
    JobConfig,
    RoundEvaluation,
    User,
)


@dataclass
class ScoreResult:
    ats_score: float
    score_breakdown: dict[str, float]
    matched_keywords: list[str]
    missing_keywords: list[str]
    experience_summary: str
    shortlist_eligible: bool
    shortlist_reason: str


def _split_lines(raw_value: str) -> list[str]:
    return [item.strip() for item in (raw_value or "").splitlines() if item.strip()]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _extract_years_of_experience(text: str) -> float:
    normalized = _normalize_text(text)
    values = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|year|yrs|yr)", normalized):
        try:
            values.append(float(match.group(1)))
        except ValueError:
            continue
    return max(values) if values else 0.0


def _extract_email(text: str) -> str:
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return match.group(0) if match else ""


def _extract_phone(text: str) -> str:
    match = re.search(r"(?:\+?\d[\d\-\s]{8,}\d)", text)
    return match.group(0) if match else ""


def _extract_name(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0][:120] if lines else "Unknown"


def _extract_pdf_text(path: str) -> str:
    if fitz is not None:
        try:
            doc = fitz.open(path)
            try:
                chunks = [page.get_text("text") for page in doc]
                text = "\n".join(chunks).strip()
                if text:
                    return text
            finally:
                doc.close()
        except Exception:
            pass

    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
            if text:
                return text
    except Exception:
        pass

    # Final fallback for malformed files with a .pdf extension.
    with open(path, 'r', encoding='utf-8', errors='ignore') as file_obj:
        return file_obj.read()


def _extract_docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as archive:
        with archive.open("word/document.xml") as xml_file:
            xml_data = xml_file.read().decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", " ", xml_data)
    return re.sub(r"\s+", " ", text)


def parse_resume_to_json(cv_path: str) -> dict[str, Any]:
    if cv_path.lower().endswith(".pdf"):
        full_text = _extract_pdf_text(cv_path)
    elif cv_path.lower().endswith(".docx"):
        full_text = _extract_docx_text(cv_path)
    else:
        raise ValueError("Unsupported CV format. Use PDF or DOCX.")

    blocks = [line.strip() for line in full_text.splitlines() if line.strip()]
    normalized = _normalize_text(full_text)

    section_map: dict[str, list[str]] = {
        "education_history": [],
        "work_experience": [],
        "skills": [],
        "certifications_courses": [],
        "projects": [],
        "achievements_awards": [],
        "languages_known": [],
    }

    markers = {
        "education": "education_history",
        "experience": "work_experience",
        "skills": "skills",
        "certification": "certifications_courses",
        "course": "certifications_courses",
        "project": "projects",
        "achievement": "achievements_awards",
        "award": "achievements_awards",
        "language": "languages_known",
    }

    current_key = None
    for line in blocks:
        lowered = line.lower()
        for marker, target in markers.items():
            if marker in lowered and len(line) < 70:
                current_key = target
                break
        else:
            if current_key:
                section_map[current_key].append(line)

    return {
        "full_name": _extract_name(full_text),
        "contact_details": {
            "email": _extract_email(full_text),
            "phone": _extract_phone(full_text),
        },
        "education_history": section_map["education_history"],
        "work_experience": section_map["work_experience"],
        "skills": section_map["skills"],
        "certifications_courses": section_map["certifications_courses"],
        "projects": section_map["projects"],
        "achievements_awards": section_map["achievements_awards"],
        "languages_known": section_map["languages_known"],
        "raw_text": normalized,
    }


def _parse_required_years(filters: list[str]) -> float:
    for item in filters:
        match = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|year|yrs|yr)", item.lower())
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return 0.0


def _keyword_match_score(text: str, required: list[str], preferred: list[str]) -> tuple[float, list[str], list[str]]:
    text_norm = _normalize_text(text)
    required = [item.lower() for item in required if item]
    preferred = [item.lower() for item in preferred if item]

    matched_required = [kw for kw in required if kw in text_norm]
    matched_preferred = [kw for kw in preferred if kw in text_norm]

    req_score = 0.0
    pref_score = 0.0

    if required:
        req_score = (len(matched_required) / len(required)) * 25.0
    if preferred:
        pref_score = (len(matched_preferred) / len(preferred)) * 5.0

    missing = [kw for kw in required + preferred if kw not in text_norm]
    return min(30.0, req_score + pref_score), matched_required + matched_preferred, missing


def _experience_score(text: str, mandatory_filters: list[str]) -> tuple[float, str]:
    years_in_resume = _extract_years_of_experience(text)
    required_years = _parse_required_years(mandatory_filters)

    domain_terms = ["developer", "engineer", "analyst", "intern", "manager", "scientist"]
    domain_hits = sum(1 for term in domain_terms if term in text)
    domain_score = min(15.0, float(domain_hits) * 3.0)

    if required_years <= 0:
        years_score = min(10.0, years_in_resume * 2.0)
    else:
        years_score = min(10.0, (years_in_resume / required_years) * 10.0)

    summary = f"{years_in_resume:.1f} years detected from resume text"
    return min(25.0, domain_score + years_score), summary


def _education_score(parsed_resume: dict[str, Any], required_qualifications: list[str]) -> float:
    education_blob = _normalize_text(" ".join(parsed_resume.get("education_history", [])))
    required_blob = _normalize_text(" ".join(required_qualifications))

    degree_keywords = ["b.tech", "btech", "b.e", "be", "b.sc", "m.sc", "m.tech", "bachelor", "master", "phd"]
    degree_present = any(kw in education_blob for kw in degree_keywords)
    relevance = 1.0 if required_blob and any(tok in education_blob for tok in required_blob.split()) else 0.0

    degree_score = 10.0 if degree_present else 0.0
    fit_score = 5.0 * relevance
    return min(15.0, degree_score + fit_score)


def _project_score(parsed_resume: dict[str, Any], tech_stack: list[str]) -> float:
    projects_text = _normalize_text(" ".join(parsed_resume.get("projects", [])))
    if not projects_text:
        return 0.0

    stack = [item.lower() for item in tech_stack if item]
    if not stack:
        return 8.0

    hits = sum(1 for item in stack if item in projects_text)
    return min(15.0, (hits / len(stack)) * 15.0)


def _certification_score(parsed_resume: dict[str, Any]) -> float:
    certs = parsed_resume.get("certifications_courses", []) or []
    awards = parsed_resume.get("achievements_awards", []) or []
    total = len(certs) + len(awards)
    return min(10.0, float(total) * 2.0)


def _resume_quality_score(parsed_resume: dict[str, Any]) -> float:
    sections = [
        "education_history",
        "work_experience",
        "skills",
        "projects",
        "certifications_courses",
    ]
    present = sum(1 for section in sections if parsed_resume.get(section))
    return min(5.0, float(present))


def score_resume_against_job(job: Job, config: JobConfig, parsed_resume: dict[str, Any]) -> ScoreResult:
    raw_text = parsed_resume.get("raw_text", "")

    keyword_score, matched_keywords, missing_keywords = _keyword_match_score(
        raw_text,
        config.required_qualifications,
        config.preferred_qualifications + config.tech_stack,
    )

    exp_score, exp_summary = _experience_score(raw_text, config.mandatory_filters)
    edu_score = _education_score(parsed_resume, config.required_qualifications)
    proj_score = _project_score(parsed_resume, config.tech_stack)
    cert_score = _certification_score(parsed_resume)
    quality_score = _resume_quality_score(parsed_resume)

    total_score = round(keyword_score + exp_score + edu_score + proj_score + cert_score + quality_score, 2)
    shortlist_eligible = total_score >= float(config.min_ats_threshold)
    shortlist_reason = (
        "Score above threshold and passed deterministic rubric checks"
        if shortlist_eligible
        else "Below minimum ATS threshold"
    )

    return ScoreResult(
        ats_score=total_score,
        score_breakdown={
            "keyword_match": round(keyword_score, 2),
            "experience_relevance": round(exp_score, 2),
            "education_fit": round(edu_score, 2),
            "project_relevance": round(proj_score, 2),
            "certifications": round(cert_score, 2),
            "resume_quality": round(quality_score, 2),
        },
        matched_keywords=matched_keywords,
        missing_keywords=missing_keywords,
        experience_summary=exp_summary,
        shortlist_eligible=shortlist_eligible,
        shortlist_reason=shortlist_reason,
    )


def upsert_pipeline_state(application: Application, state: str) -> None:
    current = ApplicantPipelineState.query.filter_by(application_id=application.id).first()
    if current is None:
        current = ApplicantPipelineState(
            application_id=application.id,
            applicant_id=application.user_id,
            job_id=application.job_id,
            state=state,
            updated_at=datetime.utcnow(),
        )
        db.session.add(current)
    else:
        current.state = state
        current.updated_at = datetime.utcnow()


def log_email_event(
    *,
    applicant: User,
    job: Job,
    application_id: int | None,
    event_type: str,
    subject: str,
    body: str,
) -> None:
    event = EmailEvent(
        applicant_id=applicant.id,
        job_id=job.id,
        application_id=application_id,
        event_type=event_type,
        subject=subject,
        body=body,
        recipient=applicant.email,
        status="logged",
    )
    db.session.add(event)
    logging.info("Email event logged: %s -> %s", event_type, applicant.email)


def _send_smtp_email(subject: str, body: str, recipient: str, reply_to: str | None = None) -> tuple[bool, str | None]:
    from flask import current_app

    smtp_host = current_app.config.get('SMTP_HOST', '')
    smtp_port = int(current_app.config.get('SMTP_PORT', 587))
    smtp_username = current_app.config.get('SMTP_USERNAME', '')
    smtp_password = current_app.config.get('SMTP_PASSWORD', '')
    smtp_from_email = current_app.config.get('SMTP_FROM_EMAIL', smtp_username)
    use_tls = bool(current_app.config.get('SMTP_USE_TLS', True))

    if not smtp_host or not smtp_from_email:
        return False, 'SMTP host/from-email is not configured'

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = smtp_from_email
    message['To'] = recipient
    if reply_to:
        message['Reply-To'] = reply_to
    message.set_content(body)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            if use_tls:
                server.starttls()
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            server.send_message(message)
        return True, None
    except Exception as exc:
        return False, str(exc)


def dispatch_email(
    *,
    applicant: User,
    job: Job,
    application_id: int | None,
    event_type: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
) -> None:
    event = EmailEvent(
        applicant_id=applicant.id,
        job_id=job.id,
        application_id=application_id,
        event_type=event_type,
        subject=subject,
        body=body,
        recipient=applicant.email,
        status='pending',
    )
    db.session.add(event)
    db.session.flush()

    sent, error_message = _send_smtp_email(subject, body, applicant.email, reply_to=reply_to)
    event.status = 'sent' if sent else f'failed: {error_message or "unknown error"}'
    logging.info("Email dispatch %s for %s -> %s", 'succeeded' if sent else 'failed', event_type, applicant.email)


def send_acknowledgement_email(applicant: User, job: Job, config: JobConfig) -> None:
    subject = f"We received your application - {job.title} at {config.company_display_name}"
    body = (
        f"Hi {applicant.first_name},\n\n"
        f"Thank you for applying for the {job.title} role at {config.company_display_name}.\n"
        "We've received your application and our team will review your resume shortly.\n"
        "You'll hear from us regarding the next steps within 3 business days.\n\n"
        "Best of luck!\n"
        f"- {config.company_display_name} Recruitment Team"
    )
    dispatch_email(
        applicant=applicant,
        job=job,
        application_id=None,
        event_type="ACKNOWLEDGEMENT",
        subject=subject,
        body=body,
        reply_to=config.reply_to_email,
    )


def _passes_mandatory_filters(score: ATSResult, config: JobConfig) -> tuple[bool, str]:
    normalized_matches = {kw.lower() for kw in (score.matched_keywords or [])}
    years_required = _parse_required_years(config.mandatory_filters)
    years_detected = _extract_years_of_experience(score.parsed_resume.get("raw_text", ""))

    for rule in config.mandatory_filters:
        lowered = rule.lower()
        if "must have" in lowered:
            required_term = lowered.replace("must have", "").strip()
            if required_term and required_term not in normalized_matches and required_term not in score.parsed_resume.get("raw_text", ""):
                return False, f"Missing mandatory criterion: {rule}"

    if years_required > 0 and years_detected < years_required:
        return False, f"Required {years_required}+ years, found {years_detected:.1f}"

    return True, "Passed mandatory filters"


def run_shortlisting(job: Job, config: JobConfig) -> dict[str, Any]:
    applications = Application.query.filter_by(job_id=job.id).all()
    if not applications:
        return {"received": 0, "shortlisted": 0, "rejected": 0, "ties_included": False}

    ats_rows = ATSResult.query.filter_by(job_id=job.id).all()
    if not ats_rows:
        return {"received": len(applications), "shortlisted": 0, "rejected": len(applications), "ties_included": False}

    eligible: list[ATSResult] = []
    rejected: list[tuple[ATSResult, str]] = []

    for row in ats_rows:
        if row.ats_score < config.min_ats_threshold:
            rejected.append((row, "Below ATS threshold"))
            continue

        ok, reason = _passes_mandatory_filters(row, config)
        if not ok:
            rejected.append((row, reason))
            continue

        eligible.append(row)

    eligible.sort(key=lambda item: item.ats_score, reverse=True)

    target = 0
    if config.shortlist_mode == "percentage":
        target = max(1, int(round((config.shortlist_value / 100.0) * len(eligible))))
    else:
        target = int(config.shortlist_value)

    shortlisted = eligible[:target]
    ties_included = False
    if target > 0 and len(eligible) > target:
        cut_score = eligible[target - 1].ats_score
        for item in eligible[target:]:
            if item.ats_score == cut_score:
                shortlisted.append(item)
                ties_included = True

    shortlisted_ids = {item.application_id for item in shortlisted}

    for row in ats_rows:
        app = Application.query.get(row.application_id)
        applicant = User.query.get(row.applicant_id)
        if app is None or applicant is None:
            continue

        if row.application_id in shortlisted_ids:
            app.status = "Shortlisted"
            upsert_pipeline_state(app, "SHORTLISTED")

            first_round = InterviewRoundConfig.query.filter_by(job_id=job.id, round_number=1).first()
            if first_round is not None:
                upsert_pipeline_state(app, "ROUND_1_SCHEDULED")

            round_type = first_round.round_type if first_round else "Next Stage"
            round_name = first_round.round_name if first_round else "Round 1"
            schedule = first_round.schedule_window if first_round else "To be scheduled"
            duration = first_round.duration_minutes if first_round else 30
            focus = ", ".join(first_round.focus_areas or ["Job-related topics"]) if first_round else "Job-related topics"

            subject = f"Congratulations! You've been shortlisted - {job.title} at {config.company_display_name}"
            body = (
                f"Hi {applicant.first_name},\n\n"
                f"You have been shortlisted for {job.title}.\n"
                f"Round: {round_name} (Round 1)\n"
                f"Type: {round_type}\n"
                f"Schedule: {schedule}\n"
                f"Duration: {duration} minutes\n"
                f"Focus: {focus}\n\n"
                f"Reply to: {config.reply_to_email}\n"
                f"- {config.company_display_name} Recruitment Team"
            )
            dispatch_email(
                applicant=applicant,
                job=job,
                application_id=app.id,
                event_type="SHORTLISTED",
                subject=subject,
                body=body,
                reply_to=config.reply_to_email,
            )
        else:
            app.status = "Rejected"
            upsert_pipeline_state(app, "REJECTED")

            if config.send_rejection_emails:
                subject = f"Update on your application - {job.title} at {config.company_display_name}"
                body = (
                    f"Hi {applicant.first_name},\n\n"
                    "Thank you for your interest. After review, we will not be moving forward at this time.\n"
                    "We encourage you to apply for future openings.\n\n"
                    f"- {config.company_display_name} Recruitment Team"
                )
                dispatch_email(
                    applicant=applicant,
                    job=job,
                    application_id=app.id,
                    event_type="REJECTED",
                    subject=subject,
                    body=body,
                    reply_to=config.reply_to_email,
                )

    db.session.commit()

    return {
        "received": len(applications),
        "shortlisted": len(shortlisted_ids),
        "rejected": len(applications) - len(shortlisted_ids),
        "ties_included": ties_included,
    }


def should_auto_shortlist(job: Job, config: JobConfig) -> bool:
    total = Application.query.filter_by(job_id=job.id).count()
    deadline_reached = datetime.utcnow() >= config.application_deadline
    expected_reached = total >= max(1, config.expected_applicants)
    return deadline_reached or expected_reached


def build_funnel_summary(job: Job) -> str:
    rounds = InterviewRoundConfig.query.filter_by(job_id=job.id).order_by(InterviewRoundConfig.round_number.asc()).all()
    if not rounds:
        return "Applicants -> shortlisted -> hired"

    parts = ["Applicants"]
    for round_cfg in rounds:
        if round_cfg.advance_count:
            parts.append(f"after Round {round_cfg.round_number}: {round_cfg.advance_count}")
    parts.append("Hired")
    return " -> ".join(parts)


def complete_round_and_advance(job: Job, round_number: int, evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    round_cfg = InterviewRoundConfig.query.filter_by(job_id=job.id, round_number=round_number).first()
    if round_cfg is None:
        raise ValueError(f"Round {round_number} not configured")
    config = JobConfig.query.filter_by(job_id=job.id, confirmed=True).first()

    for item in evaluations:
        app_id = int(item["application_id"])
        application = Application.query.get_or_404(app_id)
        score = float(item["round_score"])

        record = RoundEvaluation.query.filter_by(
            application_id=application.id,
            job_id=job.id,
            round_number=round_number,
        ).first()

        if record is None:
            record = RoundEvaluation(
                application_id=application.id,
                applicant_id=application.user_id,
                job_id=job.id,
                round_number=round_number,
                round_score=score,
                evaluation_data=item.get("evaluation", {}),
            )
            db.session.add(record)
        else:
            record.round_score = score
            record.evaluation_data = item.get("evaluation", {})
            record.completed_at = datetime.utcnow()

        upsert_pipeline_state(application, f"ROUND_{round_number}_COMPLETED")

    db.session.flush()

    ranked = (
        RoundEvaluation.query.filter_by(job_id=job.id, round_number=round_number)
        .order_by(RoundEvaluation.round_score.desc())
        .all()
    )

    advance_count = round_cfg.advance_count or 0
    selected = ranked[:advance_count] if advance_count > 0 else ranked
    selected_ids = {item.application_id for item in selected}

    next_round = InterviewRoundConfig.query.filter_by(job_id=job.id, round_number=round_number + 1).first()

    for evaluation in ranked:
        application = Application.query.get(evaluation.application_id)
        applicant = User.query.get(evaluation.applicant_id)
        if application is None or applicant is None:
            continue

        if evaluation.application_id in selected_ids:
            if next_round is None:
                upsert_pipeline_state(application, "OFFER")
                application.status = "Recommended"
                if config is not None:
                    dispatch_email(
                        applicant=applicant,
                        job=job,
                        application_id=application.id,
                        event_type='FINAL_RECOMMENDATION',
                        subject=f"Final update - {job.title} at {config.company_display_name}",
                        body=(
                            f"Hi {applicant.first_name},\n\n"
                            f"Congratulations! You completed the final round for {job.title}.\n"
                            "Our team will contact you with the offer process shortly.\n\n"
                            f"- {config.company_display_name} Recruitment Team"
                        ),
                        reply_to=config.reply_to_email,
                    )
            else:
                upsert_pipeline_state(application, f"ROUND_{next_round.round_number}_SCHEDULED")
                application.status = "Advanced"
                if config is not None:
                    dispatch_email(
                        applicant=applicant,
                        job=job,
                        application_id=application.id,
                        event_type='ROUND_ADVANCED',
                        subject=f"You advanced to Round {next_round.round_number} - {job.title}",
                        body=(
                            f"Hi {applicant.first_name},\n\n"
                            f"You advanced to {next_round.round_name} (Round {next_round.round_number}) for {job.title}.\n"
                            f"Type: {next_round.round_type}\n"
                            f"Duration: {next_round.duration_minutes} minutes\n"
                            f"Schedule: {next_round.schedule_window or 'to be scheduled dynamically'}\n\n"
                            f"- {config.company_display_name} Recruitment Team"
                        ),
                        reply_to=config.reply_to_email,
                    )
        else:
            upsert_pipeline_state(application, "FINAL_REJECTION" if next_round is None else "ELIMINATED")
            application.status = "Rejected"
            if config is not None and config.send_rejection_emails:
                dispatch_email(
                    applicant=applicant,
                    job=job,
                    application_id=application.id,
                    event_type='ROUND_ELIMINATED',
                    subject=f"Update on your application - {job.title}",
                    body=(
                        f"Hi {applicant.first_name},\n\n"
                        "Thank you for participating in this hiring round."
                        " We will not be moving forward with your profile at this stage.\n\n"
                        f"- {config.company_display_name} Recruitment Team"
                    ),
                    reply_to=config.reply_to_email,
                )

    db.session.commit()

    return {
        "round": round_number,
        "evaluated": len(ranked),
        "advanced": len(selected_ids),
        "eliminated": len(ranked) - len(selected_ids),
    }


def parse_rounds_payload(payload: str) -> list[dict[str, Any]]:
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Round configuration JSON is invalid") from exc

    if not isinstance(parsed, list):
        raise ValueError("Round configuration must be a JSON array")

    cleaned = []
    for index, row in enumerate(parsed, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Round item #{index} must be an object")
        cleaned.append(
            {
                "round_number": int(row.get("round_number", index)),
                "round_name": str(row.get("round_name", f"Round {index}")).strip(),
                "round_type": str(row.get("round_type", "AI Interview")).strip(),
                "duration_minutes": int(row.get("duration_minutes", 30)),
                "focus_areas": _split_lines(str(row.get("focus_areas", "")))
                if isinstance(row.get("focus_areas"), str)
                else [str(item).strip() for item in row.get("focus_areas", []) if str(item).strip()],
                "advance_count": int(row["advance_count"]) if row.get("advance_count") not in (None, "") else None,
                "evaluation_rubric": _split_lines(str(row.get("evaluation_rubric", "")))
                if isinstance(row.get("evaluation_rubric"), str)
                else [str(item).strip() for item in row.get("evaluation_rubric", []) if str(item).strip()],
                "schedule_window": str(row.get("schedule_window", "to be scheduled dynamically")).strip(),
            }
        )
    return cleaned
