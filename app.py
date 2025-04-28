import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, abort
from flask_sqlalchemy import SQLAlchemy
from cryptography.fernet import Fernet
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
import logging
import pytz

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configure timezone
est_tz = pytz.timezone('America/New_York')

app = Flask(__name__)

# Configure SQLite database
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pastebin.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Generate or load encryption key
try:
    if not os.path.exists('encryption.key'):
        key = Fernet.generate_key()
        with open('encryption.key', 'wb') as key_file:
            key_file.write(key)
            logger.info("Generated new encryption key")
    else:
        with open('encryption.key', 'rb') as key_file:
            key = key_file.read()
            logger.info("Loaded existing encryption key")

    cipher_suite = Fernet(key)
except Exception as e:
    logger.error(f"Error with encryption key: {str(e)}")
    raise

# Database Model
class Snippet(db.Model):
    id = db.Column(db.String(16), primary_key=True)
    encrypted_text = db.Column(db.Text, nullable=False)
    expiration_time = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Create database tables
with app.app_context():
    try:
        db.create_all()
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Error creating database tables: {str(e)}")
        raise

def cleanup_expired_snippets():
    with app.app_context():
        try:
            current_time = datetime.utcnow()
            expired = Snippet.query.filter(Snippet.expiration_time <= current_time).all()
            count = len(expired)
            for snippet in expired:
                db.session.delete(snippet)
            db.session.commit()
            logger.info(f"Cleaned up {count} expired snippets")
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}")
            db.session.rollback()

# Schedule cleanup job
scheduler = BackgroundScheduler()
scheduler.add_job(func=cleanup_expired_snippets, trigger="interval", minutes=5)
scheduler.start()

def convert_to_est(dt):
    """Convert UTC datetime to EST"""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(est_tz)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/snippets', methods=['POST'])
def create_snippet():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        text = data.get('text')
        if not text:
            return jsonify({'error': 'Text is required'}), 400

        seconds = int(data.get('expiration_seconds', 86400))  # Default to 24 hours in seconds
        max_seconds = 168 * 3600  # 7 days in seconds
        if not 1 <= seconds <= max_seconds:
            return jsonify({'error': f'Expiration time must be between 1 second and {max_seconds} seconds (7 days)'}), 400

        # Generate unique ID
        import secrets
        snippet_id = secrets.token_urlsafe(12)

        # Encrypt the text
        encrypted_text = cipher_suite.encrypt(text.encode())
        
        # Calculate expiration time in UTC
        current_time_utc = datetime.utcnow()
        expiration_time_utc = current_time_utc + timedelta(seconds=seconds)
        
        # Create new snippet (store in UTC)
        snippet = Snippet(
            id=snippet_id,
            encrypted_text=encrypted_text,
            expiration_time=expiration_time_utc
        )
        
        db.session.add(snippet)
        db.session.commit()
        
        # Convert expiration time to EST for response
        expiration_time_est = convert_to_est(expiration_time_utc)
        
        logger.info(f"Created new snippet with ID: {snippet_id}")
        return jsonify({
            'id': snippet_id,
            'expiration_time': expiration_time_est.isoformat(),
            'timezone': 'EST'
        })
    
    except ValueError as e:
        logger.warning(f"Invalid input: {str(e)}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating snippet: {str(e)}")
        db.session.rollback()
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/snippets/<snippet_id>', methods=['GET'])
def get_snippet(snippet_id):
    try:
        snippet = Snippet.query.get(snippet_id)
        if not snippet:
            logger.info(f"Snippet not found: {snippet_id}")
            return jsonify({'error': 'Snippet not found'}), 404
        
        # Check if snippet has expired (using UTC)
        if snippet.expiration_time <= datetime.utcnow():
            db.session.delete(snippet)
            db.session.commit()
            logger.info(f"Deleted expired snippet: {snippet_id}")
            return jsonify({'error': 'Snippet has expired'}), 404
        
        # Decrypt the text
        decrypted_text = cipher_suite.decrypt(snippet.encrypted_text).decode()
        
        # Convert expiration time to EST for response
        expiration_time_est = convert_to_est(snippet.expiration_time)
        
        logger.info(f"Retrieved snippet: {snippet_id}")
        return jsonify({
            'text': decrypted_text,
            'expiration_time': expiration_time_est.isoformat(),
            'timezone': 'EST'
        })
    
    except Exception as e:
        logger.error(f"Error retrieving snippet: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(404)
def not_found_error(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5002) 