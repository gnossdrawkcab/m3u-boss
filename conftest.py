# Presence of this file makes pytest treat the repo root as the rootdir and
# puts it on sys.path, so `import app.*` resolves when running `pytest`.
import os

os.environ.setdefault("M3U_BOSS_ADMIN_PASSWORD", "test-only-password")
