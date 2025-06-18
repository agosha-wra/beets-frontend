import os
import sys
import json
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for
import sqlite3
import musicbrainzngs

# Add beets to path
sys.path.insert(0, '/usr/local/lib/python3.11/site-packages')

try:
    from beets import library, config as beets_config
    from beets.library import Library
except ImportError as e:
    print(f"Error importing beets: {e}")
    sys.exit(1)

app = Flask(__name__)

# Set up MusicBrainz
musicbrainzngs.set_useragent("beets-frontend", "1.0")

class BeetsInterface:
    def __init__(self):
        self.lib = None
        self.wishlist_db_path = None
        self._init_beets()
        self._init_wishlist_db()
    
    def _init_beets(self):
        """Initialize beets library."""
        try:
            # Try to load beets config
            config_dir = os.environ.get('BEETSDIR', '/config')
            config_file = os.path.join(config_dir, 'config.yaml')
            
            if os.path.exists(config_file):
                beets_config.set_file(config_file)
            
            # Initialize library
            lib_path = beets_config['library'].as_str()
            if not os.path.isabs(lib_path):
                lib_path = os.path.join(config_dir, lib_path)
            
            self.lib = Library(lib_path)
            print(f"Beets library loaded from: {lib_path}")
            
        except Exception as e:
            print(f"Error initializing beets: {e}")
            # Create a dummy library for development
            self.lib = None
    
    def _init_wishlist_db(self):
        """Initialize wishlist database."""
        try:
            if self.lib:
                music_dir = beets_config['directory'].as_str()
                self.wishlist_db_path = os.path.join(music_dir, 'wishlist.db')
            else:
                self.wishlist_db_path = '/config/wishlist.db'
            
            # Create wishlist database if it doesn't exist
            conn = sqlite3.connect(self.wishlist_db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS wishlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mb_id TEXT UNIQUE NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT NOT NULL,
                    added_date REAL DEFAULT (julianday('now')),
                    auto_added BOOLEAN DEFAULT 0
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS artist_check_log (
                    artist_mb_id TEXT PRIMARY KEY,
                    artist_name TEXT NOT NULL,
                    last_checked REAL DEFAULT (julianday('now'))
                )
            ''')
            
            conn.commit()
            conn.close()
            print(f"Wishlist database initialized: {self.wishlist_db_path}")
            
        except Exception as e:
            print(f"Error initializing wishlist database: {e}")

# Initialize beets interface
beets_interface = BeetsInterface()

@app.route('/')
def index():
    """Home page with overview."""
    try:
        # Get library stats
        library_count = 0
        if beets_interface.lib:
            library_count = len(list(beets_interface.lib.albums()))
        
        # Get wishlist stats
        wishlist_count = 0
        if beets_interface.wishlist_db_path:
            conn = sqlite3.connect(beets_interface.wishlist_db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM wishlist')
            wishlist_count = cursor.fetchone()[0]
            conn.close()
        
        return render_template('index.html', 
                             library_count=library_count,
                             wishlist_count=wishlist_count)
    except Exception as e:
        return render_template('index.html', 
                             library_count=0,
                             wishlist_count=0,
                             error=str(e))

@app.route('/library')
def library():
    """Library page."""
    return render_template('library.html')

@app.route('/api/library')
def api_library():
    """API endpoint for library albums."""
    try:
        search = request.args.get('search', '').strip()
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))
        
        if not beets_interface.lib:
            return jsonify({'albums': [], 'total': 0, 'page': page, 'per_page': per_page})
        
        # Get albums with optional search
        if search:
            albums = beets_interface.lib.albums(search)
        else:
            albums = beets_interface.lib.albums()
        
        albums_list = list(albums)
        total = len(albums_list)
        
        # Pagination
        start = (page - 1) * per_page
        end = start + per_page
        paginated_albums = albums_list[start:end]
        
        # Convert to dict format
        albums_data = []
        for album in paginated_albums:
            albums_data.append({
                'id': album.id,
                'artist': album.albumartist or 'Unknown Artist',
                'title': album.album or 'Unknown Album',
                'year': album.year or '',
                'mb_albumid': album.mb_albumid or '',
                'mb_albumartistid': album.mb_albumartistid or '',
                'path': album.path.decode() if hasattr(album.path, 'decode') else str(album.path)
            })
        
        return jsonify({
            'albums': albums_data,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/wishlist')
def wishlist():
    """Wishlist page."""
    return render_template('wishlist.html')

@app.route('/api/wishlist')
def api_wishlist():
    """API endpoint for wishlist albums."""
    try:
        if not beets_interface.wishlist_db_path:
            return jsonify({'albums': []})
        
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT mb_id, artist, album, added_date, auto_added
            FROM wishlist
            ORDER BY added_date DESC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        albums = []
        for row in rows:
            mb_id, artist, album, added_date, auto_added = row
            albums.append({
                'mb_id': mb_id,
                'artist': artist,
                'title': album,
                'added_date': datetime.fromtimestamp(added_date).strftime('%Y-%m-%d %H:%M:%S'),
                'auto_added': bool(auto_added)
            })
        
        return jsonify({'albums': albums})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist/released')
def api_wishlist_released():
    """API endpoint to get all wishlist albums that are already released."""
    try:
        if not beets_interface.wishlist_db_path:
            return jsonify({'error': 'Wishlist database not available'}), 500
        
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT mb_id, artist, album, added_date, auto_added
            FROM wishlist
            ORDER BY added_date DESC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        released_albums = []
        current_date = datetime.now()
        
        for row in rows:
            mb_id, artist, album, added_date, auto_added = row
            
            try:
                # Get release date from MusicBrainz
                release_info = musicbrainzngs.get_release_by_id(mb_id)
                release = release_info['release']
                
                release_date = release.get('date')
                if release_date:
                    # Parse release date
                    try:
                        if len(release_date) == 4:  # Just year
                            release_datetime = datetime.strptime(f"{release_date}-01-01", '%Y-%m-%d')
                        elif len(release_date) == 7:  # Year-month
                            release_datetime = datetime.strptime(f"{release_date}-01", '%Y-%m-%d')
                        else:  # Full date
                            release_datetime = datetime.strptime(release_date[:10], '%Y-%m-%d')
                        
                        # Check if already released
                        if release_datetime <= current_date:
                            released_albums.append({
                                'mb_id': mb_id,
                                'artist': artist,
                                'title': album,
                                'release_date': release_date,
                                'added_date': datetime.fromtimestamp(added_date).strftime('%Y-%m-%d %H:%M:%S'),
                                'auto_added': bool(auto_added),
                                'musicbrainz_url': f'https://musicbrainz.org/release/{mb_id}'
                            })
                            
                    except ValueError:
                        # If we can't parse the date, assume it's released
                        released_albums.append({
                            'mb_id': mb_id,
                            'artist': artist,
                            'title': album,
                            'release_date': release_date,
                            'added_date': datetime.fromtimestamp(added_date).strftime('%Y-%m-%d %H:%M:%S'),
                            'auto_added': bool(auto_added),
                            'musicbrainz_url': f'https://musicbrainz.org/release/{mb_id}',
                            'note': 'Date parsing failed, assumed released'
                        })
                
                # Small delay to be nice to MusicBrainz
                time.sleep(0.2)
                
            except Exception as e:
                # If we can't get MusicBrainz data, skip this album
                print(f"Error checking {artist} - {album}: {e}")
                continue
        
        return jsonify({
            'success': True,
            'total_released': len(released_albums),
            'released_albums': released_albums,
            'checked_at': current_date.strftime('%Y-%m-%d %H:%M:%S')
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist/search', methods=['POST'])
def api_wishlist_search():
    """Search MusicBrainz for albums to add to wishlist."""
    try:
        data = request.get_json()
        search_term = data.get('search', '').strip()
        
        if not search_term:
            return jsonify({'error': 'Search term required'}), 400
        
        # Search MusicBrainz
        result = musicbrainzngs.search_releases(
            query=search_term,
            limit=5,
            type='album'
        )
        
        if not result['release-list']:
            return jsonify({'albums': []})
        
        albums = []
        for release in result['release-list'][:5]:
            artist = release.get('artist-credit', [{}])[0].get('name', 'Unknown Artist')
            title = release.get('title', 'Unknown Title')
            date = release.get('date', 'Unknown Date')
            mb_id = release['id']
            
            albums.append({
                'mb_id': mb_id,
                'artist': artist,
                'title': title,
                'date': date
            })
        
        return jsonify({'albums': albums})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist/add', methods=['POST'])
def api_wishlist_add():
    """Add album to wishlist."""
    try:
        data = request.get_json()
        mb_id = data.get('mb_id')
        artist = data.get('artist')
        title = data.get('title')
        
        if not all([mb_id, artist, title]):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Check if already in library
        if beets_interface.lib:
            existing_albums = beets_interface.lib.albums(f'mb_albumid:{mb_id}')
            if existing_albums:
                return jsonify({'error': 'Album already in library'}), 400
            
            # Check by name
            try:
                escaped_artist = artist.replace('"', '\\"').replace("'", "\\'")
                escaped_title = title.replace('"', '\\"').replace("'", "\\'")
                existing_by_name = beets_interface.lib.albums(f'albumartist:"{escaped_artist}" album:"{escaped_title}"')
                if existing_by_name:
                    return jsonify({'error': 'Album already in library (by name)'}), 400
            except:
                pass
        
        # Add to wishlist
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        # Check if already in wishlist
        cursor.execute('SELECT COUNT(*) FROM wishlist WHERE mb_id = ?', (mb_id,))
        if cursor.fetchone()[0] > 0:
            conn.close()
            return jsonify({'error': 'Album already in wishlist'}), 400
        
        # Insert
        cursor.execute('''
            INSERT INTO wishlist (mb_id, artist, album, added_date, auto_added)
            VALUES (?, ?, ?, ?, ?)
        ''', (mb_id, artist, title, time.time(), False))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': f'Added {artist} - {title} to wishlist'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist/remove', methods=['POST'])
def api_wishlist_remove():
    """Remove album from wishlist."""
    try:
        data = request.get_json()
        mb_id = data.get('mb_id')
        
        if not mb_id:
            return jsonify({'error': 'MusicBrainz ID required'}), 400
        
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        # Get album info before removing
        cursor.execute('SELECT artist, album FROM wishlist WHERE mb_id = ?', (mb_id,))
        result = cursor.fetchone()
        
        if not result:
            conn.close()
            return jsonify({'error': 'Album not found in wishlist'}), 404
        
        artist, album = result
        
        # Remove
        cursor.execute('DELETE FROM wishlist WHERE mb_id = ?', (mb_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': f'Removed {artist} - {album} from wishlist'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist/auto-update', methods=['POST'])
def api_wishlist_auto_update():
    """Trigger auto-update of wishlist."""
    try:
        if not beets_interface.lib:
            return jsonify({'error': 'Beets library not available'}), 400
        
        # Get unique artists from library
        artists = []
        for album in beets_interface.lib.albums():
            if album.mb_albumartistid and album.albumartist:
                artist_key = (album.mb_albumartistid, album.albumartist)
                if artist_key not in [a[:2] for a in artists]:
                    artists.append((album.mb_albumartistid, album.albumartist))
        
        added_count = 0
        auto_check_days = 30  # Default
        
        for artist_mb_id, artist_name in artists[:10]:  # Limit to first 10 for web interface
            try:
                # Calculate date range
                check_from_date = datetime.now() - timedelta(days=auto_check_days)
                
                # Search for recent releases
                releases = musicbrainzngs.browse_releases(
                    artist=artist_mb_id,
                    release_type=['album'],
                    limit=20
                )
                
                for release in releases.get('release-list', []):
                    release_date = release.get('date')
                    if not release_date:
                        continue
                    
                    try:
                        if len(release_date) == 4:
                            release_datetime = datetime.strptime(release_date, '%Y')
                        elif len(release_date) == 7:
                            release_datetime = datetime.strptime(release_date, '%Y-%m')
                        else:
                            release_datetime = datetime.strptime(release_date[:10], '%Y-%m-%d')
                        
                        if release_datetime >= check_from_date:
                            # Add to wishlist (this will handle duplicates)
                            mb_id = release['id']
                            title = release.get('title', 'Unknown Title')
                            
                            # Check if already exists
                            conn = sqlite3.connect(beets_interface.wishlist_db_path)
                            cursor = conn.cursor()
                            cursor.execute('SELECT COUNT(*) FROM wishlist WHERE mb_id = ?', (mb_id,))
                            if cursor.fetchone()[0] == 0:
                                # Check if in library
                                existing_albums = beets_interface.lib.albums(f'mb_albumid:{mb_id}')
                                if not existing_albums:
                                    cursor.execute('''
                                        INSERT INTO wishlist (mb_id, artist, album, added_date, auto_added)
                                        VALUES (?, ?, ?, ?, ?)
                                    ''', (mb_id, artist_name, title, time.time(), True))
                                    conn.commit()
                                    added_count += 1
                            conn.close()
                            
                    except ValueError:
                        continue
                
                time.sleep(0.5)  # Be nice to MusicBrainz
                
            except Exception as e:
                continue
        
        return jsonify({
            'success': True, 
            'message': f'Auto-update complete. Added {added_count} albums.',
            'added_count': added_count
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
