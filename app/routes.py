from flask import Blueprint, render_template, redirect, url_for, flash, request, session, g, current_app, abort, jsonify
from markdown import markdown
from werkzeug.utils import secure_filename
from functools import wraps
from datetime import datetime
from sqlalchemy import func
from pymongo.errors import PyMongoError
import os
import pdfplumber  # type: ignore
import logging
import time
import json

from . import db, applications_collection
from .models import (
    User,
    Job,
    Application,
    ATSResult,
    JobConfig,
    InterviewRoundConfig,
    RoundEvaluation,
    ApplicantPipelineState,
)
from .pipeline import (
    build_funnel_summary,
    complete_round_and_advance,
    parse_resume_to_json,
    parse_rounds_payload,
    run_shortlisting,
    score_resume_against_job,
    send_acknowledgement_email,
    should_auto_shortlist,
    upsert_pipeline_state,
)
from .utils import allowed_file, evaluate_cv, extract_score, generate_interview_questions, generate_feedback, convert_keys_to_strings

main = Blueprint('main', __name__)


def mongo_find_one(query):
    try:
        return applications_collection.find_one(query)
    except PyMongoError as e:
        logging.warning(f"MongoDB find_one unavailable: {e}")
        return None


def mongo_insert_one(document):
    try:
        applications_collection.insert_one(document)
        return True
    except PyMongoError as e:
        logging.warning(f"MongoDB insert_one unavailable: {e}")
        return False


def parse_lines_field(raw_value):
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.splitlines() if item.strip()]

@main.before_app_request
def load_user():
    user_id = session.get('user_id')
    if user_id:
        g.user = User.query.get(user_id)
    else:
        g.user = None

@main.context_processor
def inject_user():
    return {'user': g.user}


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash('You need to sign in first.', 'danger')
            return redirect(url_for('main.auth'))
        return view(*args, **kwargs)
    return wrapped_view


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped_view(*args, **kwargs):
            if not g.user.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)
        return wrapped_view
    return decorator

@main.route('/')
@login_required
def home():
    if g.user.has_role('recruiter', 'both'):
        return redirect(url_for('main.dashboard'))
    return redirect(url_for('main.applicant_dashboard'))


@main.route('/applicant_dashboard')
@role_required('applicant', 'both')
def applicant_dashboard():
    jobs = Job.query.filter(Job.user_id != g.user.id).order_by(Job.date_posted.desc()).all()
    recent_applications = Application.query.filter_by(user_id=g.user.id).order_by(Application.timestamp.desc()).limit(5).all()

    return render_template(
        'applicant_dashboard.html',
        jobs=jobs,
        recent_applications=recent_applications
    )


@main.route('/jobs')
@role_required('applicant', 'both')
def browse_jobs():
    jobs = Job.query.filter(Job.user_id != g.user.id).all()
    return render_template('snippet_career_list.html', jobs=jobs)

@main.route('/sign', methods=['GET', 'POST'])
def auth():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'signup':
            # Collect form data
            first_name = request.form['first_name']
            last_name = request.form['last_name']
            user_role = request.form.get('role', 'applicant').strip().lower()
            if user_role not in {'applicant', 'recruiter'}:
                user_role = 'applicant'

            company_name = request.form.get('company_name', '').strip()
            email = request.form['email'].strip().lower()
            phone_number = request.form['phone_number']
            birthday = request.form['birthday']
            password = request.form['password']
            confirm_password = request.form['confirm_password']

            if user_role == 'recruiter' and not company_name:
                flash('Company name is required for recruiter accounts.', 'danger')
                return redirect(url_for('main.auth'))

            if not company_name:
                company_name = 'Independent'

            # Validate password match
            if password != confirm_password:
                flash('Passwords do not match.', 'danger')
                return redirect(url_for('main.auth'))

            # Check if the email already exists
            existing_user = User.query.filter(
                func.lower(func.trim(User.email)) == email
            ).first()
            if existing_user:
                flash('An account with this email already exists.', 'danger')
                return redirect(url_for('main.auth'))

            # Create a new user
            user = User(
                first_name=first_name,
                last_name=last_name,
                company_name=company_name,
                email=email,
                phone_number=phone_number,
                birthday=birthday,
                role=user_role
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash('Signup successful! You can now sign in.', 'success')
            return redirect(url_for('main.auth'))

        elif action == 'signin':
            # Collect form data
            email = request.form['email'].strip().lower()
            password = request.form['password']

            # Check if the user exists
            user = User.query.filter(
                func.lower(func.trim(User.email)) == email
            ).first()
            if user and user.check_password(password):
                db.session.commit()
                session['user_id'] = user.id
                flash('Signin successful!', 'success')
                if user.has_role('recruiter', 'both'):
                    return redirect(url_for('main.dashboard'))
                return redirect(url_for('main.applicant_dashboard'))
            else:
                flash('Invalid email or password.', 'danger')
                return redirect(url_for('main.auth'))

    return render_template('sign.html')

@main.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('main.auth'))

@main.route('/create_job', methods=['GET', 'POST'])
@role_required('recruiter', 'both')
def create_job():
    if request.method == 'POST':
        title = request.form['title']
        location = request.form['location']
        description = request.form['description']
        salary = request.form['salary']
        department = request.form.get('department', '').strip()
        employment_type = request.form.get('employment_type', '').strip()
        work_mode = request.form.get('work_mode', '').strip()
        application_deadline_raw = request.form.get('application_deadline', '').strip()
        role_summary = request.form.get('role_summary', '').strip()
        expected_applicants_raw = request.form.get('expected_applicants', '').strip()
        shortlist_mode = request.form.get('shortlist_mode', 'count').strip()
        shortlist_value_raw = request.form.get('shortlist_value', '').strip()
        min_ats_threshold_raw = request.form.get('min_ats_threshold', '70').strip()
        notification_tone = request.form.get('notification_tone', 'Formal').strip().lower()
        company_display_name = request.form.get('company_display_name', g.user.company_name).strip()
        company_logo_url = request.form.get('company_logo_url', '').strip()
        reply_to_email = request.form.get('reply_to_email', g.user.email).strip().lower()
        send_rejection_emails = request.form.get('send_rejection_emails', 'yes').strip().lower() == 'yes'
        rejection_timing = request.form.get('rejection_timing', 'after_shortlisting').strip()
        confirm_publish = request.form.get('confirm_publish', '').strip()
        rounds_payload = request.form.get('rounds_payload', '').strip()

        required_fields = [
            department,
            employment_type,
            work_mode,
            application_deadline_raw,
            role_summary,
            expected_applicants_raw,
            shortlist_value_raw,
            min_ats_threshold_raw,
            company_display_name,
            reply_to_email,
        ]
        if not all(required_fields):
            flash('Please complete all required job configuration fields before publishing.', 'danger')
            return redirect(url_for('main.create_job'))

        if confirm_publish != 'CONFIRM':
            flash("Type CONFIRM to publish this job.", 'danger')
            return redirect(url_for('main.create_job'))

        try:
            deadline_dt = datetime.strptime(application_deadline_raw, '%Y-%m-%dT%H:%M')
            expected_applicants = int(expected_applicants_raw)
            shortlist_value = float(shortlist_value_raw)
            min_ats_threshold = float(min_ats_threshold_raw)
            rounds = parse_rounds_payload(rounds_payload)
        except ValueError as e:
            flash(f'Invalid job configuration values: {e}', 'danger')
            return redirect(url_for('main.create_job'))

        if not rounds:
            flash('Add at least one interview round configuration before publishing.', 'danger')
            return redirect(url_for('main.create_job'))

        new_job = Job(
            title=title,
            location=location,
            description=description,
            salary=salary,
            user_id=g.user.id
        )
        db.session.add(new_job)
        db.session.flush()

        config = JobConfig(
            job_id=new_job.id,
            department=department,
            employment_type=employment_type,
            work_mode=work_mode,
            location_display=location,
            application_deadline=deadline_dt,
            role_summary=role_summary,
            key_responsibilities=parse_lines_field(request.form.get('key_responsibilities', '')),
            required_qualifications=parse_lines_field(request.form.get('required_qualifications', '')),
            preferred_qualifications=parse_lines_field(request.form.get('preferred_qualifications', '')),
            tech_stack=parse_lines_field(request.form.get('tech_stack', '')),
            expected_applicants=expected_applicants,
            shortlist_mode=shortlist_mode,
            shortlist_value=shortlist_value,
            min_ats_threshold=min_ats_threshold,
            mandatory_filters=parse_lines_field(request.form.get('mandatory_filters', '')),
            preferred_filters=parse_lines_field(request.form.get('preferred_filters', '')),
            notification_tone=notification_tone,
            company_display_name=company_display_name,
            company_logo_url=company_logo_url,
            reply_to_email=reply_to_email,
            send_rejection_emails=send_rejection_emails,
            rejection_timing=rejection_timing,
            confirmed=True,
            published_at=datetime.utcnow(),
        )
        db.session.add(config)

        for round_row in rounds:
            db.session.add(
                InterviewRoundConfig(
                    job_id=new_job.id,
                    round_number=round_row['round_number'],
                    round_name=round_row['round_name'],
                    round_type=round_row['round_type'],
                    duration_minutes=round_row['duration_minutes'],
                    focus_areas=round_row['focus_areas'],
                    advance_count=round_row['advance_count'],
                    evaluation_rubric=round_row['evaluation_rubric'],
                    schedule_window=round_row['schedule_window'],
                )
            )

        db.session.commit()

        funnel = build_funnel_summary(new_job)
        flash(f'Job published. Funnel: {funnel}', 'success')
        return redirect(url_for('main.my_jobs'))

    return render_template('create_job.html')

@main.route('/my_jobs')
@role_required('recruiter', 'both')
def my_jobs():
    jobs = Job.query.filter_by(user_id=g.user.id).all()
    return render_template('my_jobs.html', jobs=jobs)

@main.route('/edit_job/<int:job_id>', methods=['GET', 'POST'])
@role_required('recruiter', 'both')
def edit_job(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    if request.method == 'POST':
        job.title = request.form['title']
        job.location = request.form['location']
        job.description = request.form['description']
        job.salary = request.form['salary']
        db.session.commit()
        flash('Job updated successfully!', 'success')
        return redirect(url_for('main.my_jobs'))

    return render_template('edit_job.html', job=job)

@main.route('/delete_job/<int:job_id>', methods=['POST'])
@role_required('recruiter', 'both')
def delete_job(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    db.session.delete(job)
    db.session.commit()
    flash('Job deleted successfully!', 'success')
    return redirect(url_for('main.my_jobs'))

@main.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user = User.query.get(g.user.id)
    
    if user is None:
        flash('User not found.', 'danger')
        return redirect(url_for('main.auth'))

    if request.method == 'POST':
        # Handle General Settings Form Submission
        if 'save_changes' in request.form:
            # Fetching and validating form data
            first_name = request.form.get('first_name')
            last_name = request.form.get('last_name')
            company_name = request.form.get('company_name')
            email = request.form.get('email', '').strip().lower()
            phone_number = request.form.get('phone_number')
            birthday = request.form.get('birthday')

            # Ensure no required fields are empty
            if not all([first_name, last_name, email, phone_number, birthday]):
                flash('All fields are required.', 'danger')
                return redirect(url_for('main.settings'))

            if user.has_role('recruiter', 'both') and not company_name:
                flash('Company name is required for recruiter accounts.', 'danger')
                return redirect(url_for('main.settings'))

            if not company_name:
                company_name = 'Independent'

            # Update user details
            user.first_name = first_name
            user.last_name = last_name
            user.company_name = company_name
            existing_user = User.query.filter(
                func.lower(func.trim(User.email)) == email,
                User.id != user.id
            ).first()
            if existing_user:
                flash('An account with this email already exists.', 'danger')
                return redirect(url_for('main.settings'))

            user.email = email
            user.phone_number = phone_number
            user.birthday = birthday

            # Handle Profile Photo Upload
            if 'profile_photo' in request.files:
                profile_photo = request.files['profile_photo']
                if profile_photo and allowed_file(profile_photo.filename, {'jpg', 'jpeg', 'png'}):
                    photo_filename = secure_filename(profile_photo.filename)
                    profile_photo.save(os.path.join(current_app.config['UPLOAD_FOLDER_PHOTOS'], photo_filename))
                    user.profile_photo = photo_filename

            # Commit changes to the database
            try:
                db.session.commit()
                flash('General settings updated successfully!', 'success')
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error updating settings: {e}")
                flash('An error occurred while updating your settings. Please try again.', 'danger')

            return redirect(url_for('main.settings'))

        # Handle CV Upload Form Submission
        if 'upload_cv' in request.form:
            if 'cv_file' in request.files:
                cv_file = request.files['cv_file']
                if cv_file and allowed_file(cv_file.filename, {'pdf', 'docx'}):
                    cv_filename = secure_filename(cv_file.filename)
                    cv_file.save(os.path.join(current_app.config['UPLOAD_FOLDER_CV'], cv_filename))
                    user.cv_file = cv_filename

            # Commit changes to the database
            try:
                db.session.commit()
                flash('CV uploaded successfully!', 'success')
            except Exception as e:
                db.session.rollback()
                logging.error(f"Error uploading CV: {e}")
                flash('An error occurred while uploading your CV. Please try again.', 'danger')

            return redirect(url_for('main.settings'))

    return render_template('settings.html', user=user)

@main.route('/job/<int:job_id>')
@login_required
def job_detail(job_id):
    job = Job.query.get_or_404(job_id)
    job.description = markdown(job.description)
    return render_template('job_detail.html', job=job)

@main.route('/apply/<int:job_id>', methods=['GET'])
@role_required('applicant', 'both')
def apply(job_id):
    job = Job.query.get_or_404(job_id)
    config = JobConfig.query.filter_by(job_id=job.id, confirmed=True).first()
    if config is None:
        flash('This job is not fully configured for automated hiring yet.', 'danger')
        return redirect(url_for('main.job_detail', job_id=job_id))

    existing_application_sqlite = Application.query.filter_by(user_id=g.user.id, job_id=job_id).first()
    existing_application_mongo = mongo_find_one({
        'user_id': str(g.user.id),
        'job_id': str(job_id)
    })

    if existing_application_sqlite or existing_application_mongo:
        flash('You have already applied for this job.', 'alert')
        return redirect(url_for('main.job_detail', job_id=job_id))

    if not g.user.cv_file:
        flash('Please upload your CV in settings before applying.', 'danger')
        return redirect(url_for('main.settings'))

    cv_path = os.path.join(current_app.config['UPLOAD_FOLDER_CV'], g.user.cv_file)
    if not os.path.isfile(cv_path):
        flash('CV file not found. Please upload again.', 'danger')
        return redirect(url_for('main.settings'))

    try:
        parsed_resume = parse_resume_to_json(cv_path)
    except Exception as e:
        logging.error(f"Failed to process CV: {e}")
        flash('Failed to process CV.', 'danger')
        return redirect(url_for('main.job_detail', job_id=job_id))

    score = score_resume_against_job(job, config, parsed_resume)

    new_application = Application(
        user_id=g.user.id,
        job_id=job_id,
        message=str(score.ats_score),
        timestamp=datetime.utcnow(),
        status='Applied'
    )
    db.session.add(new_application)
    db.session.flush()

    ats_row = ATSResult(
        application_id=new_application.id,
        applicant_id=g.user.id,
        job_id=job_id,
        ats_score=score.ats_score,
        score_breakdown=score.score_breakdown,
        matched_keywords=score.matched_keywords,
        missing_keywords=score.missing_keywords,
        experience_summary=score.experience_summary,
        shortlist_eligible=score.shortlist_eligible,
        shortlist_reason=score.shortlist_reason,
        parsed_resume=parsed_resume,
    )
    db.session.add(ats_row)
    upsert_pipeline_state(new_application, 'ATS_SCORED')
    send_acknowledgement_email(g.user, job, config)

    application_data = {
        'application_id': str(new_application.id),
        'user_id': str(g.user.id),
        'job_id': str(job_id),
        'ats_result': {
            'ats_score': score.ats_score,
            'score_breakdown': score.score_breakdown,
            'matched_keywords': score.matched_keywords,
            'missing_keywords': score.missing_keywords,
            'experience_summary': score.experience_summary,
            'shortlist_eligible': score.shortlist_eligible,
            'shortlist_reason': score.shortlist_reason,
        },
    }
    mongo_insert_one(application_data)

    db.session.commit()

    if should_auto_shortlist(job, config):
        report = run_shortlisting(job, config)
        flash(
            f"Shortlisting completed: {report['received']} received, {report['shortlisted']} shortlisted, {report['rejected']} rejected.",
            'info'
        )

    flash('Application submitted and ATS scored successfully!', 'success')
    return redirect(url_for('main.view_applications'))

@main.route('/interview_questions', methods=['GET', 'POST'])
@role_required('applicant', 'both')
def interview_questions():
    questions = session.get('questions')
    current_question = session.get('current_question', 0)
    responses = session.get('responses', {})

    if request.method == 'POST':
        response = request.form.get('response')
        if response:
            responses[str(current_question)] = response
            session['responses'] = responses
            current_question += 1
            session['current_question'] = current_question

            if current_question >= len(questions):
                return redirect(url_for('main.review_responses'))

    if current_question < len(questions):
        question = questions[current_question]
        return render_template('interview_questions.html', question_number=current_question + 1, question_text=question)
    else:
        return redirect(url_for('main.review_responses'))

@main.route('/review_responses')
@role_required('applicant', 'both')
def review_responses():
    return render_template('loading.html', next_url = url_for('main.generate_feedbacks'))

@main.route('/generate_feedbacks')
@role_required('applicant', 'both')
def generate_feedbacks():
    responses = session.get('responses', {})
    questions = session.get('questions', [])
    job_id = session.get('job_id')
    job = Job.query.get_or_404(job_id)
    similarity_score = session.get('similarity_score')

    feedback_list = []
    for idx, response in responses.items():
        question = questions[int(idx)]
        feedback = generate_feedback(question, response, job.description)
        score = extract_score(feedback)
        time.sleep(2)  
        feedback_list.append({
            'question': question,
            'response': response,
            'feedback': feedback,
            'score':score
        })

    new_application = Application(
        user_id=g.user.id,
        job_id=job_id,
        message=similarity_score,
        timestamp=datetime.utcnow(),
        status='Pending'
    )
    db.session.add(new_application)
    db.session.commit()

    application_data = {
        'application_id': str(new_application.id),
        'user_id': str(g.user.id),
        'job_id': str(job_id),
        'responses': convert_keys_to_strings(responses),
        'feedback': feedback_list
    }
    if not mongo_insert_one(application_data):
        flash('Application saved, but interview analytics storage is temporarily unavailable.', 'info')

    flash('Application submitted successfully!', 'success')
    return redirect(url_for('main.view_applications'))


@main.route('/job/<int:job_id>/run_shortlisting', methods=['POST'])
@role_required('recruiter', 'both')
def run_shortlisting_now(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    config = JobConfig.query.filter_by(job_id=job.id, confirmed=True).first()
    if config is None:
        flash('Job configuration not found.', 'danger')
        return redirect(url_for('main.my_jobs'))

    report = run_shortlisting(job, config)
    ties_message = ' Tie at cutoff included extra candidates.' if report['ties_included'] else ''
    flash(
        f"Shortlisting complete: {report['received']} received, {report['shortlisted']} shortlisted, {report['rejected']} rejected.{ties_message}",
        'success'
    )
    return redirect(url_for('main.view_candidates', job_id=job.id))


@main.route('/job/<int:job_id>/complete_round/<int:round_number>', methods=['POST'])
@role_required('recruiter', 'both')
def complete_round(job_id, round_number):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    payload = request.form.get('round_evaluations') or request.get_json(silent=True)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            flash('Invalid round evaluations payload.', 'danger')
            return redirect(url_for('main.view_candidates', job_id=job.id))

    if not isinstance(payload, list):
        flash('Round evaluations must be a JSON array.', 'danger')
        return redirect(url_for('main.view_candidates', job_id=job.id))

    try:
        report = complete_round_and_advance(job, round_number, payload)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('main.view_candidates', job_id=job.id))

    flash(
        f"Round {report['round']} processed: {report['advanced']} advanced, {report['eliminated']} eliminated.",
        'success'
    )
    return redirect(url_for('main.view_candidates', job_id=job.id))

@main.route('/view_applications')
@role_required('applicant', 'both')
def view_applications():
    applications = Application.query.filter_by(user_id=g.user.id).all()

    # Fetch job details for each application
    applications_list = []
    for app in applications:
        job = Job.query.get(app.job_id)
        applications_list.append({
            'id': app.id,
            'job_title': job.title if job else 'Unknown',
            'application_date': app.timestamp,
            'status': app.status
        })

    return render_template('view_applications.html', applications=applications_list)

@main.route('/view_candidates/<int:job_id>')
@role_required('recruiter', 'both')
def view_candidates(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    applications = Application.query.filter_by(job_id=job_id).all()
    round_configs = InterviewRoundConfig.query.filter_by(job_id=job_id).order_by(InterviewRoundConfig.round_number.asc()).all()
    candidates = []
    for app in applications:
        user = User.query.get(app.user_id)
        candidates.append({
            'application_id': app.id,
            'name': f"{user.first_name} {user.last_name}",
            'email': user.email,
            'phone': user.phone_number,
            'status': app.status,
            'applied_on': app.timestamp
        })

    return render_template('view_candidates.html', candidates=candidates, job=job, round_configs=round_configs)


@main.route('/job/<int:job_id>/shortlist_report')
@role_required('recruiter', 'both')
def shortlist_report(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    ats_rows = ATSResult.query.filter_by(job_id=job_id).order_by(ATSResult.ats_score.desc()).all()
    shortlisted = []
    rejected = []

    for row in ats_rows:
        app = Application.query.get(row.application_id)
        user = User.query.get(row.applicant_id)
        if app is None or user is None:
            continue

        item = {
            'application_id': app.id,
            'candidate_name': f"{user.first_name} {user.last_name}",
            'email': user.email,
            'ats_score': row.ats_score,
            'score_breakdown': row.score_breakdown,
            'reason': row.shortlist_reason or 'Evaluated by ATS',
            'status': app.status,
        }

        if app.status in {'Shortlisted', 'Advanced', 'Recommended', 'Accepted'}:
            shortlisted.append(item)
        elif app.status in {'Rejected', 'Eliminated'}:
            rejected.append(item)

    summary = {
        'received': len(ats_rows),
        'shortlisted': len(shortlisted),
        'rejected': len(rejected),
    }

    return render_template(
        'shortlist_report.html',
        job=job,
        summary=summary,
        shortlisted=shortlisted,
        rejected=rejected,
    )


@main.route('/job/<int:job_id>/round_scoring/<int:round_number>')
@role_required('recruiter', 'both')
def round_scoring_form(job_id, round_number):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    round_config = InterviewRoundConfig.query.filter_by(job_id=job_id, round_number=round_number).first_or_404()
    applications = Application.query.filter_by(job_id=job_id).filter(Application.status.in_(['Shortlisted', 'Advanced'])).all()

    candidates = []
    for app in applications:
        user = User.query.get(app.user_id)
        if user is None:
            continue
        candidates.append({
            'application_id': app.id,
            'name': f"{user.first_name} {user.last_name}",
            'email': user.email,
            'status': app.status,
        })

    return render_template(
        'round_scoring.html',
        job=job,
        round_config=round_config,
        candidates=candidates,
    )

@main.route('/view_interview/<int:application_id>')
@role_required('recruiter', 'both')
def view_interview(application_id):
    application = Application.query.get_or_404(application_id)
    job = Job.query.get(application.job_id)
    if job.user_id != g.user.id:
        abort(403)

    application_data = mongo_find_one({'application_id': str(application_id)})
    if not application_data:
        flash('Interview data not found.', 'danger')
        return redirect(url_for('main.view_candidates', job_id=job.id))

    feedback_list = application_data.get('feedback', [])
    
    # Pass application_id to the template 
    return render_template('view_interview.html', feedback_list=feedback_list, applicant=application.user, application_id=application_id)

@main.route('/accept_application/<int:application_id>', methods=['POST'])
@role_required('recruiter', 'both')
def accept_application(application_id):
    application = Application.query.get_or_404(application_id)
    job = Job.query.get(application.job_id)
    if job.user_id != g.user.id:
        abort(403)

    application.status = 'Accepted'
    db.session.commit()
    flash('Application accepted.', 'success')
    return redirect(url_for('main.view_candidates', job_id=job.id))

@main.route('/reject_application/<int:application_id>', methods=['POST'])
@role_required('recruiter', 'both')
def reject_application(application_id):
    application = Application.query.get_or_404(application_id)
    job = Job.query.get(application.job_id)
    if job.user_id != g.user.id:
        abort(403)

    application.status = 'Rejected'
    db.session.commit()
    flash('Application rejected.', 'success')
    return redirect(url_for('main.view_candidates', job_id=job.id))

@main.route('/dashboard')
@role_required('recruiter', 'both')
def dashboard():
    jobs = Job.query.filter_by(user_id=g.user.id).all()
    job_ids = [job.id for job in jobs]
    funnel_counts = {
        'applied': 0,
        'ats_scored': 0,
        'shortlisted': 0,
        'in_rounds': 0,
        'offer': 0,
        'final_rejection': 0,
    }

    if job_ids:
        states = db.session.query(ApplicantPipelineState.state, func.count(ApplicantPipelineState.id)).filter(
            ApplicantPipelineState.job_id.in_(job_ids)
        ).group_by(ApplicantPipelineState.state).all()

        for state, count in states:
            state_upper = (state or '').upper()
            if state_upper == 'APPLIED':
                funnel_counts['applied'] += count
            elif state_upper == 'ATS_SCORED':
                funnel_counts['ats_scored'] += count
            elif state_upper == 'SHORTLISTED':
                funnel_counts['shortlisted'] += count
            elif state_upper.startswith('ROUND_') or state_upper == 'ADVANCED':
                funnel_counts['in_rounds'] += count
            elif state_upper == 'OFFER':
                funnel_counts['offer'] += count
            elif state_upper in {'REJECTED', 'ELIMINATED', 'FINAL_REJECTION'}:
                funnel_counts['final_rejection'] += count

    return render_template('dashboard.html', jobs=jobs, funnel_counts=funnel_counts)

@main.route('/get_job_data/<int:job_id>')
@role_required('recruiter', 'both')
def get_job_data(job_id):
    job = Job.query.get_or_404(job_id)
    if job.user_id != g.user.id:
        abort(403)

    applications = Application.query.filter_by(job_id=job_id).all()
    candidates = []
    ages = []
    questions_responses = []

    for app in applications:
        candidate = User.query.get(app.user_id)
        feedback_data = mongo_find_one({'application_id': str(app.id)}) or {}
        total_score = sum(fb['score'] for fb in feedback_data.get('feedback', []) if fb['score'] is not None)
        try:
            birthday = datetime.strptime(candidate.birthday, "%Y-%m-%d")
            today = datetime.now()
            age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
        except ValueError:
            age = None  
        if age is not None:
            ages.append(age)

        candidates.append({
            'name': f"{candidate.first_name} {candidate.last_name}",
            'score': total_score,
            'app_id': app.id
        })

        # Add questions and responses
        if feedback_data:
            for feedback in feedback_data.get('feedback', []):
                # Handle None score values by setting them to 0
                score = feedback.get('score', 0) or 0
                questions_responses.append({
                    'question': feedback.get('question', ''),
                    'response': feedback.get('response', ''),
                    'score': score
                })

    # Sort candidates by score and select the top 3
    top_candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)[:3]

    # Add similarity score for top 3 candidates
    for candidate in top_candidates:
        app = Application.query.get(candidate['app_id'])
        try:
            similarity_score = float(app.message)
        except ValueError:
            similarity_score = 0.0  # Default value if conversion fails

        candidate['similarity'] = similarity_score  # Add similarity score to top candidates

    # Prepare data for both top candidates and all candidates
    all_candidates = [{'name': c['name'], 'totalScore': c['score']} for c in candidates]
    scores = [{'name': c['name'], 'totalScore': c['score'], 'similarity': c.get('similarity', 0)} for c in top_candidates]

    return jsonify({
        'topCandidates': top_candidates,
        'allCandidates': all_candidates,
        'scores': scores,
        'ages': ages,
        'questionsResponses': sorted(questions_responses, key=lambda x: x['score'], reverse=True)
    })
