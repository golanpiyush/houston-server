import requests

API_KEY = '08c32ee754daf39c3d08bb321fcd0e09'  # Replace with your Last.fm API key

def fetch_songs_by_title_and_artist(song_title, artist_name):
    try:
        # Step 1: Search for the song by title and artist
        search_url = f"http://ws.audioscrobbler.com/2.0/"
        search_params = {
            'method': 'track.search',
            'track': song_title,
            'artist': artist_name,
            'api_key': API_KEY,
            'format': 'json',
            'limit': 1  # Limit to 1 to find the most popular track
        }

        search_response = requests.get(search_url, params=search_params)
        search_data = search_response.json()

        # Check if any tracks were found
        if 'results' in search_data and 'trackmatches' in search_data['results']:
            tracks = search_data['results']['trackmatches']['track']
            if tracks:
                # Get the most popular track
                main_track = tracks[0]
                main_track_title = main_track['name']
                main_track_artist = main_track['artist']
                
                # Step 2: Fetch related songs for the most popular track
                similar_url = f"http://ws.audioscrobbler.com/2.0/"
                similar_params = {
                    'method': 'track.getsimilar',
                    'track': main_track_title,
                    'artist': main_track_artist,
                    'api_key': API_KEY,
                    'format': 'json',
                    'limit': 5  # Limit to 5 related songs
                }

                similar_response = requests.get(similar_url, params=similar_params)
                similar_data = similar_response.json()

                # Process the response for related songs
                if 'similartracks' in similar_data and 'track' in similar_data['similartracks']:
                    related_tracks = similar_data['similartracks']['track']
                    print("\nRelated Songs:")
                    for track in related_tracks:
                        title = track['name']
                        artist = track['artist']['name']
                        print(f"Title: {title}, Artist: {artist}")
                else:
                    print("No related songs found.")
            else:
                print("No tracks found with that title and artist.")
        else:
            print("No results found in search.")

    except Exception as e:
        print(f"Error fetching related songs: {str(e)}")

# Example usage
fetch_songs_by_title_and_artist("Gunaah", "Jeet Gannguli")
