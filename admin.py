from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user
from werkzeug.security import generate_password_hash
from models import User, db, Product


admin_bp = Blueprint('admin', __name__)


@admin_bp.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Access denied.')
        return redirect(url_for('index'))

    users = User.query.all()
    products = Product.query.all()  # Fetch the products
    return render_template('admin_dashboard.html', users=users, products=products)


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

from models import Product  # Import the Product model

# Route to view and manage products
@admin_bp.route('/admin/products', methods=['GET', 'POST'])
@login_required
def manage_products():
    if not current_user.is_admin:
        flash('Access denied.')
        return redirect(url_for('index'))

    if request.method == 'POST':
        product_name = request.form['name']
        product_description = request.form['description']

        # Create a new product instance and save it to the database
        new_product = Product(name=product_name, description=product_description)
        db.session.add(new_product)
        db.session.commit()
        flash('Product added successfully.')

        # Redirect back to the admin dashboard
        return redirect(url_for('admin.admin_dashboard'))

# Route to delete a product
@admin_bp.route('/admin/products/delete/<int:product_id>', methods=['POST'])
@login_required
def delete_product(product_id):
    if not current_user.is_admin:
        flash('Access denied.')
        return redirect(url_for('index'))

    product = Product.query.get(product_id)
    if product:
        db.session.delete(product)
        db.session.commit()
        flash('Product deleted successfully.')
    else:
        flash('Product not found.')

    return redirect(url_for('admin.manage_products'))
