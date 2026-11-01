"""Testler gerçek /data dizinine yazmasın: app modülleri import edilmeden önce geçici DATA_DIR ayarlanır."""
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="metan-test-"))
