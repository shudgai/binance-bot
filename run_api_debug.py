import uvicorn
from api import app
import signal
import sys
import os

def handle_sigterm(signum, frame):
    with open("sigterm.log", "a") as f:
        f.write(f"Received SIGTERM from somewhere! PID: {os.getpid()}\n")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8005)
