import os
from google import genai
from dotenv import load_dotenv

# Using your environment variable for safety
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

print("🔍 Searching for Batch-compatible models...")
for m in client.models.list():
    # The SDK uses 'supported_actions' and the action name is 'batchGenerateContent'
    if 'batchGenerateContent' in m.supported_actions:
        print(f"✅ Model: {m.name}")
