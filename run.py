import os
import sys

# On macOS, eventlet's monkey patching does not patch select.kqueue/kevent, which causes
# Python's KqueueSelector to raise: TypeError: changelist must be an iterable of select.kevent objects
# during PyMongo socket polling and DNS lookups. Setting DefaultSelector to SelectSelector
# and EVENTLET_NO_GREENDNS to 'yes' ensures 100% stable networking and zero DNS timeouts.
if sys.platform == "darwin":
    import selectors
    selectors.DefaultSelector = selectors.SelectSelector
    os.environ["EVENTLET_NO_GREENDNS"] = "yes"
    os.environ.setdefault("EVENTLET_HUB", "selects")

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

if __name__ == "__main__":
    print("Starting Flask-SocketIO server with eventlet...")
    socketio.run(
        app,
        host='0.0.0.0',
        port=6001,
        debug=False,
        use_reloader=False,
        # log_output=True
    )
