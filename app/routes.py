from flask import Blueprint, render_template, redirect, url_for, flash, request, session, g, current_app, abort, jsonify
from markdown import markdown
from werkzeug.utils import secure_filename
from functools import wraps
from datetime import datetime
import os
import pdfplumber  # type: ignore
import logging
import time

from . import db, applications_collection
from .models import User, Job, Application
from .utils import allowed_file, evaluate_cv, extract_score, generate_interview_questions, generate_feedback, convert_keys_to_strings

main = Blueprint('main', __name__)

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
            email = request.form['email']
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
            existing_user = User.query.filter_by(email=email).first()
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
            email = request.form['email']
            password = request.form['password']

            # Check if the user exists
            user = User.query.filter_by(email=email).first()
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

        new_job = Job(
            title=title,
            location=location,
            description=description,
            salary=salary,
            user_id=g.user.id
        )
        db.session.add(new_job)
        db.session.commit()
        flash('Job created successfully!', 'success')
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
            email = request.form.get('email')
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
                if cv_file and allowed_file(cv_file.filename, {'pdf'}):
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

    existing_application_sqlite = Application.query.filter_by(user_id=g.user.id, job_id=job_id).first()
    existing_application_mongo = applications_collection.find_one({
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
        with pdfplumber.open(cv_path) as pdf:
            text = ''.join(page.extract_text() for page in pdf.pages if page.extract_text())
    except Exception as e:
        logging.error(f"Failed to process CV: {e}")
        flash('Failed to process CV.', 'danger')
        return redirect(url_for('main.job_detail', job_id=job_id))

    match, similarity_score = evaluate_cv(text, job.description)
    if not match:
        flash(f'Your CV does not match the job requirements. Similarity score: {similarity_score:.2f}', 'error')
        return redirect(url_for('main.job_detail', job_id=job_id))

    questions = generate_interview_questions(text, job.description)
    session['questions'] = questions
    session['current_question'] = 0
    session['responses'] = {}
    session['job_id'] = job_id
    session['similarity_score'] = similarity_score

    return redirect(url_for('main.interview_questions'))

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
    applications_collection.insert_one(application_data)

    flash('Application submitted successfully!', 'success')
    return redirect(url_for('main.view_applications'))

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

    return render_template('view_candidates.html', candidates=candidates, job=job)

@main.route('/view_interview/<int:application_id>')
@role_required('recruiter', 'both')
def view_interview(application_id):
    application = Application.query.get_or_404(application_id)
    job = Job.query.get(application.job_id)
    if job.user_id != g.user.id:
        abort(403)

    application_data = applications_collection.find_one({'application_id': str(application_id)})
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
    return render_template('dashboard.html', jobs=jobs)

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
        feedback_data = applications_collection.find_one({'application_id': str(app.id)})
        total_score = sum(fb['score'] for fb in feedback_data.get('feedback', []) if fb['score'] is not None)

        # Calculate age from birthday
        try:
            birthday = datetime.strptime(candidate.birthday, "%Y-%m-%d")
            today = datetime.now()
            age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
        except ValueError:
            age = None  # or set a default value if the birthday format is incorrect

        if age is not None:
            ages.append(age)

        candidates.append({
            'name': f"{candidate.first_name} {candidate.last_name}",
            'score': total_score,
            'app_id': app.id  # Store app ID for later use
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
