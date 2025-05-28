import os
import re
import uuid
import json
import queue
import atexit
import asyncio
import logging
import threading
from flask.cli import F
from typing import Dict # Import UUID to generate unique session IDs
from yt_dlp import YoutubeDL
from dataclasses import asdict
from ytmusicapi import YTMusic
from related_songs import process_song
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response


# Initialize Flask app
app = Flask(__name__)
DATA_FILE = "data.json"
pref8 = '320' # audio quality
ints = 11 # number of related songs to send to client
tO = 30 # timout for streaming sessions

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

def send_song_info(song_info, session_id: str, index=None, event_type="related_song"):
    """Send song info through the event queue"""
    if song_info and session_id in event_queues:
        song_dict = asdict(song_info)
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

def fetch_song_details(song_name_or_url):
    try:
        # Check if the input is a YouTube URL
        yt_url_pattern = r"(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)"
        match = re.search(yt_url_pattern, song_name_or_url)

        if match:
            video_id = match.group(1)
        else:
            search_results = yt_music.search(song_name_or_url, filter='songs', limit=1)
            if not search_results:
                return None
            song_info = search_results[0]
            video_id = song_info.get('videoId')

        if not video_id:
            return None

        # Fetch detailed song information
        song_details = yt_music.get_song(video_id)
        title = song_details.get('videoDetails', {}).get('title', 'Unknown Title')
        artists = ", ".join([artist['name'] for artist in song_details.get('artists', [])])
        thumbnails = song_details.get('videoDetails', {}).get('thumbnail', {}).get('thumbnails', [])
        album_art = sorted(thumbnails, key=lambda x: (x.get('width', 0), x.get('height', 0)))[-1]['url'] if thumbnails else 'No album art found'

        # Fetch audio URL using youtube_dl
        ydl_opts = {
            'format': 'bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'extractaudio': True,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
        }

        with YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            audio_url = info_dict.get('url', 'No audio URL found')

        return {
            'title': title,
            'artists': artists,
            'albumArt': album_art,
            'audioUrl': audio_url,
            'videoId': video_id
        }

    except Exception as e:
        logger.error(f"Error fetching song details: {e}")
        return None

@app.route('/get_song', methods=['POST'])
def get_song():
    """Main endpoint for getting song details and initiating related songs processing"""
    data = request.json
    song_name = data.get('song_name')
    username = data.get('username')
    session_id = data.get('session_id')

    # Generate session_id if not provided
    if not session_id:
        session_id = str(uuid.uuid4())  # Generate a unique session ID if not provided

    if not all([song_name, username]):
        return jsonify({'error': 'Missing required fields'}), 400

    # Create queue for this session
    create_queue_for_session(session_id)

    song_details = fetch_song_details(song_name)
    if song_details:
        song_details['requested_by'] = username

        # Start related songs processing in background
        search_query = f"{song_details['title']} {song_details['artists']}"
        thread = threading.Thread(
            target=run_async_processing,
            args=(search_query, session_id)
        )
        thread.daemon = True
        thread.start()

        response_data = {
            'song_details': song_details,
            'message': 'Song details sent successfully.',
            'session_id': session_id
        }
        return jsonify(response_data), 200

    return jsonify({'error': 'No results found'}), 404

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
    # Get query parameters
    title = request.args.get('title')
    artist = request.args.get('artist')

    if not title:
        return jsonify({"error": "The 'title' parameter is required."}), 400

    try:
        # Search for the song
        query = title if not artist else f"{title} {artist}"
        search_results = yt_music.search(query, filter="songs")

        if not search_results:
            return jsonify({"error": "No songs found for the provided title and artist."}), 404

        # Use the first result
        song = search_results[0]
        video_id = song.get("videoId")
        song_title = song.get("title")
        song_artist = ", ".join(artist.get("name") for artist in song.get("artists", []))

        if not video_id:
            return jsonify({"error": "Could not find a valid video ID for the song."}), 404

        # Get the browseId for lyrics using get_watch_playlist
        watch_playlist = yt_music.get_watch_playlist(video_id)
        browse_id = watch_playlist.get("lyrics")

        if not browse_id:
            return jsonify({"error": "Lyrics not available for this song."}), 404

        # Fetch the lyrics
        lyrics_data = yt_music.get_lyrics(browse_id)

        if not lyrics_data:
            return jsonify({"error": "Lyrics not found for this song."}), 404

        # Structure the response
        response = {
            # "song_title": song_title,
            # "song_artist": song_artist,
            "lyrics": lyrics_data.get("lyrics", "Lyrics not available."),
            # "source": lyrics_data.get("source", "Unknown"),
            "has_timestamps": lyrics_data.get("hasTimestamps", False),
        }

        # Include timestamps if available
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
    


# # Get Friends & Their Current Songs (Include Profile Pictures)
# @app.route("/friends", methods=["GET"])
# def get_friends():
#     data = load_data()
#     user_id = request.args.get("user_id")

#     if user_id not in data["users"]:
#         return jsonify({"error": "User not found"}), 404

#     user = data["users"][user_id]
#     friends = [
#         {
#             "user_id": friend_id,
#             "username": data["users"][friend_id]["username"],
#             "profile_picture": data["users"][friend_id]["profile_picture"],  # Include PFP
#             "current_song": data["users"][friend_id]["current_song"]
#         }
#         for friend_id in user["friends"]
#     ]

#     return jsonify({"friends": friends})

# # Get Users Who Are Not Friends (Exclude Profile Pictures)
# @app.route("/other-users", methods=["GET"])
# def get_other_users():
#     data = load_data()
#     user_id = request.args.get("user_id")

#     if user_id not in data["users"]:
#         return jsonify({"error": "User not found"}), 404

#     user_friends = set(data["users"][user_id]["friends"])
#     other_users = [
#         {
#             "user_id": other_id,
#             "username": data["users"][other_id]["username"],  # Only username
#             "current_song": data["users"][other_id]["current_song"]
#         }
#         for other_id in data["users"] if other_id != user_id and other_id not in user_friends
#     ]

#     return jsonify({"other_users": other_users})

# # Send Friend Request
# @app.route("/send-friend-request", methods=["POST"])
# def send_friend_request():
#     data = load_data()
#     request_data = request.json
#     sender_id = request_data.get("sender_id")
#     receiver_id = request_data.get("receiver_id")

#     if sender_id not in data["users"] or receiver_id not in data["users"]:
#         return jsonify({"error": "User not found"}), 404

#     receiver = data["users"][receiver_id]

#     if sender_id in receiver["friend_requests"]:
#         return jsonify({"message": "Request already sent"}), 400

#     receiver["friend_requests"].append(sender_id)
#     save_data(data)

#     return jsonify({"message": "Friend request sent"}), 200

# # Accept Friend Request (Max 3 Friends)
# @app.route("/accept-friend-request", methods=["POST"])
# def accept_friend_request():
#     data = load_data()
#     request_data = request.json
#     user_id = request_data.get("user_id")
#     sender_id = request_data.get("sender_id")

#     if user_id not in data["users"] or sender_id not in data["users"]:
#         return jsonify({"error": "User not found"}), 404

#     user = data["users"][user_id]

#     if sender_id not in user["friend_requests"]:
#         return jsonify({"error": "No friend request found"}), 400

#     if len(user["friends"]) >= 3 or len(data["users"][sender_id]["friends"]) >= 3:
#         return jsonify({"error": "Max 3 friends allowed"}), 400

#     user["friend_requests"].remove(sender_id)
#     user["friends"].append(sender_id)
#     data["users"][sender_id]["friends"].append(user_id)

#     save_data(data)
#     return jsonify({"message": "Friend request accepted"}), 200

# # Remove Friend
# @app.route("/remove-friend", methods=["DELETE"])
# def remove_friend():
#     data = load_data()
#     request_data = request.json
#     user_id = request_data.get("user_id")
#     friend_id = request_data.get("friend_id")

#     if user_id not in data["users"] or friend_id not in data["users"]:
#         return jsonify({"error": "User not found"}), 404

#     user = data["users"][user_id]

#     if friend_id not in user["friends"]:
#         return jsonify({"error": "Not friends"}), 400

#     user["friends"].remove(friend_id)
#     data["users"][friend_id]["friends"].remove(user_id)

#     save_data(data)
#     return jsonify({"message": "Friend removed"}), 200

# # Upload Profile Picture (Locally Stored)
# @app.route("/upload-profile-pic", methods=["POST"])
# def upload_profile_pic():
#     if "file" not in request.files or "user_id" not in request.form:
#         return jsonify({"error": "Missing file or user_id"}), 400

#     file = request.files["file"]
#     user_id = request.form["user_id"]
#     filename = f"{user_id}.jpg"
#     file_path = os.path.join(UPLOAD_FOLDER, filename)
    
#     file.save(file_path)

#     data = load_data()
#     if user_id in data["users"]:
#         data["users"][user_id]["profile_picture"] = f"/{UPLOAD_FOLDER}/{filename}"  # Relative path
#         save_data(data)

#     return jsonify({"message": "Profile picture updated", "file_path": f"/{UPLOAD_FOLDER}/{filename}"}), 200


#ends here

if __name__ == "__main__":
    logger.info("Starting server")
    app.run(debug=True, threaded=True)
