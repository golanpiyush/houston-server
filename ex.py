import requests
import random

API_KEY = "08c32ee754daf39c3d08bb321fcd0e09"  # Replace with your Last.fm API key
BASE_URL = "https://ws.audioscrobbler.com/2.0/"

def fetch_top_tracks_by_genre(genre, limit=100):
    """Fetch top tracks for a specific genre."""
    params = {
        "method": "tag.gettoptracks",
        "tag": genre,
        "api_key": API_KEY,
        "format": "json",
        "limit": limit
    }
    response = requests.get(BASE_URL, params=params)
    if response.status_code == 200:
        data = response.json()
        return data.get("tracks", {}).get("track", [])
    else:
        print(f"Error fetching data for genre: {response.status_code}")
        return []

def main():
    genre = input("Enter the genre: ").strip()  # e.g., 'rock', 'pop', 'electronic'
    print(f"\nFetching top tracks for genre: {genre}...\n")
    
    # Fetch a larger set of tracks
    tracks = fetch_top_tracks_by_genre(genre, limit=100)
    if not tracks:
        print("No tracks found for this genre.")
        return
    
    # Shuffle and filter tracks to prioritize diversity
    random.shuffle(tracks)  # Randomize order
    seen_artists = set()    # Track seen artists to avoid repetition
    diverse_tracks = []
    
    for track in tracks:
        track_name = track["name"]
        artist_name = track["artist"]["name"]
        if artist_name not in seen_artists:  # Add only unique artists
            diverse_tracks.append((track_name, artist_name))
            seen_artists.add(artist_name)
        if len(diverse_tracks) >= 20:  # Limit output to 20 tracks
            break
    
    print("Top Tracks:\n")
    for track_name, artist_name in diverse_tracks:
        print(f"{track_name} by {artist_name}")

if __name__ == "__main__":
    main()
