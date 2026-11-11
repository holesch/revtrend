import os

# Makes asyncio report slow callbacks, e.g. blocking calls in async code.
os.environ.setdefault("PYTHONASYNCIODEBUG", "1")
