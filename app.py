from flask import Flask, render_template, jsonify, request, send_from_directory
import sqlite3
import os
import time
import threading
import schedule
from datetime import datetime, timedelta
import musicbrainzngs
import requests
import json
import urllib.parse
import subprocess
from pathlib import Path

app = Flask(__name__)

# Configuration
BEETS_CONFIG_PATH = '/config/config.yaml'
BEETS_DB_PATH = '/config/library.db'
MUSIC_PATH = '/music/'

# slskd configuration
SLSKD_CONFIG = {
    'enabled': os.getenv('SLSKD_ENABLED', 'false').lower() == 'true',
    'url': os.getenv('SLSKD_URL', 'http://192.168.1.65:5030'),
    'api_key': os.getenv('SLSKD_API_KEY', ''),
    'download_dir': os.getenv('SLSKD_DOWNLOAD_DIR', '/downloads')
}

# MusicBrainz setup
musicbrainzngs.set_useragent("beets-frontend", "1.0", "https://github.com/beetbox/beets")

class BeetsInterface:
    def __init__(self):
        self.lib = None
        self.wishlist_db_path = None
        self.init_beets()
        
    def init_beets(self):
        """Initialize beets library connection."""
        try:
            print("Attempting to import and initialize beets...")
            
            # Import beets components (use the working imports)
            import beets
            from beets import library
            from beets import config  # This is the correct way!
            
            print(f"✓ Beets {beets.__version__} imported successfully")
            
            # Initialize config
            config.read()  # Load default config
            print("✓ Default config loaded")
            
            # Try to load custom config if it exists
            if os.path.exists(BEETS_CONFIG_PATH):
                print(f"Loading custom config: {BEETS_CONFIG_PATH}")
                config.read(BEETS_CONFIG_PATH)
                print("✓ Custom config loaded")
            
            # Initialize library
            if os.path.exists(BEETS_DB_PATH):
                self.lib = library.Library(BEETS_DB_PATH)
                print(f"✓ Connected to beets library: {BEETS_DB_PATH}")
            else:
                print(f"⚠ Beets database not found: {BEETS_DB_PATH}")
                self.lib = None
            
            # Set wishlist database path
            config_dir = os.path.dirname(MUSIC_PATH)
            self.wishlist_db_path = os.path.join(config_dir, 'wishlist.db')
            print(f"✓ Wishlist database path: {self.wishlist_db_path}")
            
        except ImportError as e:
            print(f"Beets not available - running in standalone mode: {e}")
            self.lib = None
        except Exception as e:
            print(f"Error initializing beets: {e}")
            import traceback
            traceback.print_exc()
            self.lib = None

class SlskdClient:
    def __init__(self, base_url, api_key=None):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({'X-API-Key': api_key})
    
    def search(self, query, timeout=30):
        """Search for files and return results."""
        try:
            print(f"Starting slskd search: {query}")
            
            # Start search
            search_url = f"{self.base_url}/api/v0/searches"
            response = self.session.post(search_url, json={
                'searchText': query,
                'timeout': timeout * 1000  # Convert to milliseconds
            })
            
            if response.status_code in [200, 201]:
                search_data = response.json()
                search_id = search_data.get('id')
                
                print(f"Started search with ID: {search_id}, waiting for results...")
                
                # Wait for results
                results = self.wait_for_search_completion(search_id, timeout)
                return results
            else:
                print(f"Failed to start search: HTTP {response.status_code} - {response.text}")
                return []
                
        except Exception as e:
            print(f"Error in slskd search: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def wait_for_search_completion(self, search_id, timeout=30):
        """Wait for search to complete and return results."""
        try:
            results_url = f"{self.base_url}/api/v0/searches/{search_id}"
            start_time = time.time()
            check_interval = 2  # Check every 2 seconds
            
            while time.time() - start_time < timeout:
                elapsed = int(time.time() - start_time)
                print(f"Checking search progress... ({elapsed}s elapsed)")
                
                response = self.session.get(results_url)
                
                if response.status_code == 200:
                    search_data = response.json()
                    is_complete = search_data.get('isComplete', False)
                    state = search_data.get('state', 'Unknown')
                    response_count = search_data.get('responseCount', 0)
                    
                    print(f"Search state: {state}, Complete: {is_complete}, Responses: {response_count}")
                    
                    # If we have responses, try to fetch them
                    if response_count > 0 and elapsed >= 10:  # Wait at least 10 seconds
                        responses = self.get_search_responses(search_id)
                        
                        if responses:
                            print(f"Successfully fetched {len(responses)} responses!")
                            return responses
                        else:
                            print("Failed to fetch responses from responses endpoint")
                    
                    if is_complete or state in ['Completed', 'TimedOut', 'Cancelled']:
                        print(f"Search completed with state: {state}, fetching final responses...")
                        responses = self.get_search_responses(search_id)
                        print(f"Final fetch got {len(responses)} responses")
                        return responses
                else:
                    print(f"Failed to get search status: HTTP {response.status_code}")
                    return []
                
                # Wait before next check
                time.sleep(check_interval)
            
            # Timeout reached, try to get whatever results we have
            print(f"Search timeout after {timeout}s, attempting final fetch...")
            responses = self.get_search_responses(search_id)
            print(f"Timeout fetch got {len(responses)} responses")
            return responses
                
        except Exception as e:
            print(f"Error waiting for search completion: {e}")
            import traceback
            traceback.print_exc()
            return []

    def get_search_responses(self, search_id):
        """Get responses from a search using the responses endpoint."""
        try:
            all_responses = []
            page = 0
            page_size = 50  # Fetch in chunks
            
            while True:
                # Try the responses endpoint with pagination
                responses_url = f"{self.base_url}/api/v0/searches/{search_id}/responses"
                params = {
                    'pageIndex': page,
                    'pageSize': page_size
                }
                
                print(f"Fetching responses page {page} (size: {page_size})...")
                response = self.session.get(responses_url, params=params)
                
                if response.status_code == 200:
                    page_data = response.json()
                    
                    # Handle different possible response structures
                    if isinstance(page_data, list):
                        # Direct list of responses
                        responses = page_data
                    elif isinstance(page_data, dict):
                        # Paginated response
                        responses = page_data.get('responses', page_data.get('data', []))
                        
                        # Check if this is the last page
                        if 'hasNextPage' in page_data and not page_data['hasNextPage']:
                            all_responses.extend(responses)
                            break
                        elif 'totalPages' in page_data and page >= page_data['totalPages'] - 1:
                            all_responses.extend(responses)
                            break
                    else:
                        print(f"Unexpected response type: {type(page_data)}")
                        break
                    
                    if not responses:
                        # No more responses
                        break
                    
                    all_responses.extend(responses)
                    print(f"Got {len(responses)} responses on page {page} (total: {len(all_responses)})")
                    
                    # If we got fewer than page_size, we're probably done
                    if len(responses) < page_size:
                        break
                    
                    page += 1
                    
                    # Safety limit
                    if page > 20:  # Max 20 pages
                        print("Reached page limit, stopping...")
                        break
                        
                elif response.status_code == 404:
                    print("Responses endpoint returned 404, trying alternative method...")
                    # Try without pagination
                    simple_response = self.session.get(f"{self.base_url}/api/v0/searches/{search_id}/responses")
                    if simple_response.status_code == 200:
                        simple_data = simple_response.json()
                        if isinstance(simple_data, list):
                            return simple_data
                    break
                else:
                    print(f"Failed to get responses: HTTP {response.status_code} - {response.text}")
                    break
            
            print(f"Total responses fetched: {len(all_responses)}")
            return all_responses
            
        except Exception as e:
            print(f"Error getting search responses: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def download_files(self, username, files):
        """Download files from a user."""
        try:
            print(f"Attempting to download {len(files)} files from {username}")
            
            # Correct slskd API: POST to /api/v0/transfers/downloads/{username}
            download_url = f"{self.base_url}/api/v0/transfers/downloads/{username}"
            
            # Prepare the request body - slskd expects just the files array
            files_to_download = []
            for file_info in files:
                files_to_download.append({
                    'filename': file_info.get('filename'),
                    'size': file_info.get('size', 0)
                })
            
            print(f"Download URL: {download_url}")
            print(f"Files to download: {len(files_to_download)}")
            print(f"First file: {files_to_download[0] if files_to_download else 'None'}")
            
            # Send the request with just the files array
            response = self.session.post(download_url, json=files_to_download)
            
            print(f"Download response status: {response.status_code}")
            print(f"Download response headers: {dict(response.headers)}")
            print(f"Download response text: {response.text}")
            
            if response.status_code == 201:
                print(f"✓ Started download from {username}: {len(files_to_download)} files")
                return True
            elif response.status_code == 200:
                print(f"✓ Started download from {username}: {len(files_to_download)} files (200 response)")
                return True
            else:
                print(f"✗ Failed to start download: HTTP {response.status_code} - {response.text}")
                return False
                        
        except Exception as e:
            print(f"Error downloading files: {e}")
            import traceback
            traceback.print_exc()
            return False

def calculate_album_score(matching_files, artist, title, expected_track_count=None):
    """Calculate a score for a potential album download."""
    if not matching_files:
        return 0
    
    score = 0
    num_tracks = len(matching_files)
    
    # Track count matching is now the most important factor
    if expected_track_count and expected_track_count > 0:
        track_diff = abs(num_tracks - expected_track_count)
        if track_diff == 0:
            score += 50  # Perfect match
            print(f"      PERFECT track count match: {num_tracks}")
        elif track_diff <= 1:
            score += 35  # Very close match (within 1 track)
            print(f"      Excellent track count match: {num_tracks} (±{track_diff})")
        elif track_diff <= 2:
            score += 25  # Close match (within 2 tracks)
            print(f"      Good track count match: {num_tracks} (±{track_diff})")
        elif track_diff <= 5:
            score += 10  # Reasonable match
            print(f"      Fair track count match: {num_tracks} (±{track_diff})")
        else:
            score -= 10  # Poor match
            print(f"      Poor track count match: {num_tracks} (±{track_diff})")
    else:
        # If we don't know expected count, use old logic but with less weight
        score += min(num_tracks * 1, 20)  # Reduced from 2 to 1, max 20 instead of 30
        print(f"      No expected track count, using count-based scoring: {num_tracks} tracks")
    
    # Bonus for both artist and title matches
    both_matches = sum(1 for f in matching_files if f['artist_match'] and f['title_match'])
    artist_only = sum(1 for f in matching_files if f['artist_match'] and not f['title_match'])
    title_only = sum(1 for f in matching_files if not f['artist_match'] and f['title_match'])
    
    score += both_matches * 5   # Best: both artist and title
    score += artist_only * 3    # Good: artist match
    score += title_only * 2     # OK: title match
    
    print(f"      Match quality: {both_matches} both, {artist_only} artist-only, {title_only} title-only")
    
    # File format and quality bonuses (MP3 320 preferred)
    mp3_files = sum(1 for f in matching_files if '.mp3' in f['filename'])
    m4a_files = sum(1 for f in matching_files if '.m4a' in f['filename'])
    ogg_files = sum(1 for f in matching_files if '.ogg' in f['filename'])
    wav_files = sum(1 for f in matching_files if '.wav' in f['filename'])
    
    # Check for MP3 320kbps indicators
    mp3_320_files = 0
    mp3_vbr_files = 0
    mp3_low_quality = 0
    
    for f in matching_files:
        filename = f['filename']
        size = f['size']
        
        if '.mp3' in filename:
            # Estimate bitrate from file size (rough calculation)
            # Assume 3-5 minute average track length
            estimated_bitrate = (size * 8) / (4 * 60)  # bits per second for 4-minute track
            estimated_kbps = estimated_bitrate / 1000
            
            # Check filename for quality indicators
            if any(indicator in filename for indicator in ['320', '320k', '320kbps']):
                mp3_320_files += 1
            elif any(indicator in filename for indicator in ['vbr', 'v0', 'v2']):
                mp3_vbr_files += 1
            elif estimated_kbps >= 280:  # Likely 320kbps based on size
                mp3_320_files += 1
            elif estimated_kbps >= 200:  # Likely VBR or 256kbps
                mp3_vbr_files += 1
            elif estimated_kbps < 150:  # Likely low quality
                mp3_low_quality += 1
    
    # Scoring based on format and quality
    if mp3_320_files > num_tracks * 0.8:  # Mostly MP3 320
        score += 20
        print(f"      Mostly MP3 320kbps: +20")
    elif mp3_320_files > 0:
        score += 15
        print(f"      Some MP3 320kbps: +15")
    elif mp3_vbr_files > num_tracks * 0.6:  # Mostly MP3 VBR
        score += 12
        print(f"      Mostly MP3 VBR3 VBR: +12")
    elif mp3_files > num_tracks * 0.8:  # Mostly MP3 (unknown quality)
        score += 8
        print(f"      Mostly MP3: +8")
    elif m4a_files > 0:
        score += 6
        print(f"      M4A files: +6")
    elif ogg_files > 0:
        score += 4
        print(f"      OGG files: +4")
    elif wav_files > 0:
        score += 3  # WAV is lossless but huge
        print(f"      WAV files: +3")
    
    # Penalty for low quality MP3s
    if mp3_low_quality > 0:
        penalty = mp3_low_quality * 5
        score -= penalty
        print(f"      Low quality MP3 penalty: -{penalty}")
    
    # Bonus for organized structure (files in folders)
    folder_files = sum(1 for f in matching_files if '/' in f['file'].get('filename', ''))
    if folder_files > num_tracks * 0.7:  # Most files are in folders
        score += 10
        print(f"      Well organized (in folders): +10")
    elif folder_files > 0:
        score += 5
        print(f"      Partially organized: +5")
    
    # Bonus for reasonable file sizes for MP3 320
    # MP3 320kbps: roughly 2.4MB per minute, so 8-12MB for typical 3-5 minute track
    good_size_files = 0
    for f in matching_files:
        size_mb = f['size'] / (1024 * 1024)
        if '.mp3' in f['filename']:
            if 6 <= size_mb <= 15:  # Good size for MP3 320
                good_size_files += 1
        elif size_mb > 1:  # At least 1MB for other formats
            good_size_files += 1
    
    if good_size_files == num_tracks:
        score += 8
        print(f"      All files good size for quality: +8")
    elif good_size_files > num_tracks * 0.8:
        score += 5
        print(f"      Most files good size: +5")
    
    # Check for very large files (might be videos or uncompressed)
    huge_files = sum(1 for f in matching_files if f['size'] > 50000000)  # > 50MB
    if huge_files > 0:
        score -= huge_files * 3
        print(f"      {huge_files} suspiciously large files: -{huge_files * 3}")
    
    # Check for very small files (might be low quality or corrupted)
    tiny_files = sum(1 for f in matching_files if f['size'] < 2000000)  # < 2MB
    if tiny_files > 0:
        score -= tiny_files * 2
        print(f"      {tiny_files} suspiciously small files: -{tiny_files * 2}")
    
    # Penalty for very few tracks (likely not a full album)
    if num_tracks < 3:
        score -= 15
        print(f"      Too few tracks penalty: -15")
    elif num_tracks < 5:
        score -= 5
        print(f"      Few tracks penalty: -5")
    
    # Bonus for typical album track counts
    if 8 <= num_tracks <= 20:
        score += 5
        print(f"      Typical album length: +5")
    
    final_score = max(score, 0)  # Don't return negative scores
    print(f"      Final score: {final_score}")
    return final_score

def search_and_download_album(album_info):
    """Search for and download an album using slskd."""
    if not slskd_client or not SLSKD_CONFIG['enabled']:
        print("slskd integration is disabled")
        return False
    
    try:
        artist = album_info['artist']
        title = album_info['title']
        mb_id = album_info['mb_id']
        expected_track_count = album_info.get('track_count', 0)
        
        print(f"=== Starting download search for {artist} - {title} ===")
        if expected_track_count:
            print(f"Expected track count: {expected_track_count}")
        else:
            print("No expected track count available")
        
        # Create search queries (multiple strategies)
        search_queries = [
            f'"{artist}" "{title}"',  # Exact match with quotes
            f'{artist} {title}',      # Simple match
            f'{artist} - {title}',    # With dash separator
            f'{title} {artist}',      # Reverse order
        ]
        
        all_candidates = []
        
        for query_idx, query in enumerate(search_queries):
            print(f"Search attempt {query_idx + 1}/{len(search_queries)}: {query}")
            
            # Use longer timeout for better results
            results = slskd_client.search(query, timeout=45)
            
            if not results:
                print(f"  No results for query: {query}")
                continue
            
            print(f"  Got {len(results)} user responses")
            
            # Analyze results and find potential matches
            for response_idx, response in enumerate(results):
                username = response.get('username', '')
                files = response.get('files', [])
                
                print(f"  User {response_idx + 1}: {username} ({len(files)} files)")
                
                # Look for audio files that might match our album
                matching_files = []
                for file_info in files:
                    filename = file_info.get('filename', '').lower()
                    size = file_info.get('size', 0)
                    
                    # Check if it's an audio file (no FLAC)
                    if any(ext in filename for ext in ['.mp3', '.m4a', '.ogg', '.wav']):
                        # Check if artist and title keywords are in the path/filename
                        artist_words = [word.lower() for word in artist.split() if len(word) > 2]
                        title_words = [word.lower() for word in title.split() if len(word) > 2]
                        
                        artist_match = any(word in filename for word in artist_words)
                        title_match = any(word in filename for word in title_words)
                        
                        if artist_match or title_match:
                            matching_files.append({
                                'file': file_info,
                                'filename': filename,
                                'size': size,
                                'artist_match': artist_match,
                                'title_match': title_match
                            })
                
                if matching_files:
                    track_count = len(matching_files)
                    print(f"    Found {track_count} matching audio files")
                    
                    # Score this collection of files with expected track count
                    score = calculate_album_score(matching_files, artist, title, expected_track_count)
                    
                    if score > 0:
                        candidate = {
                            'username': username,
                            'files': [f['file'] for f in matching_files],
                            'matching_files': matching_files,
                            'score': score,
                            'query': query,
                            'track_count': track_count,
                            'track_diff': abs(track_count - expected_track_count) if expected_track_count else 0
                        }
                        all_candidates.append(candidate)
                        
                        track_match_info = ""
                        if expected_track_count:
                            diff = abs(track_count - expected_track_count)
                            if diff == 0:
                                track_match_info = " (PERFECT TRACK MATCH)"
                            elif diff <= 2:
                                track_match_info = f" (±{diff} tracks)"
                            else:
                                track_match_info = f" ({diff} tracks off)"
                        
                        print(f"    Added candidate with score {score} ({track_count} tracks{track_match_info})")
                        
                        # Show some sample files for debugging
                        for i, mf in enumerate(matching_files[:3]):
                            file_size_mb = round(mf['size'] / (1024 * 1024), 1)
                            print(f"      Sample {i+1}: {mf['filename'][:60]}... ({file_size_mb}MB)")
                        
                        # Show file format breakdown
                        format_counts = {}
                        for mf in matching_files:
                            for ext in ['.mp3', '.m4a', '.ogg', '.wav']:
                                if ext in mf['filename']:
                                    format_counts[ext] = format_counts.get(ext, 0) + 1
                                    break
                        if format_counts:
                            format_str = ", ".join([f"{count}{ext}" for ext, count in format_counts.items()])
                            print(f"      Formats: {format_str}")
            
            # If we found some good candidates, we might not need to try more queries
            if len(all_candidates) >= 3:
                print(f"Found {len(all_candidates)} candidates, stopping search")
                break
        
        if not all_candidates:
            print(f"No suitable candidates found for {artist} - {title}")
            return False
        
        # Sort candidates by score (highest first)
        all_candidates.sort(key=lambda x: x['score'], reverse=True)
        
        print(f"\nFound {len(all_candidates)} total candidates:")
        for i, candidate in enumerate(all_candidates[:5]):  # Show top 5
            track_match = ""
            if expected_track_count:
                diff = candidate['track_diff']
                if diff == 0:
                    track_match = " ✓"
                elif diff <= 2:
                    track_match = f" (±{diff})"
                else:
                    track_match = f" (-{diff})"
            
            print(f"  {i+1}. User: {candidate['username']}")
            print(f"     Score: {candidate['score']}, Tracks: {candidate['track_count']}{track_match}")
            print(f"     Query: {candidate['query']}")
        
        # Try to download from the best candidates
        downloaded = False
        attempts = min(3, len(all_candidates))  # Try up to 3 candidates
        
        for i in range(attempts):
            candidate = all_candidates[i]
            print(f"\nAttempting download {i+1}/{attempts} from {candidate['username']}")
            print(f"  Score: {candidate['score']}, Tracks: {candidate['track_count']}")
            
            # Show what we're about to download
            total_size_mb = sum(f['size'] for f in candidate['matching_files']) / (1024 * 1024)
            print(f"  Total size: {total_size_mb:.1f} MB")
            
            if slskd_client.download_files(candidate['username'], candidate['files']):
                print(f"✓ Successfully started download from {candidate['username']}")
                print(f"  Downloading {candidate['track_count']} tracks")
                mark_album_downloading(mb_id)
                downloaded = True
                break
            else:
                print(f"✗ Failed to start download from {candidate['username']}")
        
        if not downloaded:
            print(f"Failed to download any candidates for {artist} - {title}")
            # Show why we might have failed
            if all_candidates:
                best = all_candidates[0]
                print(f"Best candidate had {best['track_count']} tracks (expected: {expected_track_count})")
                print(f"Best candidate score: {best['score']}")
        
        return downloaded
            
    except Exception as e:
        print(f"Error searching/downloading album: {e}")
        import traceback
        traceback.print_exc()
        return False

def mark_album_downloading(mb_id):
    """Mark an album as currently being downloaded."""
    try:
        if not beets_interface.wishlist_db_path:
            return
        
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        # Check if download_status column exists
        cursor.execute("PRAGMA table_info(wishlist)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'download_status' not in columns:
            cursor.execute('ALTER TABLE wishlist ADD COLUMN download_status TEXT')
            cursor.execute('ALTER TABLE wishlist ADD COLUMN download_started REAL')
        
        # Update status
        cursor.execute('''
            UPDATE wishlist 
            SET download_status = "downloading", download_started = ?
            WHERE mb_id = ?
        ''', (time.time(), mb_id))
        
        conn.commit()
        conn.close()
        
        print(f"Marked album as downloading: {mb_id}")
        
    except Exception as e:
        print(f"Error marking album as downloading: {e}")

# Initialize components
beets_interface = BeetsInterface()

# Initialize slskd client if enabled
slskd_client = None
if SLSKD_CONFIG['enabled']:
    try:
        slskd_client = SlskdClient(SLSKD_CONFIG['url'], SLSKD_CONFIG['api_key'])
        print(f"slskd client initialized: {SLSKD_CONFIG['url']}")
    except Exception as e:
        print(f"Failed to initialize slskd client: {e}")

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/library')
def library():
    return render_template('library.html')

@app.route('/wishlist')
def wishlist():
    return render_template('wishlist.html')

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

@app.route('/api/library/stats')
def api_library_stats():
    """API endpoint for library statistics."""
    try:
        if not beets_interface.lib:
            return jsonify({'error': 'Library not available'}), 500
        
        # Get basic stats
        albums = list(beets_interface.lib.albums())
        items = list(beets_interface.lib.items())
        
        # Calculate additional stats
        total_duration = sum(item.length for item in items if item.length)
        total_size = sum(item.filesize for item in items if item.filesize)
        
        # Get format breakdown
        formats = {}
        for item in items:
            fmt = item.format or 'Unknown'
            formats[fmt] = formats.get(fmt, 0) + 1
        
        return jsonify({
            'albums': len(albums),
            'tracks': len(items),
            'duration': total_duration,
            'size': total_size,
            'formats': formats
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/library/recent')
def api_library_recent():
    """API endpoint for recently added albums."""
    try:
        if not beets_interface.lib:
            return jsonify({'albums': []})
        
        # Get recent albums (last 20)
        albums = beets_interface.lib.albums('added:-4w..')
        recent_albums = []
        
        for album in sorted(albums, key=lambda a: a.added, reverse=True)[:20]:
            recent_albums.append({
                'title': album.album,
                'artist': album.albumartist,
                'year': album.year,
                'added': album.added.strftime('%Y-%m-%d') if album.added else None,
                'tracks': len(list(album.items()))
            })
        
        return jsonify({'albums': recent_albums})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist')
def api_wishlist():
    """API endpoint for wishlist albums."""
    try:
        if not beets_interface.wishlist_db_path:
            return jsonify({'albums': []})
        
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        # Check what columns exist
        cursor.execute("PRAGMA table_info(wishlist)")
        columns = [column[1] for column in cursor.fetchall()]
        has_release_date = 'release_date' in columns
        has_release_year = 'release_year' in columns
        has_track_count = 'track_count' in columns
        has_download_status = 'download_status' in columns
        
        if has_track_count and has_download_status:
            cursor.execute('''
                SELECT mb_id, artist, album, added_date, auto_added, release_date, release_year, track_count, download_status
                FROM wishlist
                ORDER BY added_date DESC
            ''')
        elif has_track_count:
            cursor.execute('''
                SELECT mb_id, artist, album, added_date, auto_added, release_date, release_year, track_count
                FROM wishlist
                ORDER BY added_date DESC
            ''')
        elif has_release_date:
            cursor.execute('''
                SELECT mb_id, artist, album, added_date, auto_added, release_date, release_year
                FROM wishlist
                ORDER BY added_date DESC
            ''')
        else:
            cursor.execute('''
                SELECT mb_id, artist, album, added_date, auto_added
                FROM wishlist
                ORDER BY added_date DESC
            ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        albums = []
        for row in rows:
            if has_track_count and has_download_status:
                mb_id, artist, album, added_date, auto_added, release_date, release_year, track_count, download_status = row
            elif has_track_count:
                mb_id, artist, album, added_date, auto_added, release_date, release_year, track_count = row
                download_status = None
            elif has_release_date:
                mb_id, artist, album, added_date, auto_added, release_date, release_year = row
                track_count = None
                download_status = None
            else:
                mb_id, artist, album, added_date, auto_added = row
                release_date = None
                release_year = None
                track_count = None
                download_status = None
            
            albums.append({
                'mb_id': mb_id,
                'artist': artist,
                'title': album,
                'added_date': datetime.fromtimestamp(added_date).strftime('%Y-%m-%d %H:%M:%S'),
                'auto_added': bool(auto_added),
                'release_date': release_date,
                'release_year': release_year,
                'track_count': track_count,
                'download_status': download_status,
                'musicbrainz_url': f'https://musicbrainz.org/release/{mb_id}'
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
        release_date = data.get('release_date', '')
        
        if not all([mb_id, artist, title]):
            return jsonify({'error': 'Missing required fields'}), 400
        
        # Get track count from MusicBrainz
        track_count = 0
        try:
            release_info = musicbrainzngs.get_release_by_id(mb_id, includes=['recordings'])
            release = release_info['release']
            
            if 'medium-list' in release:
                for medium in release['medium-list']:
                    if 'track-count' in medium:
                        track_count += int(medium['track-count'])
                    elif 'track-list' in medium:
                        track_count += len(medium['track-list'])
                        
            # If no release date provided, get it too
            if not release_date:
                release_date = release.get('date', '')
                
        except Exception as e:
            print(f"Could not get track count from MusicBrainz: {e}")
        
        # Parse release year from date
        release_year = None
        if release_date:
            try:
                if len(release_date) >= 4:
                    release_year = int(release_date[:4])
            except ValueError:
                pass
        
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
        
        # Check what columns exist
        cursor.execute("PRAGMA table_info(wishlist)")
        columns = [column[1] for column in cursor.fetchall()]
        has_track_count = 'track_count' in columns
        
        if has_track_count:
            # Insert with track count
            cursor.execute('''
                INSERT INTO wishlist (mb_id, artist, album, added_date, auto_added, release_date, release_year, track_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (mb_id, artist, title, time.time(), False, release_date, release_year, track_count))
        else:
            # Insert without track count (backward compatibility)
            cursor.execute('''
                INSERT INTO wishlist (mb_id, artist, album, added_date, auto_added, release_date, release_year)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (mb_id, artist, title, time.time(), False, release_date, release_year))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': f'Added {artist} - {title} to wishlist',
            'album': {
                'mb_id': mb_id,
                'artist': artist,
                'title': title,
                'release_date': release_date,
                'release_year': release_year,
                'track_count': track_count,
                'auto_added': False,
                'musicbrainz_url': f'https://musicbrainz.org/release/{mb_id}'
            }
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist/remove/<mb_id>', methods=['DELETE'])
def api_wishlist_remove(mb_id):
    """Remove album from wishlist."""
    try:
        if not beets_interface.wishlist_db_path:
            return jsonify({'error': 'Wishlist not available'}), 500
        
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM wishlist WHERE mb_id = ?', (mb_id,))
        
        if cursor.rowcount > 0:
            conn.commit()
            conn.close()
            return jsonify({'success': True, 'message': 'Album removed from wishlist'})
        else:
            conn.close()
            return jsonify({'error': 'Album not found in wishlist'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/wishlist/download/<mb_id>', methods=['POST'])
def api_download_album(mb_id):
    """Manually trigger download for a specific album."""
    print(f"=== DOWNLOAD REQUEST RECEIVED FOR {mb_id} ===")
    
    try:
        if not SLSKD_CONFIG['enabled']:
            print("slskd integration is disabled")
            return jsonify({'error': 'slskd integration is disabled'}), 400
        
        print("Getting album info from wishlist database...")
        
        # Get album info from wishlist - include track_count
        conn = sqlite3.connect(beets_interface.wishlist_db_path)
        cursor = conn.cursor()
        
        # Check what columns exist
        cursor.execute("PRAGMA table_info(wishlist)")
        columns = [column[1] for column in cursor.fetchall()]
        has_track_count = 'track_count' in columns
        
        if has_track_count:
            cursor.execute('''
                SELECT mb_id, artist, album, release_date, release_year, track_count
                FROM wishlist
                WHERE mb_id = ?
            ''', (mb_id,))
            
            result = cursor.fetchone()
            if result:
                mb_id, artist, album, release_date, release_year, track_count = result
            else:
                conn.close()
                return jsonify({'error': 'Album not found in wishlist'}), 404
        else:
            cursor.execute('''
                SELECT mb_id, artist, album, release_date, release_year
                FROM wishlist
                WHERE mb_id = ?
            ''', (mb_id,))
            
            result = cursor.fetchone()
            if result:
                mb_id, artist, album, release_date, release_year = result
                track_count = 0
            else:
                conn.close()
                return jsonify({'error': 'Album not found in wishlist'}), 404
        
        conn.close()
        
        print(f"Found album: {artist} - {album} (expected tracks: {track_count})")
        
        album_info = {
            'mb_id': mb_id,
            'artist': artist,
            'title': album,
            'release_date': release_date,
            'release_year': release_year,
            'track_count': track_count  # Make sure to include this!
        }
        
        print("Starting download thread...")
        
        # Start download in background thread
        thread = threading.Thread(
            target=search_and_download_album,
            args=(album_info,)
        )
        thread.start()
        
        print("Download thread started successfully")
        
        return jsonify({
            'success': True,
            'message': f'Started download search for {artist} - {album} ({track_count} tracks)'
        })
        
    except Exception as e:
        print(f"Error in download endpoint: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/musicbrainz/search')
def api_musicbrainz_search():
    """Search MusicBrainz for releases."""
    try:
        query = request.args.get('q', '')
        if not query:
            return jsonify({'releases': []})
        
        # Search MusicBrainz
        result = musicbrainzngs.search_releases(
            query=query,
            limit=10,
            type='album'
        )
        
        releases = []
        for release in result.get('release-list', []):
            artist = release.get('artist-credit', [{}])[0].get('name', 'Unknown Artist')
            
            releases.append({
                'id': release['id'],
                'title': release.get('title', 'Unknown Title'),
                'artist': artist,
                'date': release.get('date', ''),
                'country': release.get('country', ''),
                'status': release.get('status', ''),
                'score': release.get('score', 0)
            })
        
        return jsonify({'releases': releases})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("Starting beets-frontend...")
    print(f"Beets library: {'Available' if beets_interface.lib else 'Not available'}")
    print(f"Wishlist database: {beets_interface.wishlist_db_path}")
    print(f"slskd integration: {'Enabled' if SLSKD_CONFIG['enabled'] else 'Disabled'}")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
