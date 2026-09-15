import os
import sys

# Eventlet monkey patch (safe wrapper for local/server environments)
try:
    if sys.platform == "darwin":
        os.environ.setdefault("EVENTLET_HUB", "selects")
    import eventlet
    eventlet.monkey_patch()
except Exception:
    pass

from app import create_app
from flask_cors import CORS

# Initialize Flask WSGI Application
app = create_app()

# Enable Cross-Origin Resource Sharing (CORS) for all frontend clients
CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    supports_credentials=True,
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "Accept"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"]
)

# Explicit WSGI entrypoints for Vercel / Gunicorn
application = app

# Root health-check endpoint for Vercel deployment verification
@app.route("/", methods=["GET"])
def root():
    return {
        "status": "success",
        "message": "Brifix Investor Backend API is running on Vercel",
        "version": "1.0.0"
    }

# Local / Dev server execution
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 6001))
    print(f"Starting Brifix backend server on port {port}...")
    try:
        from app.socket import socketio
        from app.socket import indexes
        socketio.run(app, host="0.0.0.0", port=port, debug=False)
    except Exception as e:
        print(f"Starting standard Flask server on port {port}... ({e})")
        app.run(host="0.0.0.0", port=port, debug=False)
