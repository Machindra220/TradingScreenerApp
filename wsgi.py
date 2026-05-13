from app import create_app
from waitress import serve
import sys
import io

# Force UTF-8 encoding for stdout and stderr to handle emojis in Windows services
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from app import create_app
from waitress import serve

# This call triggers the import of screener.py which contains the emoji
app = create_app()

if __name__ == "__main__":
    print("Starting server on http://127.0.0.1:5005")
    serve(app, host="127.0.0.1", port=5005)