import subprocess
import time
import socket
import os
from pathlib import Path

# Dedicated Chrome profile for Project Sane
DEDICATED_PROFILE = str(
    Path.home() / "Library" / "Application Support" / "Google" / "ChromeProjectSane"
)

def is_port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0

def launch_native_chrome(port: int = 9223):
    """
    Launches the real Google Chrome executable natively via macOS subprocess.
    If it's already running on the debug port, it just skips launching.
    """
    if is_port_in_use(port):
        return  # Already running

    chrome_mac_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    
    if not os.path.exists(chrome_mac_path):
        raise FileNotFoundError(f"Google Chrome not found at {chrome_mac_path}")

    # Launch Chrome detached
    cmd = [
        chrome_mac_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={DEDICATED_PROFILE}",
        "--profile-directory=Profile 1",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    
    # Wait until the port is open to ensure Playwright can connect
    for _ in range(30):
        if is_port_in_use(port):
            return
        time.sleep(0.5)
        
    raise RuntimeError(f"Chrome failed to open debug port {port} after 15 seconds.")
