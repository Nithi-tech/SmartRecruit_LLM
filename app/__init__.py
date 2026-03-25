from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_session import Session
from flask_migrate import Migrate # type: ignore
from pymongo import MongoClient
from sqlalchemy import inspect, text
from .config import Config

db = SQLAlchemy()
migrate = Migrate()
sess = Session()

mongo_client = MongoClient(
    'mongodb://localhost:27017/',
    serverSelectionTimeoutMS=1000,
    connectTimeoutMS=1000,
    socketTimeoutMS=1000
)
mongodb = mongo_client['applications']
applications_collection = mongodb['applications']


def _ensure_user_role_schema():
    inspector = inspect(db.engine)
    if not inspector.has_table('user'):
        return

    columns = {column['name'] for column in inspector.get_columns('user')}

    if 'role' not in columns:
        db.session.execute(text("ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'applicant'"))
        db.session.commit()

    # Backfill roles for older data.
    db.session.execute(text("UPDATE user SET role = 'applicant' WHERE role IS NULL OR role = ''"))
    db.session.execute(
        text(
            """
            UPDATE user
            SET role = 'recruiter'
            WHERE id IN (SELECT DISTINCT user_id FROM job)
              AND role = 'applicant'
            """
        )
    )
    db.session.execute(
        text(
            """
            UPDATE user
            SET role = 'both'
            WHERE id IN (SELECT DISTINCT user_id FROM job)
              AND id IN (SELECT DISTINCT user_id FROM application)
            """
        )
    )
    db.session.commit()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)  
    migrate.init_app(app, db)
    sess.init_app(app)

    with app.app_context():
        db.create_all()
        _ensure_user_role_schema()
        from .routes import main as main_blueprint
        app.register_blueprint(main_blueprint)

        return app
