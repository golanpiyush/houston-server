import asyncio
import concurrent.futures
from ytmusicapi import YTMusic
import yt_dlp
from yt_dlp import YoutubeDL
import re
from dataclasses import dataclass
from typing import List, Optional
import aiohttp
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
ytmusic = YTMusic()

@dataclass
class SongInfo:
    title: str
    artists: List[str]
    video_id: str
    album_art_url: Optional[str] = None
    audio_url: Optional[str] = None
    featuring: List[str] = None

# Keywords to skip in related songs
skip_keywords = [
    "Latest", "Cover", "Remix", "Extended", "version", "Karaoke", 
    "Lyrical", 'Instrumental', 'Audio', 'Official Music Video'
]

def extract_featured_artists(title: str) -> List[str]:
    """Extract featured artists from the song title."""
    pattern = r"\((feat\.|ft\.|featuring)\s*([^\)]+)\)"
    matches = re.findall(pattern, title, re.IGNORECASE)
    return [match[1].strip() for match in matches]

def is_unwanted_title(title: str) -> bool:
    """Check if the title should be skipped."""
    title_lower = title.lower()
    
    if any(keyword.lower() in title_lower for keyword in skip_keywords):
        return True
        
    if '|' in title:
        segments = [segment.strip().lower() for segment in title.split('|')]
        if any(keyword.lower() in segments for keyword in skip_keywords):
            return True
            
    return title_lower.startswith(("lyrical:", "audio:", "official music video:"))

def get_best_thumbnail(thumbnails: List[dict]) -> Optional[str]:
    """Get the highest quality thumbnail URL from a list of thumbnails."""
    if not thumbnails:
        return None
    return max(thumbnails, key=lambda x: x.get('width', 0)).get('url')

async def get_audio_url(video_id: str) -> Optional[str]:
    """Extract high-quality audio URL using yt-dlp."""
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False
    }
    
    try:
        with YoutubeDL(ydl_opts) as ydl:
            loop = asyncio.get_event_loop()
            # Use run_in_executor to run the blocking extract_info function asynchronously
            info = await loop.run_in_executor(
                None, 
                lambda: ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            )
            # Return the audio URL from the extracted info
            return info.get('url') if info else None
    except Exception as e:
        logger.error(f"Error extracting audio URL for video {video_id}: {str(e)}")
        return None
async def search_song(ytmusic: YTMusic, video_id: str) -> Optional[dict]:
    """Search for a song by video ID to get thumbnails."""
    try:
        loop = asyncio.get_event_loop()
        search_result = await loop.run_in_executor(
            None,
            lambda: ytmusic.search(video_id, filter="songs", limit=1)
        )
        if search_result:
            return search_result[0]
        return None
    except Exception as e:
        logger.error(f"Error searching for song {video_id}: {str(e)}")
        return None

def print_song_info(song: SongInfo, index: Optional[int] = None):
    """Print song information in a consistent format."""
    prefix = f"\n{index}. " if index is not None else "\n"
    print(f"{prefix}{song.title}")
    print(f"   Artists: {', '.join(song.artists)}")
    if song.featuring:
        print(f"   Featuring: {', '.join(song.featuring)}")
    # print(f"   Album Art URL: {song.album_art_url}")
    # print(f"   Audio URL: {song.audio_url}")

async def process_song(ytmusic: YTMusic, song_data: dict, main_song_id: str = None, index: Optional[int] = None) -> Optional[SongInfo]:
    """Process a single song's data, print its info immediately, and return SongInfo object."""
    try:
        title = song_data.get('title')
        video_id = song_data.get('videoId')
        artists = [artist['name'] for artist in song_data.get('artists', [])]
        
        if not all([title, video_id, artists]):
            return None
            
        if video_id == main_song_id or is_unwanted_title(title):
            return None
            
        featuring = extract_featured_artists(title)
        
        # Get song details to fetch high-quality album art
        loop = asyncio.get_event_loop()
        song_details = await loop.run_in_executor(
            None,
            lambda: ytmusic.get_song(video_id)
        )
        
        # Extract album art from song details
        thumbnails = song_details.get('videoDetails', {}).get('thumbnail', {}).get('thumbnails', [])
        album_art_url = (
            sorted(thumbnails, key=lambda x: (x.get('width', 0), x.get('height', 0)))[-1]['url']
            if thumbnails else 'No album art found'
        )

        # Get the audio URL
        audio_url = await get_audio_url(video_id)

        song_info = SongInfo(
            title=title,
            artists=artists,
            video_id=video_id,
            album_art_url=album_art_url,
            audio_url=audio_url,
            featuring=featuring
        )
        
        # Print song info immediately after processing
        print_song_info(song_info, index)
        
        return song_info
    except Exception as e:
        logger.error(f"Error processing song: {str(e)}")
        return None

async def fetch_related_songs(ytmusic: YTMusic, video_id: str, main_song_id: str, max_results: int = 10) -> List[SongInfo]:
    """Fetch and process related songs one by one, excluding the main song."""
    try:
        loop = asyncio.get_event_loop()
        related_data = await loop.run_in_executor(
            None,
            lambda: ytmusic.get_watch_playlist(videoId=video_id)
        )
        
        related_tracks = related_data.get('tracks', [])[:max_results]
        valid_results = []
        
        # Process songs one by one and skip the main song
        for index, track in enumerate(related_tracks, 1):
            print(f"Checking track {index}: {track.get('videoId')}")
            if track.get('videoId') == main_song_id:  # Skip the main song
                print(f"Skipping main song with ID: {main_song_id}")
                continue
            
            song = await process_song(ytmusic, track, video_id, index)
            if song:
                valid_results.append(song)
        
        return valid_results
    except Exception as e:
        logger.error(f"Error fetching related songs: {str(e)}")
        return []



async def process_query(ytmusic: YTMusic, query: str) -> None:
    """Main processing function for a search query."""
    try:
        loop = asyncio.get_event_loop()
        search_results = await loop.run_in_executor(
            None,
            lambda: ytmusic.search(query, filter="songs", limit=1)
        )
        
        if not search_results:
            logger.error(f"No results found for query: {query}")
            return
            
        main_song = await process_song(ytmusic, search_results[0])
        if not main_song:
            logger.error("Could not process main song")
            return
            
        print("\nMain Song:")
        print_song_info(main_song)
        
        print("\nFetching related songs...")
        related_songs = await fetch_related_songs(ytmusic, main_song.video_id, main_song.video_id)
        
        # Optionally, you can print or process the related_songs list here
        if related_songs:
            print("\nRelated Songs:")
            for song in related_songs:
                print_song_info(song)
            
    except Exception as e:
        logger.error(f"Error processing query: {str(e)}")

# async def main():
#     """Main entry point."""
#     ytmusic = YTMusic()
#     query = input("Enter a song query: ")
#     await process_query(ytmusic, query)

# if __name__ == "__main__":
#     asyncio.run(main())