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
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
import base64
from authlib.integrations.flask_client import OAuth, OAuthError
import os
import time
from dotenv import load_dotenv
import logging
import jwt


# Setup basic configuration for logging
logging.basicConfig(level=logging.INFO)

# Load environment variables from .env file
load_dotenv()

#auth_bp
auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

def init_auth(oauth_instance):
    global oauth
    oauth = oauth_instance

@auth_bp.route('/login/google')
def google_login():
    redirect_uri = url_for('auth.google_authorize', _external=True, _scheme='http')
    return oauth.google.authorize_redirect(redirect_uri)

@auth_bp.route('/authorize/google')
def google_authorize():
    try:
        token = oauth.google.authorize_access_token()
        if token:
            session['token'] = token
            session['token']['expires_at'] = time.time() + token.get('expires_in', 3600)
            session.modified = True
            logging.info(f"Token stored in session: {session['token']}")

            # Retrieve user information
            user_info = oauth.google.get('https://www.googleapis.com/oauth2/v1/userinfo').json()
            logging.info(f"User info retrieved: {user_info}")

            # Check the ID token payload
            id_token = token.get('id_token')
            if id_token:
                id_token_payload = jwt.decode(id_token, options={"verify_signature": False})
                logging.info(f"ID token payload: {id_token_payload}")

                # Ensure 'sub' is present in the ID token payload
                user_sub = id_token_payload.get('sub')
                if not user_sub:
                    raise ValueError("ID token payload does not contain 'sub'")

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
            return redirect(url_for('index'))
        else:
            logging.error("No token retrieved from Google.")
            flash("Authentication failed. No token received.", 'error')
    except OAuthError as e:
        logging.error(f"OAuth error occurred: {e}")
        flash("Authentication failed. Please try again.", 'error')
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        flash("Authentication failed. Please try again.", 'error')
    return redirect(url_for('auth.login'))

@auth_bp.route('/get_credits', methods=['GET'])
@login_required
def get_credits():
    user = current_user
    return jsonify({'credits': user.credits})

@auth_bp.route('/deduct_credit', methods=['POST'])
@login_required
def deduct_credit():
    user = current_user
    if user.credits > 0:
        user.credits -= 1
        db.session.commit()
        logging.info(f"Credit deducted for user {user.username}. Remaining credits: {user.credits}")
    else:
        logging.info(f"User {user.username} has no credits left.")
    return jsonify({'credits': user.credits})


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
            flash('Email address already registered', 'danger')
            return render_template('auth.html', form=form, tab='register')

        new_user = User(email=email, username=username)
        new_user.set_password(form.password.data)
        try:
            db.session.add(new_user)
            db.session.commit()
            flash('Registration successful', 'success')
            return redirect(url_for('auth.login'))
        except IntegrityError:
            db.session.rollback()
            flash('An error occurred during registration. Please try again.', 'danger')
            return render_template('auth.html', form=form, tab='register')

    return render_template('auth.html', form=form, tab='register')

@auth_bp.route('/auth/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    next_page = request.args.get('next')
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            return redirect(next_page or url_for('index'))
        flash('Invalid username or password', 'danger')
    return render_template('auth.html', form=form, tab='login')

@auth_bp.route('/logout')
@login_required
def logout():
    session.clear()  # Clear all session data on logout
    logout_user()
    return redirect(url_for('auth.login'))

@auth_bp.route('/auth/reset_password_request', methods=['GET', 'POST'])
def reset_password_request():
    if request.method == 'POST':
        email = request.form['email']
        user = User.query.filter_by(email=email).first()
        if user:
            send_reset_email(user)
        flash('Check your email for the instructions to reset your password', 'info')
        return redirect(url_for('auth.login'))
    return render_template('auth.html', tab='reset')

@auth_bp.route('/auth/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = URLSafeTimedSerializer(current_app.config['SECRET_KEY']).loads(token, salt='reset-password', max_age=3600)
    except SignatureExpired:
        return '<h1>The token is expired!</h1>'
    except BadSignature:
        return '<h1>Invalid token!</h1>'
    user = User.query.filter_by(email=email).first()
    if not user:
        return '<h1>User not found!</h1>'
    if request.method == 'POST':
        new_password = request.form['password']
        confirm_password = request.form['confirm_password']
        if new_password == confirm_password:
            user.password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
            db.session.commit()
            flash('Your password has been updated!', 'success')
            return redirect(url_for('auth.login'))
        else:
            flash('Passwords do not match', 'danger')
            return redirect(url_for('auth.reset_password', token=token))
    return render_template('reset_password.html', token=token)

def send_reset_email(user):
    try:
        token = URLSafeTimedSerializer(current_app.config['SECRET_KEY']).dumps(user.email, salt='reset-password')
        subject = 'Password Reset Request'
        body = f'''To reset your password, visit the following link:
{url_for('auth.reset_password', token=token, _external=True)}

If you did not make this request then simply ignore this email and no changes will be made.'''
        msg = Message(subject, recipients=[user.email], body=body)
        mail.send(msg)
        logging.info(f"Password reset email sent to {user.email}")
    except Exception as e:
        logging.error(f"Failed to send password reset email: {e}")
