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

# .env file se variables load karne ke liye
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'mysupersecretkeyIsVeryLongAndSecure')

# --- DATABASE CONFIGURATION (Azure PostgreSQL) ---
# Azure App Service mein 'DB_URI' variable set hona chahiye
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DB_URI')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- AZURE BLOB STORAGE CONFIGURATION ---
AZURE_CONNECTION_STRING = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
AZURE_CONTAINER_NAME = os.getenv('AZURE_CONTAINER_NAME')

# Azure Client Initialize karein
try:
    if AZURE_CONNECTION_STRING:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    else:
        print("Warning: AZURE_STORAGE_CONNECTION_STRING not found in environment.")
except Exception as e:
    print(f"Azure Storage Connection Error: {e}")

# Database aur Login Manager initialize karein
db.init_app(app)

# --- AUTO-CREATE TABLES ON STARTUP ---
# Yeh hissa Azure par missing tables ka error khatam karega
with app.app_context():
    try:
        db.create_all()
        print("Database tables checked/created successfully!")
    except Exception as e:
        print(f"Error creating database tables: {e}")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- FILTERS ---
@app.template_filter('timeago')
def timeago(date):
    now = datetime.utcnow()
    diff = now - date
    seconds = diff.total_seconds()
    if seconds < 60: return "Just now"
    minutes = seconds // 60
    if minutes < 60: return f"{int(minutes)}m ago"
    hours = minutes // 60
    if hours < 24: return f"{int(hours)}h ago"
    days = hours // 24
    return f"{int(days)}d ago"

# --- AI IMAGE ANALYSIS ---
def analyze_image(img_obj):
    tags = []
    try:
        if img_obj.mode != 'RGB': img_obj = img_obj.convert('RGB')
        
        # Quality Analysis
        width, height = img_obj.size
        if width * height > 1000000: tags.append("HD ᴴᴰ")
        else: tags.append("SD")

        # Brightness Analysis
        stat = ImageStat.Stat(img_obj.convert('L'))
        brightness = stat.mean[0]
        if brightness > 150: tags.append("Bright ☀️")
        elif brightness < 80: tags.append("Dark 🌙")
        else: tags.append("Neutral Lighting ☁️")

        # Color Analysis
        img_small = img_obj.resize((1, 1))
        color = img_small.getpixel((0, 0))
        r, g, b = color
        if r > g and r > b: tags.append("Warm Tone 🔴")
        elif b > r and b > g: tags.append("Cool Tone 🔵")
        else: tags.append("Balanced Color 🎨")

    except Exception as e:
        print(f"Analysis failed: {e}")
        return "Not Analyzed"
    return " | ".join(tags)

# --- ROUTES ---

@app.route('/')
def home():
    if current_user.is_authenticated: return redirect(url_for('feed'))
    return redirect(url_for('login'))

@app.route('/feed')
@login_required
def feed():
    query = request.args.get('q')
    if query:
        search_term = f"%{query}%"
        photos = Photo.query.join(User).filter(
            (Photo.title.ilike(search_term)) | 
            (Photo.caption.ilike(search_term)) | 
            (Photo.location.ilike(search_term)) |
            (User.username.ilike(search_term))
        ).order_by(Photo.uploaded_at.desc()).all()
    else:
        photos = Photo.query.order_by(Photo.uploaded_at.desc()).all()
    return render_template('feed.html', photos=photos)

@app.route('/u/<username>')
@login_required
def profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    photos = Photo.query.filter_by(user_id=user.id).order_by(Photo.uploaded_at.desc()).all()
    saved_photos = Photo.query.join(Save).filter(Save.user_id == user.id).order_by(Save.timestamp.desc()).all()
    liked_photos = Photo.query.join(Like).filter(Like.user_id == user.id).order_by(Like.timestamp.desc()).all()
    return render_template('profile.html', user=user, photos=photos, saved_photos=saved_photos, liked_photos=liked_photos)

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def creator_dashboard():
    if current_user.role != 'creator':
        flash("Only Creators can upload photos.", 'warning')
        return redirect(url_for('feed'))
        
    if request.method == 'POST':
        file = request.files.get('photo')
        title = request.form.get('title')
        caption = request.form.get('caption')
        people = request.form.get('people')
        location = request.form.get('location')
        
        if file and title and file.filename != '':
            filename = secure_filename(file.filename)
            
            try:
                img = Image.open(file)
                if img.mode != 'RGB': img = img.convert('RGB')
                
                auto_tags = analyze_image(img)
                img.thumbnail((1080, 1080))
                
                in_mem_file = io.BytesIO()
                img.save(in_mem_file, format='JPEG', optimize=True, quality=85)
                in_mem_file.seek(0)
                
                blob_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
                blob_client = blob_service_client.get_blob_client(container=AZURE_CONTAINER_NAME, blob=blob_name)
                blob_client.upload_blob(in_mem_file, overwrite=True)
                
                file_url = blob_client.url
                
                new_photo = Photo(filename=file_url, title=title, caption=caption, 
                                  location=location, people_present=people, 
                                  auto_tags=auto_tags, user_id=current_user.id)
                                  
                db.session.add(new_photo)
                db.session.commit()
                flash('Photo Uploaded to Azure successfully!', 'success')
                return redirect(url_for('profile', username=current_user.username))
                
            except Exception as e:
                flash(f"Azure Upload Error: {str(e)}", 'danger')
                
    return render_template('dashboard.html')

@app.route('/like/<int:photo_id>', methods=['POST'])
@login_required
def toggle_like(photo_id):
    if current_user.role == 'creator': return jsonify({'liked': False, 'error': 'Creators cannot like'})
    photo = Photo.query.get_or_404(photo_id)
    existing_like = Like.query.filter_by(user_id=current_user.id, photo_id=photo_id).first()
    liked = False
    if existing_like:
        db.session.delete(existing_like)
    else:
        new_like = Like(user_id=current_user.id, photo_id=photo_id)
        db.session.add(new_like)
        liked = True
    db.session.commit()
    return jsonify({'liked': liked, 'count': photo.likes.count()})

@app.route('/save/<int:photo_id>', methods=['POST'])
@login_required
def toggle_save(photo_id):
    if current_user.role == 'creator': return jsonify({'saved': False, 'error': 'Creators cannot save'})
    photo = Photo.query.get_or_404(photo_id)
    existing_save = Save.query.filter_by(user_id=current_user.id, photo_id=photo_id).first()
    saved = False
    if existing_save:
        db.session.delete(existing_save)
    else:
        new_save = Save(user_id=current_user.id, photo_id=photo_id)
        db.session.add(new_save)
        saved = True
    db.session.commit()
    return jsonify({'saved': saved})

@app.route('/comment/<int:photo_id>', methods=['POST'])
@login_required
def add_comment(photo_id):
    if current_user.role == 'creator': return jsonify({'success': False, 'message': 'Creators cannot comment'})
    text = request.form.get('text')
    if not text: return jsonify({'success': False, 'message': 'Empty comment'})
    
    analysis = TextBlob(text)
    score = analysis.sentiment.polarity
    if score < -0.3: return jsonify({'success': False, 'message': 'Blocked: Negative content 🚫'})
    
    sentiment_type = "neutral"
    if score > 0.3: text += " [AI: Positive]"; sentiment_type = "positive"
    elif score < 0: text += " [AI: Negative]"; sentiment_type = "negative"
    else: text += " [AI: Neutral]"
    
    db.session.add(Comment(text=text, user_id=current_user.id, photo_id=photo_id))
    db.session.commit()
    clean_text = text.split('[AI:')[0]
    return jsonify({'success': True, 'username': current_user.username, 'text': clean_text, 'sentiment': sentiment_type})

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('feed'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash('Username taken', 'danger')
            return redirect(url_for('register'))
        new_user = User(username=username, password=generate_password_hash(password), role='consumer')
        db.session.add(new_user)
        db.session.commit()
        flash('Account created! Please Log In.', 'success')
        return redirect(url_for('login')) 
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated: return redirect(url_for('feed'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            if user.role == role:
                login_user(user)
                return redirect(url_for('feed'))
            else:
                flash(f'Incorrect Role! Registered as {user.role.title()}.', 'warning')
        else:
            flash('Invalid credentials', 'danger')
    return render_template('login.html')

@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if request.method == 'POST':
        current_user.bio = request.form.get('bio')
        db.session.commit()
        flash('Profile updated!', 'success')
        return redirect(url_for('profile', username=current_user.username))
    return render_template('edit_profile.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)