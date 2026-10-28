"""Start the CafeBooks desktop app (also the entry point for the packaged .exe)."""
import sys

from cafebooks.__main__ import load_dotenv
from cafebooks.desktop import main

if __name__ == "__main__":
    load_dotenv()
    sys.exit(main())
