from ytmusicapi import YTMusic

# Initialize the YTMusic API client
ytmusic = YTMusic()

# Dynamic input for song title and artist name
song_title = 'Bhula Dena'
artist_name = 'Mustafa Zahid'

# Search for the song to get its details and artist
search_results = ytmusic.search(query=f"{song_title} {artist_name}", filter="songs")

if search_results:
    song_info = search_results[0]  # Get the first result
    print(f"Found song: {song_info['title']} by {artist_name}")

    # Initialize a set to collect related songs, avoiding duplicates
    related_songs = set()
    related_songs.add(f"{song_info['title']} by {artist_name}")  # Add the original song

    # Search for related artists or genres dynamically based on the song or artist
    related_artist_search_results = ytmusic.search(query=song_title, filter="songs")
    
    # Add songs with a different title and artist (to avoid repeating the original song)
    for result in related_artist_search_results:
        track_title = result["title"]
        track_artist = result["artists"][0]["name"]
        
        if track_title != song_title and track_artist != artist_name and len(related_songs) < 25:
            related_songs.add(f"{track_title} by {track_artist}")
    
    # Search related songs based on genre or tags associated with the song
    genre_search_results = ytmusic.search(query="EDM", filter="songs")  # or dynamically detect genre based on song
    for result in genre_search_results:
        track_title = result["title"]
        track_artist = result["artists"][0]["name"]
        
        # Add only new songs that aren't from the original artist
        if track_artist != artist_name and len(related_songs) < 25:
            related_songs.add(f"{track_title} by {track_artist}")

    # Output the results
    print("Related Songs (Up to 25):")
    for song in list(related_songs)[:25]:
        print("- " + song)
else:
    print("Song not found in search results.")
