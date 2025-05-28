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
from flask.cli import F
from typing import Dict
from yt_dlp import YoutubeDL
from dataclasses import asdict
from ytmusicapi import YTMusic
from related_songs import fetch_related_songs, process_song, search_song
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response

# Initialize Flask app
app = Flask(__name__)
DATA_FILE = "data.json"
USER_DATA_DIR = 'user_data'

pref8 = '320'  # audio quality
ints = 4  # number of related songs to send to client
tO = 30  # timeout for streaming sessions

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

UPLOAD_FOLDER = "profile_pics"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# API Key from Last.fm
API_KEY = os.getenv('LASTFM_API_KEY', 'xyg')

# YTMusic client
yt_music = YTMusic()

# Thread pool for concurrent tasks
executor = ThreadPoolExecutor(max_workers=5)
atexit.register(executor.shutdown, wait=True)

# Rate limiting variables
last_request_time = 0
min_request_interval = 2  # Minimum seconds between requests

def rate_limit_delay():
    """Add delay between requests to avoid rate limiting"""
    global last_request_time
    current_time = time.time()
    elapsed = current_time - last_request_time
    
    if elapsed < min_request_interval:
        sleep_time = min_request_interval - elapsed + random.uniform(0.5, 1.5)
        time.sleep(sleep_time)
    
    last_request_time = time.time()

# Load JSON data
def load_data():
    try:
        with open(DATA_FILE, "r") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"users": {}}

# Save JSON data
def save_data(data):
    with open(DATA_FILE, "w") as file:
        json.dump(data, file, indent=4)

# Create directory if it doesn't exist
if not os.path.exists(USER_DATA_DIR):
    os.makedirs(USER_DATA_DIR)

# Queue for SSE communication
event_queues: Dict[str, queue.Queue] = {}

def create_queue_for_session(session_id: str) -> queue.Queue:
    """Create a new queue for a session"""
    if session_id not in event_queues:
        event_queues[session_id] = queue.Queue()
    return event_queues[session_id]

def format_sse(data: str, event=None) -> str:
    """Format data for SSE"""
    msg = f'data: {data}\n\n'
    if event is not None:
        msg = f'event: {event}\n{msg}'
    return msg

from dataclasses import is_dataclass
def send_song_info(song_info, session_id: str, index=None, event_type="related_song"):
    """Send song info through the event queue"""
    if song_info and session_id in event_queues:
        song_dict = asdict(song_info) if is_dataclass(song_info) else song_info
        if index is not None:
            song_dict['index'] = index
        event_queues[session_id].put((event_type, song_dict))

async def process_related_songs(query: str, session_id: str):
    """Process related songs and send via SSE"""
    try:
        loop = asyncio.get_event_loop()
        search_results = await loop.run_in_executor(
            None,
            lambda: yt_music.search(query, filter="songs", limit=1)
        )
        
        if not search_results:
            event_queues[session_id].put(("error", {"message": "No related songs found"}))
            return
            
        main_song = await process_song(yt_music, search_results[0])
        if main_song:
            related_data = await loop.run_in_executor(
                None,
                lambda: yt_music.get_watch_playlist(videoId=main_song.video_id)
            )
            
            related_tracks = related_data.get('tracks', [])[:ints]
            
            for index, track in enumerate(related_tracks, 1):
                if track.get('videoId') == main_song.video_id:
                    continue
                    
                song = await process_song(yt_music, track, main_song.video_id, index)
                if song:
                    send_song_info(song, session_id, index)
        
        # Signal completion
        event_queues[session_id].put(("complete", {"message": "Related songs processing complete"}))
        
    except Exception as e:
        logger.error(f"Error processing related songs: {e}")
        event_queues[session_id].put(("error", {"message": str(e)}))

def run_async_processing(query: str, session_id: str):
    """Run async processing in a separate thread"""
    async def async_wrapper():
        await process_related_songs(query, session_id)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_wrapper())
    loop.close()

def get_yt_dlp_options():
    """Get yt-dlp options with enhanced anti-detection measures"""
    return {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'quiet': True,
        'extractaudio': True,
        'no_warnings': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio', 
            'preferredcodec': 'mp3', 
            'preferredquality': '192'
        }],
        # Enhanced anti-detection headers
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        },
        # Additional options to avoid detection
        'sleep_interval': 1,
        'max_sleep_interval': 3,
        'sleep_interval_requests': 1,
        'sleep_interval_subtitles': 1,
        # Retry options
        'retries': 3,
        'fragment_retries': 3,
        'skip_unavailable_fragments': True,
        # Use cookies if available (you can set this up)
        'cookiefile': None,  # You can add a cookie file path here
        # Extractor options
        'extractor_retries': 3,
        'file_access_retries': 3,
    }

def fetch_song_details_fallback(song_name):
    """Fallback method using YTMusic only without yt-dlp"""
    try:
        # Check if the input is a YouTube URL
        yt_url_pattern = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)"
        match = re.search(yt_url_pattern, song_name)
        
        if match:
            video_id = match.group(1)
        else:
            search_results = yt_music.search(song_name, filter='songs', limit=1)
            if not search_results:
                return None
            song_info = search_results[0]
            video_id = song_info.get('videoId')
        
        if not video_id:
            return None
        
        # Fetch detailed song information
        song_details = yt_music.get_song(video_id)
        
        # Extract title
        title = song_details.get('videoDetails', {}).get('title', 'Unknown Title')
        
        # Extract artist using 'author' field
        artist = song_details.get('videoDetails', {}).get('author', 'Unknown Artist')
        
        # Fetch thumbnails
        thumbnails = song_details.get('videoDetails', {}).get('thumbnail', {}).get('thumbnails', [])
        album_art = sorted(thumbnails, key=lambda x: (x.get('width', 0), x.get('height', 0)))[-1]['url'] if thumbnails else 'No album art found'
        
        # Return without audio URL for now
        return {
            'title': title,
            'artists': artist,
            'albumArt': album_art,
            'audioUrl': f'https://www.youtube.com/watch?v={video_id}',  # YouTube URL as fallback
            'videoId': video_id,
            'fallback_mode': True
        }
    
    except Exception as e:
        logger.error(f"Error in fallback method: {e}")
        return None

def fetch_song_details(song_name, max_retries=3):
    """Enhanced song details fetching with multiple fallback strategies"""
    for attempt in range(max_retries):
        try:
            # Add rate limiting delay
            rate_limit_delay()
            
            # Check if the input is a YouTube URL
            yt_url_pattern = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)"
            match = re.search(yt_url_pattern, song_name)
            
            if match:
                video_id = match.group(1)
            else:
                search_results = yt_music.search(song_name, filter='songs', limit=1)
                if not search_results:
                    logger.warning(f"No search results found for: {song_name}")
                    return None
                song_info = search_results[0]
                video_id = song_info.get('videoId')
            
            if not video_id:
                logger.warning(f"No video ID found for: {song_name}")
                return None
            
            # Fetch detailed song information using YTMusic
            song_details = yt_music.get_song(video_id)
            
            # Extract title
            title = song_details.get('videoDetails', {}).get('title', 'Unknown Title')
            
            # Extract artist using 'author' field
            artist = song_details.get('videoDetails', {}).get('author', 'Unknown Artist')
            
            # Fetch thumbnails
            thumbnails = song_details.get('videoDetails', {}).get('thumbnail', {}).get('thumbnails', [])
            album_art = sorted(thumbnails, key=lambda x: (x.get('width', 0), x.get('height', 0)))[-1]['url'] if thumbnails else 'No album art found'
            
            # Try to fetch audio URL using yt-dlp with enhanced options
            try:
                ydl_opts = get_yt_dlp_options()
                
                with YoutubeDL(ydl_opts) as ydl:
                    info_dict = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                    audio_url = info_dict.get('url', None)
                
                if not audio_url:
                    raise Exception("No audio URL found")
                
                return {
                    'title': title,
                    'artists': artist,
                    'albumArt': album_art,
                    'audioUrl': audio_url,
                    'videoId': video_id,
                    'fallback_mode': False
                }
                
            except Exception as yt_dlp_error:
                logger.warning(f"yt-dlp failed (attempt {attempt + 1}): {yt_dlp_error}")
                
                # If this is the last attempt, return fallback response
                if attempt == max_retries - 1:
                    logger.info("Using fallback mode without direct audio URL")
                    return {
                        'title': title,
                        'artists': artist,
                        'albumArt': album_art,
                        'audioUrl': f'https://www.youtube.com/watch?v={video_id}',
                        'videoId': video_id,
                        'fallback_mode': True,
                        'message': 'Direct audio streaming unavailable, using YouTube link'
                    }
                
                # Wait before retry
                wait_time = (attempt + 1) * 2 + random.uniform(1, 3)
                logger.info(f"Waiting {wait_time:.1f} seconds before retry...")
                time.sleep(wait_time)
                continue
        
        except Exception as e:
            logger.error(f"Error fetching song details (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                # Try the complete fallback method
                return fetch_song_details_fallback(song_name)
            
            # Wait before retry
            wait_time = (attempt + 1) * 2 + random.uniform(1, 3)
            time.sleep(wait_time)
    
    return None

@app.route('/get_song', methods=['POST'])
def get_song():
    """
    Main endpoint for getting song details and initiating related songs processing.
    
    Expected request body:
    {
        "song_name": str,
        "username": str,
        "session_id": str (optional),
        "streamer_status": str (either 'yeshoustonstreamer' or 'nohoustonstreamer')
    }
    """
    try:
        data = request.json
        if not data:
            logger.error("No JSON data received in request")
            return jsonify({'error': 'No JSON data provided'}), 400

        # Extract and validate required fields
        song_name = data.get('song_name')
        username = data.get('username')
        session_id = data.get('session_id')
        streamer_status = data.get('streamer_status')

        # Detailed request logging
        logger.debug(f"Received request data: {data}")

        # Default isStreamer to False
        is_streamer = ''
        
        if streamer_status:
            # Check if the value corresponds to 'yeshoustonstreamer' or 'nohoustonstreamer'
            if streamer_status.lower() == 'yeshoustonstreamer':
                logger.info('yeshoustonstreamer')
                is_streamer = True
            elif streamer_status.lower() == 'nohoustonstreamer':
                logger.info('nonhoustonstreamer')
                is_streamer = False
            else:
                logger.warning(f"Invalid streamer_status value: {streamer_status}")

        logger.info(f"Final streamer mode value received: {is_streamer}")

        # Validate required fields
        if not song_name:
            logger.error("Missing song_name field")
            return jsonify({'error': 'Missing song_name field'}), 400
        if not username:
            logger.error("Missing username field")
            return jsonify({'error': 'Missing username field'}), 400

        # Generate or validate session_id
        session_id = session_id or str(uuid.uuid4())
        logger.debug(f"Using session_id: {session_id}")

        # Initialize streamer queue if needed
        if is_streamer:
            try:
                logger.debug(f"Attempting to create queue for session {session_id}")
                create_queue_for_session(session_id)
                logger.debug(f"Successfully created queue for session {session_id}")
            except Exception as e:
                logger.error(f"Failed to create queue for session {session_id}: {str(e)}")
                return jsonify({'error': 'Failed to initialize streamer queue'}), 500

        # Fetch song details with enhanced error handling
        try:
            logger.debug(f"Fetching song details for: {song_name}")
            song_details = fetch_song_details(song_name)
            logger.debug(f"Fetched song details: {song_details}")
        except Exception as e:
            logger.error(f"Failed to fetch song details: {str(e)}")
            return jsonify({'error': 'Failed to fetch song details'}), 500

        if not song_details:
            logger.warning(f"No results found for song: {song_name}")
            return jsonify({'error': 'No results found'}), 404

        # Add requester information
        song_details['requested_by'] = username

        # Start related songs processing for streamers
        if is_streamer:
            search_query = f"{song_details['title']} {song_details['artists']}"
            logger.debug(f"Starting related songs processing with query: {search_query}")
            thread = threading.Thread(
                target=run_async_processing,
                args=(search_query, session_id),
                name=f"RelatedSongs-{session_id}"
            )
            thread.daemon = True
            thread.start()
            logger.info(f"Started related songs processing for session {session_id}")

        # Prepare response
        response_data = {
            'song_details': song_details,
            'message': 'Song details sent successfully.',
            'session_id': session_id if is_streamer else None,
            'streamer_mode': is_streamer
        }
        
        logger.debug(f"Sending response: {response_data}")
        return jsonify(response_data), 200

    except Exception as e:
        logger.error(f"Unexpected error in get_song endpoint: {str(e)}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500

# Rest of your routes remain the same...
@app.route('/fetch_related_songs', methods=['POST'])
def fetch_related_songs_route():
    """
    Endpoint to fetch related songs based on title and artist.
    Streams results via SSE similar to get_song endpoint.
    
    Expected request body:
    {
        "title": str,
        "artist": str,
        "session_id": str
    }
    """
    try:
        data = request.get_json()
        logger.info(f"Incoming payload for fetch_related_songs: {data}")
        
        # Validate required fields
        required_fields = ['title', 'artist', 'session_id']
        if not data or any(field not in data for field in required_fields):
            logger.error("Missing required fields in payload")
            return jsonify({"error": "title, artist, and session_id are required"}), 400
        
        title = data['title']
        artist = data['artist']
        session_id = data['session_id']
        
        # Create or get existing SSE queue
        create_queue_for_session(session_id)
        
        # Construct search query similar to get_song
        search_query = f"{title} {artist}"
        logger.debug(f"Constructed search query: {search_query}")

        # Start related songs processing in background thread
        # Using the same processing function as get_song
        thread = threading.Thread(
            target=run_async_processing,
            args=(search_query, session_id),
            name=f"RelatedSongs-{session_id}"
        )
        thread.daemon = True
        thread.start()
        
        logger.info(f"Started related songs processing for session {session_id}")
        
        return jsonify({
            "status": "success",
            "message": "Related songs processing initiated",
            "session_id": session_id
        }), 202  # 202 Accepted indicates the request is being processed

    except Exception as e:
        logger.error(f"Error in fetch_related_songs_route: {str(e)}", exc_info=True)
        if session_id in event_queues:
            event_queues[session_id].put(("error", {"message": f"Processing failed: {str(e)}"}))
        return jsonify({"error": "Internal server error"}), 500

@app.route('/stream_related_songs/<session_id>')
def stream_related(session_id):
    """SSE endpoint for streaming related songs"""
    if session_id not in event_queues:
        return jsonify({'error': 'Invalid session ID'}), 404

    def generate():
        while True:
            try:
                event_type, data = event_queues[session_id].get(timeout=tO)
                yield format_sse(json.dumps(data), event=event_type)
                if event_type in ["complete", "error"]:
                    # Clean up queue after completion
                    del event_queues[session_id]
                    break
            except queue.Empty:
                yield ': keepalive\n\n'
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        }
    )

@app.route('/fetchlyrics', methods=['GET'])
def fetch_lyrics():
    """
    Endpoint to fetch song lyrics using YTMusic API.
    Query Parameters:
      - title: The song title (required)
      - artist: The artist name (optional)
    """
    title = request.args.get('title')
    artist = request.args.get('artist')

    if not title:
        return jsonify({"error": "The 'title' parameter is required."}), 400

    try:
        query = title if not artist else f"{title} {artist}"
        search_results = yt_music.search(query, filter="songs")

        if not search_results:
            return jsonify({"error": "No songs found for the provided title and artist."}), 404

        song = search_results[0]
        video_id = song.get("videoId")

        if not video_id:
            return jsonify({"error": "Could not find a valid video ID for the song."}), 404

        watch_playlist = yt_music.get_watch_playlist(video_id)
        browse_id = watch_playlist.get("lyrics")

        if not browse_id:
            return jsonify({"error": "Lyrics not available for this song."}), 404

        lyrics_data = yt_music.get_lyrics(browse_id)

        if not lyrics_data:
            return jsonify({"error": "Lyrics not found for this song."}), 404

        response = {
            "lyrics": lyrics_data.get("lyrics", "Lyrics not available."),
            "has_timestamps": lyrics_data.get("hasTimestamps", False),
        }

        if lyrics_data.get("hasTimestamps", False):
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
        return jsonify({"error": "An error occurred while fetching lyrics.", "details": str(e)}), 500

# Ensure user_data directory exists
os.makedirs("user_data", exist_ok=True)

# Route to handle login or ban users
@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get("username")
    user_id = data.get("user_id")
    login_time = data.get("login_time")

    # Define user file path
    user_file = f"user_data/{user_id}.txt"

    # Check if user file exists
    if os.path.exists(user_file):
        # Check if user is banned
        with open(user_file, 'r') as file:
            lines = file.readlines()
            for line in lines:
                if line.startswith("Status: banned"):
                    return jsonify({"message": f"User {user_id} is banned"}), 403

    # Write or update user data
    with open(user_file, 'w') as file:
        file.write(f"Username: {username}\n")
        file.write(f"User ID: {user_id}\n")
        file.write(f"Login Time: {login_time}\n")
        
        # Check if the request includes a ban flag
        if data.get("ban") == True:
            file.write("Status: banned\n")
            return jsonify({"message": f"User {user_id} has been banned"}), 200
        else:
            file.write("Status: active\n")

    return jsonify({"message": f"User {user_id} login recorded"}), 200

# Route to get a user's info (for testing purposes)
@app.route('/user/<user_id>', methods=['GET'])
def get_user(user_id):
    user_file = os.path.join(USER_DATA_DIR, f"{user_id}.txt")
    if os.path.exists(user_file):
        with open(user_file, 'r') as file:
            user_data = file.read()
        return f"<pre>{user_data}</pre>", 200
    else:
        return jsonify({"error": "User not found"}), 404


@app.route("/")
def index():
    return "Server is running : houston says hi!

if __name__ == "__main__":
    logger.info("Starting server")
    app.run(debug=True, threaded=True)
