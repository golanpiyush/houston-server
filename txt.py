from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
import time

def get_song_details(spotify_url):
    # Setup Chrome browser
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")  # Run in headless mode (without opening the browser)
    
    # Initialize the Chrome WebDriver
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    try:
        # Open the Spotify URL in the browser
        driver.get(spotify_url)
        
        # Wait for the page to load (you might need to adjust this wait time)
        time.sleep(3)  # You can increase the sleep time if the page takes longer to load
        
        # Extract the title and artist name
        title = driver.find_element(By.CSS_SELECTOR, 'h1 span').text
        artist = driver.find_element(By.CSS_SELECTOR, 'a[href*="spotify:artist"]').text
        
        return title, artist
    except Exception as e:
        print(f"Error: {e}")
        return None, None
    finally:
        # Close the driver after extracting data
        driver.quit()

# Example usage
if __name__ == "__main__":
    spotify_url = input("Enter Spotify song URL: ")
    title, artist = get_song_details(spotify_url)
    
    if title and artist:
        print(f"Song Title: {title}")
        print(f"Artist: {artist}")
    else:
        print("Could not extract song details.")
