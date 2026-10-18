"""Settings every suite shares with the images."""

import os

# The stable HTTP conventions for the httpx and FastAPI spans (ADR 0008), as both images set it.
# Read once, when the first client or app is instrumented.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
