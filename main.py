import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time

import aiohttp
import aiohttp.web_request
import requests
from aiohttp import web
from dotenv import load_dotenv

from BoardManager import BoardManager
from github_auth import auth

load_dotenv(".env")

app = web.Application()
routes = web.RouteTableDef()

board = BoardManager()

with open('.jwt', 'r', encoding='utf-8') as d:
    jwt_data = json.load(d)

# Load config.json and verify that it is valid and fields aren't empty
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
except FileNotFoundError:
    print("Failed to load config.json. Please create a config.json file.")
    exit(1)

if config["repo_author"] == "" or config["repo_name"] == "" or config["check_name"] == "" or config["board_mount_point"] == "":
    print("One or more fields in config.json are empty. Please fill out all fields.")
    exit(1)

# Add initialization variables
queue = []

async def refresh_github_token():
    """Refresh the GitHub App JWT token every 8 minutes."""
    while True:
        auth()
        
        # Wait for 8 minutes before next refresh (JWT expires in 10 minutes)
        await asyncio.sleep(480)  # 480 seconds = 8 minutes

async def queue_runner():
    while True:
        if len(queue) > 0:
            data = queue.pop(0)
            # Run install_repo in a background thread and wait for it to complete
            await asyncio.to_thread(install_repo, data)
        else:
            await asyncio.sleep(1)

def verify_signature(payload_body, secret_token, signature_header):
    if not signature_header:
        raise web.HTTPForbidden(reason="x-hub-signature-256 header is missing!")
    print(secret_token)
    hash_object = hmac.new(secret_token.encode('utf-8'), msg=payload_body.encode('utf-8'), digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, signature_header):
        raise web.HTTPForbidden(reason="Request signatures didn't match!")

# Webhook endpoint
@routes.post("/webhook")
async def webhook(request: aiohttp.web_request.Request):
    verify_signature(await request.text(), os.getenv("WEBHOOK_SECRET"), request.headers.get("x-hub-signature-256"))

    data = await request.json()
    print(data)

    if "created" not in data["action"] and "synchronize" not in data["action"]:
        return web.json_response({"message": "Webhook received"})

    try:
        response = requests.get(data["pull_request"]["url"], timeout=30)
    except:
        pass

    response.raise_for_status()
    pr_data = response.json()

    action_id = queue_pr_check(pr_data["head"]["sha"], jwt_data["access_token"])

    try:
        queue.append({
            "url": data["pull_request"]["url"],
            "action_id": action_id,
            "pr_data": pr_data
        })
    except Exception as e:
        print(f"Error processing webhook: {e}")

    return web.json_response({"message": "Webhook received"})

def install_repo(install_data):
    # Get pull request data
    pr_data = install_data["pr_data"]
    
    try:
        start_pr_check(install_data["action_id"], jwt_data["access_token"])
    except Exception as e:
        print(f"Failed to start PR check: {e}")

    # Pull the repository
    repo_url = f"https://github.com/{pr_data["head"]["repo"]["full_name"]}"
    repo_branch = pr_data["head"]["ref"]

    repo_path = f"repos/{repo_branch}-{pr_data["head"]["sha"]}"
    if os.path.exists(repo_path):
        os.system(f"rm -rf {repo_path}")
    os.makedirs(repo_path)

    try:
        subprocess.run(["git", "clone", repo_url, repo_path], check=True)
        subprocess.run(["git", "checkout", pr_data["head"]["sha"]], cwd=repo_path, check=True)
        subprocess.run(["make", "install", f"BOARD_MOUNT_POINT={config['board_mount_point']}"], cwd=repo_path, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to install repository to board: {e}")
        fail_pr_check(install_data["action_id"], jwt_data["access_token"])
        return

    # Install repository to board
    print(f"Installed {pr_data["title"]} to PROVES Kit")

    test_board(install_data["action_id"], jwt_data["access_token"])

def queue_pr_check(head_sha, access_token):
    response = requests.post(f"https://api.github.com/repos/{config['repo_author']}/{config['repo_name']}/check-runs", headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json", 
        "X-GitHub-Api-Version": "2022-11-28"
    }, json={
        "head_sha": head_sha,
        "name": config["check_name"],
        "status": "queued"
    }, timeout=30)
    response.raise_for_status()
    return response.json()["id"]

def start_pr_check(check_run_id, access_token):
    response = requests.patch(f"https://api.github.com/repos/{config['repo_author']}/{config['repo_name']}/check-runs/{check_run_id}", headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }, json={"status": "in_progress"}, timeout=30)
    response.raise_for_status()

def fail_pr_check(check_run_id, access_token):
    response = requests.patch(f"https://api.github.com/repos/{config['repo_author']}/{config['repo_name']}/check-runs/{check_run_id}", headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }, json={"status": "completed", "conclusion": "failure"}, timeout=30)
    response.raise_for_status()

def finish_pr_check(check_run_id, access_token):
    response = requests.patch(f"https://api.github.com/repos/{config['repo_author']}/{config['repo_name']}/check-runs/{check_run_id}", headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }, json={"status": "completed", "conclusion": "success"}, timeout=30)
    response.raise_for_status()

def test_board(check_run_id, access_token):
    msgs = []
    timer_finished = False
    stop_timer = False
    success = False
    tried_ctrl_d = False
    
    def callback(data):
        nonlocal msgs
        print(data)
        msgs.append(data)
    
    def timer():
        nonlocal timer_finished
        timer_amt = 180
        while timer_amt > 0 and not stop_timer:
            time.sleep(1)
            timer_amt -= 1
        timer_finished = True
    
    # Start timer in a separate thread
    timer_thread = threading.Thread(target=timer)
    timer_thread.start()

    board.set_data_callback(callback)

    # Try to reload board at start so there isn't any false positives
    if not board.in_main:
        board.send_data(b"\x04")
    else:
        board.send_data(b"\x03")
        time.sleep(0.1)
        board.send_data(b"\x04")

    # Monitor board while timer has not finished
    while not timer_finished:
        if len(msgs) > 0:
            msg = msgs.pop(0)
            if "Setting Safe Sleep Mode" in msg.decode("utf-8"):
                success = True
                stop_timer = True
                break

            elif "Code done running." in msg.decode("utf-8"):
                if not tried_ctrl_d:
                    board.send_data(b"\x04")
                    tried_ctrl_d = True
                else:
                    stop_timer = True
                    break

        else:
            time.sleep(0.01)

    if success:
        finish_pr_check(check_run_id, access_token)
    else:
        fail_pr_check(check_run_id, access_token)

    timer_thread.join()
    board.set_data_callback(None)

app.add_routes(routes)

if __name__ == '__main__':
    # Create and run any constant tasks
    loop = asyncio.get_event_loop()
    loop.create_task(queue_runner())
    loop.create_task(refresh_github_token())

    web.run_app(app, loop=loop, host='0.0.0.0', port=8000)
