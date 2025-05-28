import os
import re
import uuid
import json
import queue
import atexit
import asyncio
import logging
import threading
import time
import random
import signal
import sys
from functools import wraps
from typing import Dict, Optional, Any
from yt_dlp import YoutubeDL
from dataclasses import asdict, is_dataclass
from ytmusicapi import YTMusic
from related_songs import fetch_related_songs, process_song, search_song
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, Response

# Initialize Flask app
app = Flask(__name__)
DATA_FILE = "data.json"
USER_DATA_DIR = 'user_data'

# Configuration
CONFIG = {
    'audio_quality': '320',
    'related_songs_count': 8,
    'sse_timeout': 30,
    'max_workers': 3,  # Reduced from 5 to prevent memory issues
    'rate_limit_interval': 1.5,  # Reduced from 2 seconds
    'max_retries': 2,  # Reduced from 3
    'request_timeout': 15,  # Add timeout for requests
    'max_concurrent_sessions': 10,  # Limit concurrent SSE sessions
}

# Configure logging with better format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Ensure directories exist
os.makedirs("profile_pics", exist_ok=True)
os.makedirs(USER_DATA_DIR, exist_ok=True)

# Environment variables
API_KEY = os.getenv('LASTFM_API_KEY', 'xyg')

# Global instances with error handling
try:
    yt_music = YTMusic()
    logger.info("YTMusic client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize YTMusic: {e}")
    yt_music = None

# Thread pool with proper cleanup
executor = ThreadPoolExecutor(max_workers=CONFIG['max_workers'])
atexit.register(lambda: executor.shutdown(wait=False, cancel_futures=True))

# Rate limiting with thread safety
class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self.last_request_time = 0
        self._lock = threading.Lock()
    
    def wait_if_needed(self):
        with self._lock:
            current_time = time.time()
            elapsed = current_time - self.last_request_time
            
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed + random.uniform(0.1, 0.3)
                time.sleep(sleep_time)
            
            self.last_request_time = time.time()

rate_limiter = RateLimiter(CONFIG['rate_limit_interval'])

# Enhanced session management
event_queues: Dict[str, queue.Queue] = {}
session_threads: Dict[str, threading.Thread] = {}
queue_lock = threading.Lock()

def cleanup_session(session_id: str):
    """Clean up session resources"""
    with queue_lock:
        if session_id in event_queues:
            try:
                # Clear any remaining items
                while not event_queues[session_id].empty():
                    event_queues[session_id].get_nowait()
            except queue.Empty:
                pass
            del event_queues[session_id]
        
        if session_id in session_threads:
            
            # Remove the reference but don't try to interact with it
            del session_threads[session_id]

def create_queue_for_session(session_id: str) -> queue.Queue:
    """Create a new queue for a session with limits"""
    with queue_lock:
        # Limit concurrent sessions
        if len(event_queues) >= CONFIG['max_concurrent_sessions']:
            # Clean up old sessions
            old_sessions = list(event_queues.keys())[:len(event_queues) - CONFIG['max_concurrent_sessions'] + 1]
            for old_session in old_sessions:
                cleanup_session(old_session)
        
        if session_id not in event_queues:
            event_queues[session_id] = queue.Queue(maxsize=50)  # Limit queue size
        return event_queues[session_id]

def timeout_handler(func):
    """Decorator to add timeout handling"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        def target(result_queue):
            try:
                result = func(*args, **kwargs)
                result_queue.put(('success', result))
            except Exception as e:
                result_queue.put(('error', e))
        
        result_queue = queue.Queue()
        thread = threading.Thread(target=target, args=(result_queue,))
        thread.daemon = True
        thread.start()
        
        try:
            status, result = result_queue.get(timeout=CONFIG['request_timeout'])
            if status == 'error':
                raise result
            return result
        except queue.Empty:
            raise TimeoutError(f"Function {func.__name__} timed out after {CONFIG['request_timeout']} seconds")
    
    return wrapper

def format_sse(data: str, event=None) -> str:
    """Format data for SSE"""
    msg = f'data: {data}\n\n'
    if event is not None:
        msg = f'event: {event}\n{msg}'
    return msg

def send_song_info(song_info, session_id: str, index=None, event_type="related_song"):
    """Send song info through the event queue with error handling"""
    if not song_info or session_id not in event_queues:
        return
    
    try:
        song_dict = asdict(song_info) if is_dataclass(song_info) else song_info
        if index is not None:
            song_dict['index'] = index
        
        # Use put_nowait to avoid blocking
        event_queues[session_id].put_nowait((event_type, song_dict))
    except queue.Full:
        logger.warning(f"Queue full for session {session_id}, dropping message")
    except Exception as e:
        logger.error(f"Error sending song info: {e}")

def get_optimized_yt_dlp_options():
    """Get optimized yt-dlp options for server environment"""
    return {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'socket_timeout': 10,
        'retries': 1,  # Reduced retries
        'fragment_retries': 1,
        'skip_unavailable_fragments': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        },
        'extractor_retries': 1,
        'file_access_retries': 1,
        # Disable unnecessary features
        'writesubtitles': False,
        'writeautomaticsub': False,
        'writedescription': False,
        'writeinfojson': False,
        'writethumbnail': False,
    }

@timeout_handler
def fetch_song_details_safe(song_name: str) -> Optional[Dict[str, Any]]:
    """Thread-safe song details fetching with timeout"""
    if not yt_music:
        raise Exception("YTMusic client not initialized")
    
    rate_limiter.wait_if_needed()
    
    # Check if URL or search term
    yt_url_pattern = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)"
    match = re.search(yt_url_pattern, song_name)
    
    try:
        if match:
            video_id = match.group(1)
        else:
            search_results = yt_music.search(song_name, filter='songs', limit=1)
            if not search_results:
                return None
            video_id = search_results[0].get('videoId')
        
        if not video_id:
            return None
        
        # Get song details from YTMusic
        song_details = yt_music.get_song(video_id)
        
        title = song_details.get('videoDetails', {}).get('title', 'Unknown Title')
        artist = song_details.get('videoDetails', {}).get('author', 'Unknown Artist')
        thumbnails = song_details.get('videoDetails', {}).get('thumbnail', {}).get('thumbnails', [])
        album_art = sorted(thumbnails, key=lambda x: (x.get('width', 0), x.get('height', 0)))[-1]['url'] if thumbnails else None
        
        # Try yt-dlp for audio URL (with timeout protection)
        audio_url = None
        try:
            ydl_opts = get_optimized_yt_dlp_options()
            with YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                audio_url = info_dict.get('url')
        except Exception as e:
            logger.warning(f"yt-dlp extraction failed: {e}")
        
        return {
            'title': title,
            'artists': artist,
            'albumArt': album_art,
            'audioUrl': audio_url or f'https://www.youtube.com/watch?v={video_id}',
            'videoId': video_id,
            'fallback_mode': audio_url is None
        }
        
    except Exception as e:
        logger.error(f"Error in fetch_song_details_safe: {e}")
        return None

async def process_related_songs_async(query: str, session_id: str):
    """Async processing of related songs with better error handling"""
    try:
        if not yt_music:
            raise Exception("YTMusic client not available")
        
        # Search for main song
        search_results = yt_music.search(query, filter="songs", limit=1)
        if not search_results:
            event_queues[session_id].put_nowait(("error", {"message": "No related songs found"}))
            return
        
        main_song = await process_song(yt_music, search_results[0])
        if not main_song:
            event_queues[session_id].put_nowait(("error", {"message": "Failed to process main song"}))
            return
        
        # Get related tracks
        related_data = yt_music.get_watch_playlist(videoId=main_song.video_id)
        related_tracks = related_data.get('tracks', [])[:CONFIG['related_songs_count']]
        
        # Process tracks concurrently but limited
        processed_count = 0
        for index, track in enumerate(related_tracks, 1):
            if session_id not in event_queues:  # Check if session still exists
                break
                
            if track.get('videoId') == main_song.video_id:
                continue
            
            try:
                song = await process_song(yt_music, track, main_song.video_id, index)
                if song:
                    send_song_info(song, session_id, index)
                    processed_count += 1
            except Exception as e:
                logger.warning(f"Failed to process track {index}: {e}")
                continue
        
        # Signal completion
        if session_id in event_queues:
            event_queues[session_id].put_nowait(("complete", {
                "message": "Related songs processing complete",
                "processed_count": processed_count
            }))
        
    except Exception as e:
        logger.error(f"Error processing related songs: {e}")
        if session_id in event_queues:
            try:
                event_queues[session_id].put_nowait(("error", {"message": str(e)}))
            except queue.Full:
                pass

def run_async_processing_safe(query: str, session_id: str):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(process_related_songs_async(query, session_id))
    except Exception as e:
        logger.error(f"Error in async processing: {e}")
        if session_id in event_queues:
            try:
                event_queues[session_id].put_nowait(("error", {"message": f"Processing failed: {str(e)}"}))
            except queue.Full:
                pass
    finally:
        try:
            loop.close()
        except:
            pass
        # Remove future reference after completion
        with queue_lock:
            if session_id in session_threads:
                del session_threads[session_id]
        cleanup_session(session_id)

        
# Load/Save JSON data with error handling
def load_data():
    try:
        with open(DATA_FILE, "r") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"users": {}}

def save_data(data):
    try:
        with open(DATA_FILE, "w") as file:
            json.dump(data, file, indent=2)
    except Exception as e:
        logger.error(f"Error saving data: {e}")

@app.route('/get_song', methods=['POST'])
def get_song():
    """Optimized main endpoint for getting song details"""
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        # Validate required fields
        song_name = data.get('song_name')
        username = data.get('username')
        session_id = data.get('session_id')
        streamer_status = data.get('streamer_status', '').lower()

        if not song_name or not username:
            return jsonify({'error': 'Missing required fields: song_name, username'}), 400

        # Determine streamer mode
        is_streamer = streamer_status == 'yeshoustonstreamer'
        session_id = session_id or str(uuid.uuid4()) if is_streamer else None

        logger.info(f"Processing request: song='{song_name}', user='{username}', streamer={is_streamer}")

        # Create queue for streamers
        if is_streamer and session_id:
            create_queue_for_session(session_id)

        # Fetch song details with timeout protection
        try:
            song_details = fetch_song_details_safe(song_name)
        except TimeoutError:
            logger.error("Song details fetch timed out")
            return jsonify({'error': 'Request timed out, please try again'}), 408
        except Exception as e:
            logger.error(f"Error fetching song details: {e}")
            return jsonify({'error': 'Failed to fetch song details'}), 500

        if not song_details:
            return jsonify({'error': 'No results found for the requested song'}), 404

        song_details['requested_by'] = username

        # Start related songs processing for streamers
        if is_streamer and session_id:
            search_query = f"{song_details['title']} {song_details['artists']}"
            
            # Submit to thread pool
            future = executor.submit(run_async_processing_safe, search_query, session_id)
            session_threads[session_id] = future

        response_data = {
            'song_details': song_details,
            'message': 'Song details retrieved successfully',
            'session_id': session_id,
            'streamer_mode': is_streamer
        }

        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"Unexpected error in get_song: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/fetch_related_songs', methods=['POST'])
def fetch_related_songs_route():
    """Optimized endpoint for fetching related songs"""
    try:
        data = request.get_json(force=True)
        
        required_fields = ['title', 'artist', 'session_id']
        if not data or any(field not in data for field in required_fields):
            return jsonify({"error": "title, artist, and session_id are required"}), 400
        
        title = data['title']
        artist = data['artist']
        session_id = data['session_id']
        
        create_queue_for_session(session_id)
        search_query = f"{title} {artist}"
        
        # Submit to thread pool
        future = executor.submit(run_async_processing_safe, search_query, session_id)
        session_threads[session_id] = future
        
        return jsonify({
            "status": "success",
            "message": "Related songs processing initiated",
            "session_id": session_id
        }), 202

    except Exception as e:
        logger.error(f"Error in fetch_related_songs: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/stream_related_songs/<session_id>')
def stream_related(session_id):
    """Optimized SSE endpoint with better resource management"""
    if session_id not in event_queues:
        return jsonify({'error': 'Invalid session ID'}), 404

    def generate():
        keepalive_count = 0
        max_keepalives = CONFIG['sse_timeout'] // 5  # Send keepalive every 5 seconds
        
        try:
            while keepalive_count < max_keepalives:
                try:
                    event_type, data = event_queues[session_id].get(timeout=5)
                    yield format_sse(json.dumps(data), event=event_type)
                    
                    if event_type in ["complete", "error"]:
                        break
                        
                except queue.Empty:
                    yield ': keepalive\n\n'
                    keepalive_count += 1
                    continue
        except GeneratorExit:
            # Client disconnected
            pass
        finally:
            # Clean up session
            cleanup_session(session_id)
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'  # Disable nginx buffering
        }
    )

@app.route('/fetchlyrics', methods=['GET'])
def fetch_lyrics():
    """Optimized lyrics fetching endpoint"""
    title = request.args.get('title')
    artist = request.args.get('artist')

    if not title:
        return jsonify({"error": "The 'title' parameter is required."}), 400

    if not yt_music:
        return jsonify({"error": "Music service unavailable"}), 503

    try:
        rate_limiter.wait_if_needed()
        
        query = f"{title} {artist}" if artist else title
        search_results = yt_music.search(query, filter="songs", limit=1)

        if not search_results:
            return jsonify({"error": "No songs found"}), 404

        video_id = search_results[0].get("videoId")
        if not video_id:
            return jsonify({"error": "Invalid video ID"}), 404

        watch_playlist = yt_music.get_watch_playlist(video_id)
        browse_id = watch_playlist.get("lyrics")

        if not browse_id:
            return jsonify({"error": "Lyrics not available"}), 404

        lyrics_data = yt_music.get_lyrics(browse_id)
        if not lyrics_data:
            return jsonify({"error": "Lyrics not found"}), 404

        response = {
            "lyrics": lyrics_data.get("lyrics", "Lyrics not available"),
            "has_timestamps": lyrics_data.get("hasTimestamps", False),
        }

        if lyrics_data.get("hasTimestamps"):
            response["timed_lyrics"] = [
                {
                    "text": line.get("text", ""),
                    "start_time": line.get("start_time", "Unknown"),
                    "end_time": line.get("end_time", "Unknown"),
                }
                for line in lyrics_data.get("lyrics", [])
            ]

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error fetching lyrics: {e}")
        return jsonify({"error": "Failed to fetch lyrics"}), 500

@app.route('/login', methods=['POST'])
def login():
    """User login/ban handling"""
    try:
        data = request.get_json(force=True)
        username = data.get("username")
        user_id = data.get("user_id")
        login_time = data.get("login_time")

        if not all([username, user_id]):
            return jsonify({"error": "Missing required fields"}), 400

        user_file = os.path.join(USER_DATA_DIR, f"{user_id}.txt")

        # Check if user is banned
        if os.path.exists(user_file):
            try:
                with open(user_file, 'r') as file:
                    content = file.read()
                    if "Status: banned" in content:
                        return jsonify({"message": f"User {user_id} is banned"}), 403
            except IOError:
                pass

        # Write user data
        try:
            with open(user_file, 'w') as file:
                file.write(f"Username: {username}\n")
                file.write(f"User ID: {user_id}\n")
                file.write(f"Login Time: {login_time or 'Unknown'}\n")
                
                if data.get("ban"):
                    file.write("Status: banned\n")
                    return jsonify({"message": f"User {user_id} has been banned"}), 200
                else:
                    file.write("Status: active\n")
        except IOError as e:
            logger.error(f"Error writing user file: {e}")
            return jsonify({"error": "Failed to save user data"}), 500

        return jsonify({"message": f"User {user_id} login recorded"}), 200

    except Exception as e:
        logger.error(f"Error in login endpoint: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/user/<user_id>', methods=['GET'])
def get_user(user_id):
    """Get user information"""
    user_file = os.path.join(USER_DATA_DIR, f"{user_id}.txt")
    try:
        if os.path.exists(user_file):
            with open(user_file, 'r') as file:
                user_data = file.read()
            return f"<pre>{user_data}</pre>", 200
        else:
            return jsonify({"error": "User not found"}), 404
    except Exception as e:
        logger.error(f"Error reading user file: {e}")
        return jsonify({"error": "Failed to read user data"}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "active_sessions": len(event_queues),
        "ytmusic_available": yt_music is not None
    }), 200

@app.route("/")
def index():
    return "Server is running : houston says hi!"

# Graceful shutdown handling
def signal_handler(signum, frame):
    logger.info("Received shutdown signal, cleaning up...")
    
    # Clean up all sessions
    with queue_lock:
        for session_id in list(event_queues.keys()):
            cleanup_session(session_id)
    
    # Shutdown thread pool
    executor.shutdown(wait=False, cancel_futures=True)
    
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

if __name__ == "__main__":
    logger.info("Starting optimized music server")
    app.run(debug=False, threaded=True, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
