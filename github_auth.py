import json
import os
import time

import aiohttp
import jwt


async def generate_jwt():
    with open("auth.pem", 'rb') as pem_file:
        signing_key = pem_file.read()

    payload = {
        # Issued at time
        'iat': int(time.time()),
        # JWT expiration time (10 minutes maximum)
        'exp': int(time.time()) + 600,
        
        # GitHub App's client ID
        'iss': os.getenv("GITHUB_CLIENT_ID")
    }

    encoded_jwt = jwt.encode(payload, signing_key, algorithm='RS256')

    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"https://api.github.com/orgs/{os.getenv("GITHUB_ORG")}/installation",
            headers={
                "Authorization": f"Bearer {encoded_jwt}"
            }
        ) as response:
            token_data = await response.json()
            print(token_data)

            with open('.jwt', 'w', encoding='utf-8') as f:
                json.dump({
                    'jwt': encoded_jwt,
                    'installation_id': token_data['id']
                }, f)
