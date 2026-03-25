import os
from dotenv import load_dotenv # type: ignore

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'TESTINGCHEATS123'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///site.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_TYPE = 'filesystem'
    UPLOAD_FOLDER_CV = os.path.join('app', 'static', 'uploads', 'cv')
    UPLOAD_FOLDER_PHOTOS = os.path.join('app', 'static', 'uploads', 'photos')
    API_TOKEN = os.environ.get('API_TOKEN', 'default_api_token')
    API_URL = "https://api-inference.huggingface.co/models/meta-llama/Meta-Llama-3-8B-Instruct"
    MONGO_URI = 'mongodb://localhost:27017/applications'
    SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
    SMTP_USERNAME = os.environ.get('SMTP_USERNAME', '')
    SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
    SMTP_FROM_EMAIL = os.environ.get('SMTP_FROM_EMAIL', SMTP_USERNAME or 'noreply@smartrecruit.local')
    SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS', 'true').lower() == 'true'