# """
# Standalone SmartAPI WebSocket feeder.
# Runs as a subprocess — completely isolated from eventlet.
# Outputs JSON tick data to stdout, one line per tick.
# """
# import sys
# import json
# import time
# import ssl
# import traceback


# def log(msg):
#     """Write a control event to stdout."""
#     print(json.dumps(msg), flush=True)


# def main():
#     if len(sys.argv) < 6:
#         log({"_event": "error", "message": f"Usage: {sys.argv[0]} auth_token api_key client_code feed_token tokens_json"})
#         return

#     auth_token = sys.argv[1]
#     api_key = sys.argv[2]
#     client_code = sys.argv[3]
#     feed_token = sys.argv[4]
#     tokens = json.loads(sys.argv[5])

#     log({"_event": "starting", "tokens": tokens, "client_code": client_code})

#     # ── Quick auth test first ──
#     try:
#         # import websocket

#         url = "wss://smartapisocket.angelone.in/smart-stream"
#         headers = {
#             "Authorization": auth_token,
#             "x-api-key": api_key,
#             "x-client-code": client_code,
#             "x-feed-token": feed_token,
#         }

#         test_result = {"connected": False, "error": None}

#         def on_test_open(ws):
#             test_result["connected"] = True
#             ws.close()

#         def on_test_error(ws, error):
#             test_result["error"] = str(error)

#         def on_test_close(ws, code, msg):
#             pass

#         test_ws = websocket.WebSocketApp(
#             url, header=headers,
#             on_open=on_test_open,
#             on_error=on_test_error,
#             on_close=on_test_close,
#         )

#         import threading
#         timer = threading.Timer(10, lambda: test_ws.close())
#         timer.daemon = True
#         timer.start()
#         test_ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
#         timer.cancel()

#         if test_result["error"]:
#             log({"_event": "auth_failed", "message": str(test_result["error"])})
#             if "401" in str(test_result["error"]) or "Authentication Failed" in str(test_result["error"]):
#                 log({"_event": "fatal_error", "message": "Feed token expired or invalid. Please re-login to Angel One."})
#                 return
#         elif not test_result["connected"]:
#             log({"_event": "auth_timeout", "message": "Connection timed out"})
#             return
#         else:
#             log({"_event": "auth_ok", "message": "Authentication successful"})

#     except Exception as e:
#         log({"_event": "test_error", "message": str(e)})

#     # ── Start real SmartAPI WebSocket ──
#     try:
#         from SmartApi.smartWebSocketV2 import SmartWebSocketV2
#     except ImportError as e:
#         log({"_event": "error", "message": f"Failed to import SmartAPI: {e}"})
#         return

#     ws = None
#     tick_count = [0]

#     def on_data(wsapp, message):
#         try:
#             tick_count[0] += 1
#             print(json.dumps(message), flush=True)
#         except Exception as e:
#             log({"_event": "data_error", "message": str(e)})

#     def on_open(wsapp):
#         log({"_event": "connected", "timestamp": time.time()})
#         token_list = [{"exchangeType": 1, "tokens": tokens}]
#         ws.subscribe("feed_corr", 2, token_list)
#         log({"_event": "subscribed", "tokens": tokens, "mode": "QUOTE"})

#     def on_error(wsapp, error):
#         log({"_event": "error", "message": str(error)})

#     def on_close(wsapp):
#         log({"_event": "closed", "ticks_sent": tick_count[0], "timestamp": time.time()})

#     try:
#         ws = SmartWebSocketV2(
#             auth_token=auth_token,
#             api_key=api_key,
#             client_code=client_code,
#             feed_token=feed_token,
#             max_retry_attempt=10,
#             retry_strategy=1,
#             retry_delay=5,
#         )
#         ws.on_open = on_open
#         ws.on_data = on_data
#         ws.on_error = on_error
#         ws.on_close = on_close

#         log({"_event": "connecting"})
#         ws.connect()

#     except Exception as e:
#         log({"_event": "fatal_error", "message": str(e), "traceback": traceback.format_exc()})

#     log({"_event": "exited", "ticks_sent": tick_count[0]})


# if __name__ == "__main__":
#     main()
