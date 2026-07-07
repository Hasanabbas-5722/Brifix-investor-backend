import eventlet
eventlet.monkey_patch()

from app import create_app
from app.socket import socketio
from flask_cors import CORS


# Import socket handlers to register them
from app.socket import indexes

app = create_app()

CORS(app, origins=[
    "*"
])

@app.route("/health", methods=['GET'])
def Health():
    return {
        "service": "Brifix-Investor-Backend",
        "Status": "Running",
    },200

if __name__ == "__main__":
    print("Starting Flask-SocketIO server with eventlet...")
    socketio.run(
        app,
        host='0.0.0.0',
        port=6001,
        debug=True,
        use_reloader=True,
        # log_output=True
    )
