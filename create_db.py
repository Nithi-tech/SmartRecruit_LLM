from app import create_app, db
from sqlalchemy import text

app = create_app()

with app.app_context():
    db.create_all()
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
    print("Database created successfully!")