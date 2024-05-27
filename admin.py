from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from models import User, db

admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Access denied.')
        return redirect(url_for('index'))

    users = User.query.all()
    return render_template('admin_dashboard.html', users=users)


@admin_bp.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
def admin_reset_password(user_id):
    if not current_user.is_admin:
        flash('Access denied.')
        return redirect(url_for('index'))

    user = User.query.get(user_id)
    if user:
        new_password = request.form['new_password']
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash(f'Password for {user.email} has been reset.')
    else:
        flash('User not found.')

    return redirect(url_for('admin.admin_dashboard'))

