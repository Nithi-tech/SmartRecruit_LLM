from . import db
from datetime import datetime
from werkzeug.security import check_password_hash, generate_password_hash

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    company_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone_number = db.Column(db.String(15), nullable=False)
    birthday = db.Column(db.String(10), nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='applicant')
    cv_file = db.Column(db.String(120)) 
    profile_photo = db.Column(db.String(120)) 

    def set_password(self, raw_password):
        self.password = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        if self.password == raw_password:
            self.set_password(raw_password)
            return True

        try:
            return check_password_hash(self.password, raw_password)
        except ValueError:
            return False

    def has_role(self, *roles):
        return self.role in roles

class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    salary = db.Column(db.String(50), nullable=False)  
    date_posted = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


class JobConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False, unique=True)
    department = db.Column(db.String(120), nullable=False)
    employment_type = db.Column(db.String(30), nullable=False)
    work_mode = db.Column(db.String(30), nullable=False)
    location_display = db.Column(db.String(120), nullable=True)
    application_deadline = db.Column(db.DateTime, nullable=False)
    role_summary = db.Column(db.Text, nullable=False)
    key_responsibilities = db.Column(db.JSON, nullable=False, default=list)
    required_qualifications = db.Column(db.JSON, nullable=False, default=list)
    preferred_qualifications = db.Column(db.JSON, nullable=False, default=list)
    tech_stack = db.Column(db.JSON, nullable=False, default=list)
    expected_applicants = db.Column(db.Integer, nullable=False)
    shortlist_mode = db.Column(db.String(20), nullable=False, default='count')
    shortlist_value = db.Column(db.Float, nullable=False, default=20)
    min_ats_threshold = db.Column(db.Float, nullable=False, default=70)
    mandatory_filters = db.Column(db.JSON, nullable=False, default=list)
    preferred_filters = db.Column(db.JSON, nullable=False, default=list)
    notification_tone = db.Column(db.String(20), nullable=False, default='formal')
    company_display_name = db.Column(db.String(120), nullable=False)
    company_logo_url = db.Column(db.String(255), nullable=True)
    reply_to_email = db.Column(db.String(120), nullable=False)
    send_rejection_emails = db.Column(db.Boolean, nullable=False, default=True)
    rejection_timing = db.Column(db.String(30), nullable=False, default='after_shortlisting')
    confirmed = db.Column(db.Boolean, nullable=False, default=False)
    published_at = db.Column(db.DateTime, nullable=True)


class InterviewRoundConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    round_name = db.Column(db.String(120), nullable=False)
    round_type = db.Column(db.String(40), nullable=False)
    duration_minutes = db.Column(db.Integer, nullable=False)
    focus_areas = db.Column(db.JSON, nullable=False, default=list)
    advance_count = db.Column(db.Integer, nullable=True)
    evaluation_rubric = db.Column(db.JSON, nullable=False, default=list)
    schedule_window = db.Column(db.String(255), nullable=True)


class ATSResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('application.id'), nullable=False, unique=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False)
    ats_score = db.Column(db.Float, nullable=False)
    score_breakdown = db.Column(db.JSON, nullable=False, default=dict)
    matched_keywords = db.Column(db.JSON, nullable=False, default=list)
    missing_keywords = db.Column(db.JSON, nullable=False, default=list)
    experience_summary = db.Column(db.Text, nullable=True)
    shortlist_eligible = db.Column(db.Boolean, nullable=False, default=False)
    shortlist_reason = db.Column(db.Text, nullable=True)
    parsed_resume = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class ApplicantPipelineState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('application.id'), nullable=False, unique=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False)
    state = db.Column(db.String(40), nullable=False, default='APPLIED')
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class RoundEvaluation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey('application.id'), nullable=False)
    applicant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)
    round_score = db.Column(db.Float, nullable=False)
    evaluation_data = db.Column(db.JSON, nullable=False, default=dict)
    completed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EmailEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=True)
    application_id = db.Column(db.Integer, db.ForeignKey('application.id'), nullable=True)
    event_type = db.Column(db.String(60), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    recipient = db.Column(db.String(120), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default='logged')

class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey('job.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default='Pending')

    user = db.relationship('User', backref=db.backref('applications', lazy=True))
    job = db.relationship('Job', backref=db.backref('applications', lazy=True))

    __table_args__ = (db.UniqueConstraint('user_id', 'job_id', name='unique_user_job_application'),)
