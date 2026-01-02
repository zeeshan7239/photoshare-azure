import os
import io
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Photo, Like, Comment, Save
from textblob import TextBlob
from PIL import Image, ImageStat
from dotenv import load_dotenv

# --- AZURE STORAGE LIBRARY ---
from azure.storage.blob import BlobServiceClient

# .env file load karein
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'zeeshan_secure_key_7239')

# --- DATABASE CONFIGURATION (Data Persistence Fix) ---
# Docker redeploy par data bachane ke liye Azure PostgreSQL lazmi hai
db_uri = os.getenv('AZURE_POSTGRESQL_CONNECTIONSTRING') or os.getenv('SQLALCHEMY_DATABASE_URI')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if not db_uri:
    # Fallback to SQLite (Sirf local testing ke liye)
    os.makedirs(app.instance_path, exist_ok=True)
    db_path = os.path.join(app.instance_path, 'app.db')
    db_uri = f'sqlite:///{db_path}'
    print("⚠️ WARNING: No Azure DB found. Using SQLite. Data WILL BE DELETED on redeploy.")
else:
    print("✅ SUCCESS: Connected to Persistent Azure PostgreSQL.")

app.config['SQLALCHEMY_DATABASE_URI'] = db_uri

# --- AZURE BLOB STORAGE CONFIGURATION ---
AZURE_CONNECTION_STRING = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
AZURE_CONTAINER_NAME = os.getenv('AZURE_CONTAINER_NAME')

# Local uploads folder (Backup)
LOCAL_UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(LOCAL_UPLOAD_FOLDER, exist_ok=True)

blob_service_client = None
if AZURE_CONNECTION_STRING:
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    except Exception as e:
        print(f"Azure Storage Error: {e}")

# Database initialize karein
db.init_app(app)

with app.app_context():
    try:
        db.create_all()
        print("Database tables initialized!")
    except Exception as e:
        print(f"DB Error: {e}")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- FILTERS & AI ANALYSIS ---
@app.template_filter('timeago')
def timeago(date):
    diff = datetime.utcnow() - date
    s = diff.total_seconds()
    if s < 60: return "Just now"
    if s < 3600: return f"{int(s//60)}m ago"
    if s < 86400: return f"{int(s//3600)}h ago"
    return f"{int(s//86400)}d ago"

def analyze_image(img_obj):
    tags = []
    try:
        if img_obj.mode != 'RGB': img_obj = img_obj.convert('RGB')
        w, h = img_obj.size
        tags.append("HD ᴴᴰ" if w*h > 1000000 else "SD")
        stat = ImageStat.Stat(img_obj.convert('L'))
        brightness = stat.mean[0]
        tags.append("Bright ☀️" if brightness > 150 else "Dark 🌙" if brightness < 80 else "Neutral ☁️")
        img_small = img_obj.resize((1, 1))
        r, g, b = img_small.getpixel((0, 0))
        tags.append("Warm Tone 🔴" if r > g and r > b else "Cool Tone 🔵" if b > r and b > g else "Balanced 🎨")
    except: return "Not Analyzed"
    return " | ".join(tags)

# --- ROUTES ---

@app.route('/')
def home():
    if current_user.is_authenticated: return redirect(url_for('feed'))
    return redirect(url_for('login'))

@app.route('/feed')
@login_required
def feed():
    photos = Photo.query.order_by(Photo.uploaded_at.desc()).all()
    return render_template('feed.html', photos=photos)

@app.route('/u/<username>')
@login_required
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    photos = Photo.query.filter_by(user_id=user.id).all()
    return render_template('profile.html', user=user, photos=photos)

# --- UPDATED REGISTRATION (Dropdown Fix) ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('feed'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role', 'consumer') # Form se role hasil karein
        
        if User.query.filter_by(username=username).first():
            flash('Username taken', 'danger')
            return redirect(url_for('register'))
        
        new_user = User(username=username, 
                        password=generate_password_hash(password), 
                        role=role) 
        db.session.add(new_user)
        db.session.commit()
        flash(f'Account created as {role.title()}! Log in now.', 'success')
        return redirect(url_for('login')) 
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            if user.role == request.form.get('role'):
                login_user(user)
                return redirect(url_for('feed'))
            flash(f'Role mismatch! You are a {user.role}.', 'warning')
    return render_template('login.html')

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def creator_dashboard():
    if current_user.role != 'creator':
        flash("Only Creators can upload.", 'warning')
        return redirect(url_for('feed'))
    if request.method == 'POST':
        file = request.files.get('photo')
        if file and blob_service_client:
            filename = secure_filename(file.filename)
            img = Image.open(file)
            auto_tags = analyze_image(img)
            in_mem_file = io.BytesIO()
            img.save(in_mem_file, format='JPEG')
            in_mem_file.seek(0)
            blob_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
            blob_client = blob_service_client.get_blob_client(container=AZURE_CONTAINER_NAME, blob=blob_name)
            blob_client.upload_blob(in_mem_file, overwrite=True)
            new_photo = Photo(filename=blob_client.url, title=request.form.get('title'), 
                              caption=request.form.get('caption'), auto_tags=auto_tags, user_id=current_user.id)
            db.session.add(new_photo)
            db.session.commit()
            return redirect(url_for('profile', username=current_user.username))
    return render_template('dashboard.html')

@app.route('/comment/<int:photo_id>', methods=['POST'])
@login_required
def add_comment(photo_id):
    text = request.form.get('text')
    analysis = TextBlob(text)
    # Advanced Sentiment Analysis Feature
    if analysis.sentiment.polarity < -0.3:
        return jsonify({'success': False, 'message': 'Negative content blocked!'})
    db.session.add(Comment(text=text, user_id=current_user.id, photo_id=photo_id))
    db.session.commit()
    return jsonify({'success': True, 'username': current_user.username, 'text': text})

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)