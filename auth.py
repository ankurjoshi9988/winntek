from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from flask_mail import Message
from extensions import mail
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash
from sqlalchemy.exc import IntegrityError
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, BooleanField
from wtforms.validators import DataRequired, Email, EqualTo
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from extensions import db, login_manager, oauth
from models import User
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
import base64
from authlib.integrations.flask_client import OAuth, OAuthError
import os
import time
import jwt  # Ensure this library is installed
import logging
from dotenv import load_dotenv

# Setup basic configuration for logging
logging.basicConfig(level=logging.INFO)

# Load environment variables from .env file
load_dotenv()

# auth_bp
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

def init_auth(oauth_instance):
    global oauth
    oauth = oauth_instance

def decode_jwt_token(token):
    try:
        # Decode and verify the JWT using the symmetric key
        id_token_payload = jwt.decode(token, os.getenv("JWT_SECRET_KEY"), algorithms=["HS256"])
        logging.info(f"ID token payload: {id_token_payload}")

        # Ensure 'sub' (user ID) is present in the payload
        user_sub = id_token_payload.get('sub')
        if not user_sub:
            logging.error("ID token payload does not contain 'sub'.")
            raise ValueError("ID token payload does not contain 'sub'.")

        return id_token_payload

    except jwt.ExpiredSignatureError:
        logging.error("JWT has expired.")
        raise
    except jwt.InvalidTokenError:
        logging.error("Invalid JWT token.")
        raise
    except Exception as e:
        logging.error(f"Error decoding JWT: {str(e)}")
        raise

@auth_bp.route('/login/google')
def google_login():
    try:
        redirect_uri = url_for('auth.google_authorize', _external=True, _scheme='http')
        return oauth.google.authorize_redirect(redirect_uri)
    except Exception as e:
        logging.error(f"Error during Google login: {str(e)}")
        flash("An error occurred while trying to log in with Google. Please try again.", 'danger')
        return redirect(url_for('auth.login'))

@auth_bp.route('/authorize/google')
def google_authorize():
    try:
        token = oauth.google.authorize_access_token()
        if token:
            session['token'] = token
            session['token']['expires_at'] = time.time() + token.get('expires_in', 3600)
            session.modified = True
            logging.info(f"Token stored in session: {session['token']}")

            # Verify the ID token payload securely using symmetric key
            id_token = token.get('id_token')
            if id_token:
                id_token_payload = decode_jwt_token(id_token)

                # Retrieve user information
                user_info = oauth.google.get('https://www.googleapis.com/oauth2/v1/userinfo').json()
                logging.info(f"User info retrieved: {user_info}")

                # Retrieve or create the user based on email
                user = User.query.filter_by(email=user_info['email']).first()

                if not user:
                    user = User(
                        email=user_info['email'],
                        username=user_info['name'],
                        password_hash=generate_password_hash(id_token_payload['sub'], method='pbkdf2:sha256')
                    )
                    db.session.add(user)
                    db.session.commit()

                login_user(user)
                flash("Login successful!", 'success')
                return redirect(url_for('index'))
            else:
                logging.error("No ID token retrieved.")
                flash("Authentication failed. No ID token received.", 'danger')
        else:
            logging.error("No token retrieved from Google.")
            flash("Authentication failed. No token received.", 'danger')

    except OAuthError as e:
        logging.error(f"OAuth error occurred: {str(e)}")
        flash("Authentication failed. Please try again.", 'danger')
    except Exception as e:
        logging.error(f"An unexpected error occurred: {str(e)}")
        flash("Authentication failed. Please try again.", 'danger')

    return redirect(url_for('auth.login'))

@auth_bp.route('/get_credits', methods=['GET'])
@login_required
def get_credits():
    try:
        user = current_user
        return jsonify({'credits': user.credits})
    except Exception as e:
        logging.error(f"Error retrieving credits for user {current_user.username}: {str(e)}")
        return jsonify({'error': 'Unable to retrieve credits.'}), 500

@auth_bp.route('/deduct_credit', methods=['POST'])
@login_required
def deduct_credit():
    try:
        user = current_user
        if user.credits > 0:
            user.credits -= 1
            db.session.commit()
            logging.info(f"Credit deducted for user {user.username}. Remaining credits: {user.credits}")
        else:
            logging.info(f"User {user.username} has no credits left.")
        return jsonify({'credits': user.credits})
    except Exception as e:
        logging.error(f"Error deducting credit for user {current_user.username}: {str(e)}")
        return jsonify({'error': 'Unable to deduct credit.'}), 500

class RegistrationForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired(), EqualTo('confirm_password', message='Passwords must match')])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired()])
    submit = SubmitField('Register')

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember Me')
    submit = SubmitField('Login')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@auth_bp.route('/auth/register', methods=['GET', 'POST'])
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        email = form.email.data
        username = form.username.data

        # Check if the email already exists
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            logging.info(f"Attempt to register with existing email: {email}")
            flash('Email address already registered', 'danger')
            return render_template('auth.html', form=form, tab='register')

        new_user = User(email=email, username=username)
        new_user.set_password(form.password.data)
        try:
            db.session.add(new_user)
            db.session.commit()
            logging.info(f"New user registration: {email}, username: {username}")
            flash('Registration successful', 'success')
            return redirect(url_for('auth.login'))
        except IntegrityError:
            db.session.rollback()
            logging.error("An error occurred during registration.")
            flash('An error occurred during registration. Please try again.', 'danger')
            return render_template('auth.html', form=form, tab='register')
        except Exception as e:
            logging.error(f"Unexpected error during registration: {str(e)}")
            flash('An unexpected error occurred. Please try again.', 'danger')
            return render_template('auth.html', form=form, tab='register')

    return render_template('auth.html', form=form, tab='register')

@auth_bp.route('/auth/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    next_page = request.args.get('next')
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user:
            if user.check_password(form.password.data):
                login_user(user, remember=form.remember.data)
                logging.info(f"User {user.username} logged in successfully.")
                flash("Login successful!", 'success')
                return redirect(next_page or url_for('index'))
            else:
                logging.warning(f"Invalid password attempt for user {form.username.data}.")
        else:
            logging.warning(f"Invalid login attempt for non-existent user {form.username.data}.")
        flash('Invalid username or password', 'danger')
    return render_template('auth.html', form=form, tab='login')

@auth_bp.route('/logout')
@login_required
def logout():
    try:
        session.clear()  # Clear all session data on logout
        logout_user()
        flash("You have been logged out successfully.", 'success')
        return redirect(url_for('auth.login'))
    except Exception as e:
        logging.error(f"Error during logout: {str(e)}")
        flash("An error occurred while logging out. Please try again.", 'danger')
        return redirect(url_for('auth.login'))

@auth_bp.route('/auth/reset_password_request', methods=['GET', 'POST'])
def reset_password_request():
    if request.method == 'POST':
        email = request.form['email']
        user = User.query.filter_by(email=email).first()
        if user:
            try:
                send_reset_email(user)
                flash('Check your email for the instructions to reset your password', 'info')
            except Exception as e:
                logging.error(f"Error sending reset password email: {str(e)}")
                flash('An error occurred while sending the reset email. Please try again.', 'danger')
        else:
            flash('Email not found.', 'danger')
    return render_template('reset_password_request.html')

def send_reset_email(user):
    token = URLSafeTimedSerializer(current_app.config['SECRET_KEY']).dumps(user.email, salt='reset-password')
    msg = Message('Password Reset Request', sender='noreply@example.com', recipients=[user.email])
    msg.body = f"""To reset your password, visit the following link:
{url_for('auth.reset_password', token=token, _external=True)}

If you did not make this request then simply ignore this email and no changes will be made.
"""
    mail.send(msg)

@auth_bp.route('/auth/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = URLSafeTimedSerializer(current_app.config['SECRET_KEY']).loads(token, salt='reset-password', max_age=3600)
        user = User.query.filter_by(email=email).first()
        if user:
            if request.method == 'POST':
                new_password = request.form['password']
                user.set_password(new_password)
                db.session.commit()
                flash('Your password has been reset!', 'success')
                return redirect(url_for('auth.login'))
            return render_template('reset_password.html', token=token)
        else:
            flash('Invalid or expired token.', 'danger')
    except SignatureExpired:
        flash('The reset password token is expired.', 'danger')
    except BadSignature:
        flash('The reset password token is invalid.', 'danger')
    except Exception as e:
        logging.error(f"Error resetting password: {str(e)}")
        flash('An unexpected error occurred. Please try again.', 'danger')
    return redirect(url_for('auth.login'))

@auth_bp.route('/auth/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html', user=current_user)

# Additional routes and logic can be added as needed
