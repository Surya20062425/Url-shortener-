"""
Professional URL Shortener - Complete Implementation
====================================================

Features:
- Short URL generation using base62 encoding
- SQLite persistence
- Click analytics
- URL expiration
- Rate limiting
- REST API with Flask

Usage:
1. Install dependencies: pip install flask
2. Run: python url_shortener.py
3. API will be available at http://localhost:5000
"""

import sqlite3
import hashlib
import time
import string
import random
from datetime import datetime, timedelta
from functools import wraps
from dataclasses import dataclass
from typing import Optional, Dict, List
import threading
import json
import os

# Configuration
BASE62_ALPHABET = string.ascii_letters + string.digits  # a-z, A-Z, 0-9
BASE = len(BASE62_ALPHABET)
SHORT_CODE_LENGTH = 7
DEFAULT_EXPIRATION_DAYS = 30
RATE_LIMIT_REQUESTS = 100  # requests per window
RATE_LIMIT_WINDOW = 3600   # seconds (1 hour)

# Database path
DB_PATH = "url_shortener.db"


class Database:
    """SQLite database manager for URL storage."""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.local = threading.local()
        self._init_db()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self.local, 'connection') or self.local.connection is None:
            self.local.connection = sqlite3.connect(self.db_path)
            self.local.connection.row_factory = sqlite3.Row
        return self.local.connection
    
    def _init_db(self):
        """Initialize database tables."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                short_code TEXT UNIQUE NOT NULL,
                original_url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                clicks INTEGER DEFAULT 0,
                last_accessed TIMESTAMP,
                is_active BOOLEAN DEFAULT 1
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                short_code TEXT NOT NULL,
                clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ip_address TEXT,
                user_agent TEXT,
                referrer TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_short_code ON urls(short_code)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_clicks_short_code ON clicks(short_code)
        ''')
        
        conn.commit()
    
    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL query."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        return cursor
    
    def fetchone(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Fetch single row."""
        cursor = self.execute(query, params)
        return cursor.fetchone()
    
    def fetchall(self, query: str, params: tuple = ()) -> List[sqlite3.Row]:
        """Fetch all rows."""
        cursor = self.execute(query, params)
        return cursor.fetchall()
    
    def close(self):
        """Close database connection."""
        if hasattr(self.local, 'connection') and self.local.connection:
            self.local.connection.close()
            self.local.connection = None


class RateLimiter:
    """Simple in-memory rate limiter."""
    
    def __init__(self):
        self.requests: Dict[str, List[float]] = {}
        self.lock = threading.Lock()
    
    def is_allowed(self, key: str) -> bool:
        """Check if request is within rate limit."""
        now = time.time()
        
        with self.lock:
            if key not in self.requests:
                self.requests[key] = []
            
            # Clean old requests
            self.requests[key] = [
                req_time for req_time in self.requests[key]
                if now - req_time < RATE_LIMIT_WINDOW
            ]
            
            if len(self.requests[key]) >= RATE_LIMIT_REQUESTS:
                return False
            
            self.requests[key].append(now)
            return True


class URLShortener:
    """Core URL shortening service."""
    
    def __init__(self):
        self.db = Database()
        self.rate_limiter = RateLimiter()
    
    def _encode_base62(self, num: int) -> str:
        """Convert integer to base62 string."""
        if num == 0:
            return BASE62_ALPHABET[0]
        
        result = []
        while num > 0:
            num, remainder = divmod(num, BASE)
            result.append(BASE62_ALPHABET[remainder])
        
        return ''.join(reversed(result))
    
    def _generate_short_code(self, url: str) -> str:
        """Generate unique short code using hash + counter."""
        # Create hash from URL + timestamp + random salt
        hash_input = f"{url}{time.time()}{random.randint(0, 999999)}"
        hash_bytes = hashlib.sha256(hash_input.encode()).digest()
        
        # Convert first 8 bytes to integer
        num = int.from_bytes(hash_bytes[:8], 'big')
        
        # Encode to base62
        code = self._encode_base62(num)
        
        # Ensure minimum length
        while len(code) < SHORT_CODE_LENGTH:
            code = BASE62_ALPHABET[0] + code
        
        return code[:SHORT_CODE_LENGTH]
    
    def _is_code_available(self, code: str) -> bool:
        """Check if short code is not in use."""
        result = self.db.fetchone(
            "SELECT 1 FROM urls WHERE short_code = ? AND is_active = 1",
            (code,)
        )
        return result is None
    
    def shorten_url(
        self, 
        original_url: str, 
        custom_code: Optional[str] = None,
        expires_in_days: Optional[int] = None
    ) -> Dict:
        """
        Create shortened URL.
        
        Args:
            original_url: URL to shorten
            custom_code: Optional custom short code
            expires_in_days: Optional expiration period
        
        Returns:
            Dict with short_code, short_url, and metadata
        """
        # Validate URL
        if not original_url.startswith(('http://', 'https://')):
            raise ValueError("URL must start with http:// or https://")
        
        # Generate or validate short code
        if custom_code:
            if len(custom_code) < 3 or len(custom_code) > 20:
                raise ValueError("Custom code must be 3-20 characters")
            if not all(c in BASE62_ALPHABET for c in custom_code):
                raise ValueError("Custom code must be alphanumeric")
            if not self._is_code_available(custom_code):
                raise ValueError("Custom code already in use")
            short_code = custom_code
        else:
            # Generate unique code
            attempts = 0
            while attempts < 5:
                short_code = self._generate_short_code(original_url)
                if self._is_code_available(short_code):
                    break
                attempts += 1
            else:
                raise RuntimeError("Failed to generate unique code")
        
        # Calculate expiration
        if expires_in_days is None:
            expires_in_days = DEFAULT_EXPIRATION_DAYS
        
        expires_at = datetime.now() + timedelta(days=expires_in_days)
        
        # Store in database
        self.db.execute(
            """INSERT INTO urls (short_code, original_url, expires_at) 
               VALUES (?, ?, ?)""",
            (short_code, original_url, expires_at)
        )
        
        return {
            'short_code': short_code,
            'short_url': f"http://localhost:5000/{short_code}",
            'original_url': original_url,
            'expires_at': expires_at.isoformat(),
            'created_at': datetime.now().isoformat()
        }
    
    def get_original_url(self, short_code: str, request_info: Optional[Dict] = None) -> Optional[str]:
        """
        Retrieve original URL and record click.
        
        Args:
            short_code: Short code to look up
            request_info: Optional dict with ip, user_agent, referrer
        
        Returns:
            Original URL or None if not found/expired
        """
        # Check if URL exists and is active
        row = self.db.fetchone(
            """SELECT original_url, expires_at, is_active, clicks 
               FROM urls WHERE short_code = ?""",
            (short_code,)
        )
        
        if not row:
            return None
        
        # Check expiration
        expires_at = datetime.fromisoformat(row['expires_at'])
        if datetime.now() > expires_at:
            # Deactivate expired URL
            self.db.execute(
                "UPDATE urls SET is_active = 0 WHERE short_code = ?",
                (short_code,)
            )
            return None
        
        if not row['is_active']:
            return None
        
        # Record click
        self._record_click(short_code, request_info)
        
        # Update click count and last accessed
        self.db.execute(
            """UPDATE urls 
               SET clicks = clicks + 1, last_accessed = CURRENT_TIMESTAMP 
               WHERE short_code = ?""",
            (short_code,)
        )
        
        return row['original_url']
    
    def _record_click(self, short_code: str, request_info: Optional[Dict]):
        """Record click analytics."""
        if request_info is None:
            request_info = {}
        
        self.db.execute(
            """INSERT INTO clicks (short_code, ip_address, user_agent, referrer)
               VALUES (?, ?, ?, ?)""",
            (
                short_code,
                request_info.get('ip'),
                request_info.get('user_agent'),
                request_info.get('referrer')
            )
        )
    
    def get_stats(self, short_code: str) -> Optional[Dict]:
        """Get URL statistics."""
        url_row = self.db.fetchone(
            """SELECT original_url, created_at, expires_at, clicks, last_accessed, is_active
               FROM urls WHERE short_code = ?""",
            (short_code,)
        )
        
        if not url_row:
            return None
        
        # Get click history
        clicks = self.db.fetchall(
            """SELECT clicked_at, ip_address, referrer 
               FROM clicks WHERE short_code = ? ORDER BY clicked_at DESC""",
            (short_code,)
        )
        
        return {
            'short_code': short_code,
            'original_url': url_row['original_url'],
            'created_at': url_row['created_at'],
            'expires_at': url_row['expires_at'],
            'total_clicks': url_row['clicks'],
            'last_accessed': url_row['last_accessed'],
            'is_active': bool(url_row['is_active']),
            'click_history': [
                {
                    'time': click['clicked_at'],
                    'ip': click['ip_address'],
                    'referrer': click['referrer']
                }
                for click in clicks
            ]
        }
    
    def delete_url(self, short_code: str) -> bool:
        """Soft delete URL."""
        cursor = self.db.execute(
            "UPDATE urls SET is_active = 0 WHERE short_code = ?",
            (short_code,)
        )
        return cursor.rowcount > 0
    
    def cleanup_expired(self) -> int:
        """Clean up expired URLs. Returns count of deactivated URLs."""
        cursor = self.db.execute(
            """UPDATE urls SET is_active = 0 
               WHERE is_active = 1 AND expires_at < CURRENT_TIMESTAMP"""
        )
        return cursor.rowcount


# Flask API (optional - requires: pip install flask)
def create_app():
    """Create Flask application."""
    try:
        from flask import Flask, request, redirect, jsonify, abort
    except ImportError:
        print("Flask not installed. Install with: pip install flask")
        return None
    
    app = Flask(__name__)
    shortener = URLShortener()
    
    def get_client_ip():
        """Get client IP address."""
        if request.headers.get('X-Forwarded-For'):
            return request.headers.get('X-Forwarded-For').split(',')[0].strip()
        return request.remote_addr
    
    def rate_limit_check():
        """Check rate limit for client."""
        client_ip = get_client_ip()
        if not shortener.rate_limiter.is_allowed(client_ip):
            abort(429, description="Rate limit exceeded")
    
    @app.route('/api/shorten', methods=['POST'])
    def api_shorten():
        """API endpoint to shorten URL."""
        rate_limit_check()
        
        data = request.get_json() or {}
        original_url = data.get('url')
        custom_code = data.get('custom_code')
        expires_in = data.get('expires_in_days')
        
        if not original_url:
            return jsonify({'error': 'URL is required'}), 400
        
        try:
            expires_in_int = int(expires_in) if expires_in else None
        except ValueError:
            return jsonify({'error': 'Invalid expiration days'}), 400
        
        try:
            result = shortener.shorten_url(
                original_url, 
                custom_code=custom_code,
                expires_in_days=expires_in_int
            )
            return jsonify(result), 201
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except RuntimeError as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/<<short_code>')
    def redirect_to_url(short_code):
        """Redirect to original URL."""
        request_info = {
            'ip': get_client_ip(),
            'user_agent': request.headers.get('User-Agent'),
            'referrer': request.headers.get('Referer')
        }
        
        original_url = shortener.get_original_url(short_code, request_info)
        
        if not original_url:
            return jsonify({'error': 'URL not found or expired'}), 404
        
        return redirect(original_url, code=302)
    
    @app.route('/api/stats/<short_code>')
    def api_stats(short_code):
        """Get URL statistics."""
        stats = shortener.get_stats(short_code)
        if not stats:
            return jsonify({'error': 'URL not found'}), 404
        return jsonify(stats)
    
    @app.route('/api/delete/<short_code>', methods=['DELETE'])
    def api_delete(short_code):
        """Delete shortened URL."""
        if shortener.delete_url(short_code):
            return jsonify({'message': 'URL deleted'}), 200
        return jsonify({'error': 'URL not found'}), 404
    
    @app.route('/api/cleanup', methods=['POST'])
    def api_cleanup():
        """Clean up expired URLs."""
        count = shortener.cleanup_expired()
        return jsonify({'cleaned_up': count}), 200
    
    @app.route('/api/health')
    def health_check():
        """Health check endpoint."""
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat()
        })
    
    return app


# CLI Interface
def run_cli():
    """Command-line interface for URL shortener."""
    shortener = URLShortener()
    
    print("=" * 50)
    print("   Professional URL Shortener - CLI")
    print("=" * 50)
    print("Commands: shorten, stats, delete, cleanup, quit")
    print("-" * 50)
    
    while True:
        try:
            command = input("\n> ").strip().lower()
            
            if command == 'quit':
                break
            
            elif command == 'shorten':
                url = input("Enter URL: ").strip()
                custom = input("Custom code (optional): ").strip()
                custom = custom if custom else None
                
                try:
                    result = shortener.shorten_url(url, custom_code=custom)
                    print(f"\n✓ Shortened successfully!")
                    print(f"  Short URL: {result['short_url']}")
                    print(f"  Code: {result['short_code']}")
                    print(f"  Expires: {result['expires_at']}")
                except Exception as e:
                    print(f"✗ Error: {e}")
            
            elif command == 'stats':
                code = input("Enter short code: ").strip()
                stats = shortener.get_stats(code)
                if stats:
                    print(f"\nStats for {code}:")
                    print(f"  Original URL: {stats['original_url']}")
                    print(f"  Total clicks: {stats['total_clicks']}")
                    print(f"  Created: {stats['created_at']}")
                    print(f"  Expires: {stats['expires_at']}")
                    print(f"  Active: {stats['is_active']}")
                else:
                    print("URL not found")
            
            elif command == 'delete':
                code = input("Enter short code: ").strip()
                if shortener.delete_url(code):
                    print("URL deleted")
                else:
                    print("URL not found")
            
            elif command == 'cleanup':
                count = shortener.cleanup_expired()
                print(f"Cleaned up {count} expired URLs")
            
            else:
                print("Unknown command")
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
    
    print("\nGoodbye!")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--server':
        # Run Flask server
        app = create_app()
        if app:
            print("Starting URL Shortener API on http://localhost:5000")
            print("API Endpoints:")
            print("  POST /api/shorten    - Shorten URL")
            print("  GET  /<code>         - Redirect to original URL")
            print("  GET  /api/stats/<code> - Get statistics")
            print("  DELETE /api/delete/<code> - Delete URL")
            app.run(host='0.0.0.0', port=5000, debug=True)
    else:
        # Run CLI
        run_cli()
