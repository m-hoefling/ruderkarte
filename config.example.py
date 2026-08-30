"""
Configuration template for API keys and sensitive settings.

Copy this file to 'config.py' and fill in your actual API keys.
The config.py file is NOT tracked in git for security reasons.
"""

# CartoDB Basemap API key
# Get your key from: https://basemaps.cartocdn.com/
# Free tier is available with attribution
CARTO_API_KEY = "your_api_key_here"

# Tile source configuration
# Provide a `TILE_SOURCE` dict to fully control the map tile layer used by
# generate_waterways_map.py. Keys:
#  - "tiles": URL template (required), e.g. "https://{s}.example.com/{z}/{x}/{y}.png"
#  - "attr": attribution string (recommended)
#  - "name": layer name shown in layer control (optional)
#  - other keys accepted by folium.TileLayer, e.g. "detect_retina", "max_zoom", "subdomains"
#
# Example 1: CARTO (uses the CARTO_API_KEY defined above)
# TILE_SOURCE = {
#     "tiles": f"https://basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png?key={CARTO_API_KEY}",
#     "attr": "© OpenStreetMap contributors & CartoDB",
#     "name": "Carto Voyager",
#     "detect_retina": True,
# }
#
# Example 2: Stadia Maps (replace YOUR_API_KEY with your key)
# TILE_SOURCE = {
#     "tiles": "https://tiles.stadiamaps.com/tiles/alidade_smooth/{z}/{x}/{y}{r}.png?api_key=YOUR_API_KEY",
#     "attr": "© Stadia Maps &copy; OpenStreetMap contributors",
#     "name": "Stadia Alidade Smooth",
#     "detect_retina": True,
# }
