from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
import tinytuya
import os
import threading
import colorsys
import json
from dotenv import load_dotenv
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Load environment variables
load_dotenv()

app = Flask(__name__)
CORS(app) # Enable CORS for all routes

# Spotify OAuth Configuration
# Ensure SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI are set in .env
spotify_oauth = SpotifyOAuth(
    client_id=os.getenv("SPOTIPY_CLIENT_ID", "placeholder"),
    client_secret=os.getenv("SPOTIPY_CLIENT_SECRET", "placeholder"),
    redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://localhost:5000/api/spotify/callback"),
    scope="user-modify-playback-state user-read-playback-state",
    cache_path=".spotipyoauthcache"
)

class BulbController:
    def __init__(self):
        self.device_id = os.getenv('TUYA_DEVICE_ID', 'placeholder')
        self.device_ip = os.getenv('TUYA_DEVICE_IP', 'placeholder')
        self.local_key = os.getenv('TUYA_LOCAL_KEY', 'placeholder')
        self.version = float(os.getenv('TUYA_VERSION', '3.3'))
        
        # Thread lock to prevent simultaneous socket writes
        self.lock = threading.Lock()
        
        self.bulb = tinytuya.BulbDevice(
            self.device_id, 
            self.device_ip, 
            self.local_key
        )
        self.bulb.set_version(self.version)
        
        # STABILITY FIX: Keep the TCP connection open to prevent handshake lag
        self.bulb.set_socketPersistent(True)

controller = BulbController()

def decode_tuya_color(color_data):
    """Converts Tuya's HSV string (V1 Hex or V2 JSON) to a standard HEX code."""
    if not color_data:
        return "#ffffff" 
    
    try:
        # Check if it is the new V2 JSON format (e.g., Wipro Bulbs)
        if '{' in color_data and '}' in color_data:
            hsv = json.loads(color_data)
            # Tuya V2 JSON uses 0-360 for Hue, 0-1000 for Saturation and Value
            h = hsv.get('h', 0) / 360.0
            s = hsv.get('s', 1000) / 1000.0
            v = hsv.get('v', 1000) / 1000.0
        else:
            # Fallback for old V1 format (HHHHSSSSVVVV)
            h = int(color_data[0:4], 16) / 360.0
            s = int(color_data[4:8], 16) / 1000.0
            v = int(color_data[8:12], 16) / 1000.0
            
        # Convert to standard RGB (0.0 to 1.0)
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        
        # Convert to 0-255 range and format as HEX
        r, g, b = int(r * 255), int(g * 255), int(b * 255)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception as e:
        print(f"Color decode error: {e}")
        return "#ffffff"

@app.route('/', methods=['GET'])
def index():
    return jsonify({"status": "ATHENA Lighting Control API is running"})

@app.route('/ping', methods=['GET', 'HEAD'])
def ping():
    return "OK", 200

@app.route('/api/bulb/toggle', methods=['POST'])
def toggle_bulb():
    try:
        data = request.json
        state = data.get('state', False)
        
        with controller.lock:
            if state:
                controller.bulb.turn_on(nowait=True)
            else:
                controller.bulb.turn_off(nowait=True)
            
        return jsonify({"success": True, "state": state})
    except Exception as e:
        return jsonify({"error": str(e), "message": "Failed to toggle bulb"}), 500

@app.route('/api/bulb/color', methods=['POST'])
def set_color():
    try:
        data = request.json
        r = data.get('r', 255)
        g = data.get('g', 255)
        b = data.get('b', 255)
        hex_color = data.get('hex', '').lower()
        
        with controller.lock:
            # 1. The White Hardware Fix
            # If the user asks for any white variant, route to the dedicated White LEDs
            white_hexes = ['#ffffff', '#fffaf0', '#fdf5e6', '#f0ffff', '#e0f7fa']
            
            if hex_color in white_hexes:
                controller.bulb.set_mode('white', nowait=True)
            else:
                # 2. The Gold/Orange Hardware Fix
                # Compensate for overpowering Red LEDs by forcing higher Green values
                if hex_color == '#ffd700':  # Gold
                    r, g, b = 255, 255, 0   # Force max green to balance the red
                elif hex_color == '#ffa500': # Orange
                    r, g, b = 255, 180, 0   # Boost green to separate it from pure red
                    
                controller.bulb.set_colour(r, g, b, nowait=True)
            
        return jsonify({"success": True, "color": {"r": r, "g": g, "b": b}})
    except Exception as e:
        return jsonify({"error": str(e), "message": "Failed to set color"}), 500

@app.route('/api/bulb/brightness', methods=['POST'])
def set_brightness():
    try:
        data = request.json
        level = data.get('level', 100) # Expecting 1-100
        
        with controller.lock:
            # tinytuya set_brightness_percentage handles the conversion for us
            controller.bulb.set_brightness_percentage(level, nowait=True)
            
        return jsonify({"success": True, "brightness": level})
    except Exception as e:
        return jsonify({"error": str(e), "message": "Failed to set brightness"}), 500

@app.route('/api/bulb/status', methods=['GET'])
def get_status():
    try:
        with controller.lock:
            status = controller.bulb.status()
            
        if 'Error' in status:
            return jsonify({"error": "Device unreachable", "details": status}), 500
        
        dps = status.get('dps', {})
        
        # Standard Tuya Data Points
        power = dps.get('20', False)
        mode = dps.get('21', 'white') # 'white', 'colour', 'scene', 'music'
        tuya_color_string = dps.get('24', '') 
        
        # Decode the exact color set by the Tuya App
        current_hex = "#ffffff"
        if mode == 'colour' and tuya_color_string:
            current_hex = decode_tuya_color(tuya_color_string)
        
        return jsonify({
            "success": True, 
            "power": power,
            "mode": mode,
            "color_hex": current_hex,
            "raw_status": status
        })
    except Exception as e:
        return jsonify({"error": str(e), "message": "Failed to get status"}), 500

@app.route('/api/spotify/login', methods=['GET'])
def spotify_login():
    auth_url = spotify_oauth.get_authorize_url()
    return jsonify({"auth_url": auth_url})

@app.route('/api/spotify/callback', methods=['GET'])
def spotify_callback():
    code = request.args.get('code')
    if code:
        spotify_oauth.get_access_token(code)
        return "<h1>Successfully authenticated with Spotify!</h1><p>You can close this tab and return to ATHENA.</p>", 200
    return "Error: No code provided", 400

@app.route('/api/spotify/status', methods=['GET'])
def spotify_status():
    token_info = spotify_oauth.get_cached_token()
    return jsonify({"authenticated": bool(token_info)})

@app.route('/api/spotify/play', methods=['POST'])
def spotify_play():
    try:
        data = request.json
        query = data.get('query', '')
        
        token_info = spotify_oauth.get_cached_token()
        if not token_info:
            return jsonify({"error": "Not authenticated. Please authorize Spotify first."}), 401
            
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        # Search for the track
        results = sp.search(q=query, limit=1, type='track')
        if not results['tracks']['items']:
            return jsonify({"error": f"Could not find any track matching '{query}'"}), 404
            
        track_uri = results['tracks']['items'][0]['uri']
        track_name = results['tracks']['items'][0]['name']
        artist_name = results['tracks']['items'][0]['artists'][0]['name']
        
        # Start playback (requires an active Spotify device)
        sp.start_playback(uris=[track_uri])
        
        return jsonify({
            "success": True, 
            "message": f"Playing {track_name} by {artist_name}",
            "track": track_name,
            "artist": artist_name
        })
    except spotipy.exceptions.SpotifyException as e:
        # Check if the error is due to no active device
        if e.http_status == 404 and "NO_ACTIVE_DEVICE" in str(e):
             return jsonify({"error": str(e), "message": "No active Spotify device found. Please open Spotify on your phone or PC and try again."}), 404
        return jsonify({"error": str(e), "message": "Spotify API error."}), 500
    except Exception as e:
        return jsonify({"error": str(e), "message": "Failed to play Spotify track"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)