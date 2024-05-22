from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from flask_login import login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo
from itsdangerous import URLSafeTimedSerializer, SignatureExpired
from extensions import db, login_manager, oauth  # Ensure these are correctly imported from your extensions module
from models import User  # Assuming User model is defined in models.py or similar
from googleapiclient.discovery import build
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
    redirect_uri = url_for('auth.google_authorize', _external=True, _scheme='https')
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



class RegistrationForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired(), EqualTo('confirm_password', message='Passwords must match')])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired()])
    submit = SubmitField('Register')

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = generate_password_hash(form.password.data, method='pbkdf2:sha256')
        new_user = User(email=form.email.data, username=form.username.data, password_hash=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('auth.login'))
    return render_template('register.html', form=form)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        else:
            flash('Invalid username or password')
    return render_template('auth.html', form=form)

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))

# This is an example assuming your OAuth flow redirects here with the token

@auth_bp.route('/reset_password', methods=['GET', 'POST'])
def reset_password_request():
    if request.method == 'POST':
        email = request.form['email']
        user = User.query.filter_by(email=email).first()
        if user:
            send_reset_email(user)
        flash('Check your email for the instructions to reset your password', 'info')
        return redirect(url_for('auth.login'))
    return render_template('reset_password_request.html')

@auth_bp.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        email = URLSafeTimedSerializer(current_app.config['SECRET_KEY']).loads(token, max_age=3600)
    except SignatureExpired:
        return '<h1>The token is expired!</h1>'
    except Exception as e:
        return f'<h1>Invalid token: {str(e)}</h1>'

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



def get_valid_token():
    logging.info("Checking for valid token...")
    if 'token' in session and 'expires_at' in session['token'] and 'access_token' in session['token']:
        logging.info(f"Token found in session: {session['token']}")
        if session['token']['expires_at'] > time.time():
            logging.info("Token is still valid.")
            return session['token']['access_token']
        else:
            logging.info("Token has expired. Attempting to refresh...")
    else:
        logging.info("No valid token available. Need to log in again.")
        return None

    refreshed_token = refresh_token_if_needed()
    if not refreshed_token:
        logging.error("Failed to refresh token or no refresh token available.")
        return None

    session['token'] = {
        'access_token': refreshed_token['access_token'],
        'refresh_token': refreshed_token.get('refresh_token', session['token'].get('refresh_token')),
        'expires_at': int(time.time()) + refreshed_token['expires_in']
    }
    session.modified = True
    logging.info(f"Token refreshed and stored in session: {session['token']}")
    return session['token']['access_token']


def refresh_token_if_needed():
    logging.info("Checking if token refresh is needed...")
    if 'token' in session and 'refresh_token' in session['token']:
        if session['token'].get('expires_at', 0) < time.time():
            logging.info("Token has expired. Attempting to refresh...")
            try:
                new_token = oauth.google.refresh_token(refresh_token=session['token']['refresh_token'])
                session['token'] = {
                    'access_token': new_token['access_token'],
                    'refresh_token': new_token.get('refresh_token', session['token']['refresh_token']),
                    'expires_at': int(time.time()) + new_token['expires_in']
                }
                session.modified = True
                logging.info(f"Token refreshed: {session['token']}")
                return new_token['access_token']
            except Exception as e:
                logging.error(f"Failed to refresh token: {e}")
                return None
    logging.info("No token refresh needed or available.")
    return session.get('token', {}).get('access_token')


def refresh_token(refresh_token):
    try:
        logging.info("Attempting to refresh token...")
        new_token_response = oauth.google.refresh_token(refresh_token=refresh_token)
        logging.info(f"Token refreshed successfully: {new_token_response}")
        return {
            'access_token': new_token_response['access_token'],
            'expires_in': new_token_response['expires_in'],
            'refresh_token': new_token_response.get('refresh_token', refresh_token)
        }
    except Exception as e:
        logging.error(f"Failed to refresh token: {e}")
        return None



def send_reset_email(user):
    token = URLSafeTimedSerializer(current_app.config['SECRET_KEY']).dumps(user.email, salt='reset-password')
    subject = 'Password Reset Request'
    body = f'''To reset your password, visit the following link:
{url_for('auth.reset_password', token=token, _external=True)}

If you did not make this request then simply ignore this email and no changes will be made.'''
    send_email(user, subject, body)


def send_email(user, subject, body):
    """Send an email using the Gmail API."""
    try:
        access_token = get_valid_token()
        if not access_token:
            logging.error("No valid token available for sending email")
            return 'No valid token available'

        credentials = Credentials(
            token=access_token,
            refresh_token=session['token'].get('refresh_token', None),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.getenv('GOOGLE_CLIENT_ID'),
            client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
            scopes=['https://www.googleapis.com/auth/gmail.send']
        )
        service = build('gmail', 'v1', credentials=credentials)
        email_msg = f"From: {user.email}\r\nTo: {user.email}\r\nSubject: {subject}\r\n\r\n{body}"
        encoded_message = base64.urlsafe_b64encode(email_msg.encode()).decode()
        message = {'raw': encoded_message}
        sent_message = service.users().messages().send(userId="me", body=message).execute()
        logging.info(f'Message Id: {sent_message["id"]}')
        return 'Email sent successfully'
    except KeyError as ke:
        logging.error(f"A KeyError occurred: {ke}")
        return 'Failed to send email due to missing key'
    except Exception as e:
        logging.error('An error occurred:', e)
        return 'Failed to send email'
