from flask_login import UserMixin
from extensions import db

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    @property
    def is_active(self):
        # Assume all users are active in this basic example
        return True

    # Optional: Define these properties if you want more fine-grained control
    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False
