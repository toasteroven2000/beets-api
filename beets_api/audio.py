"""Audio files, served so a browser can play them.

Only files inside a Roots are served (the music directory for an Item, the
Import Paths for a file waiting to be imported), and only ones that look like
audio, so this can't be used to read anything else. Starlette's FileResponse
answers Range requests, which a browser needs to seek.
"""

import os

from fastapi import HTTPException, responses

from .roots import Roots

# What a browser may be told it is playing; beets reads more, but only these
# have a type an <audio> element can use.
AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg; codecs=opus",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}


def audio_response(path: str | bytes, roots: Roots | None, outside: str) -> responses.FileResponse:
    """The file as a response, or the HTTPException that says why not."""
    path = os.fsdecode(path)
    if roots is None or not roots.contains(path):
        raise HTTPException(403, outside)
    media_type = AUDIO_TYPES.get(os.path.splitext(path)[1].lower())
    if media_type is None:
        raise HTTPException(415, "not an audio file")
    if not os.path.isfile(path):
        raise HTTPException(404, "file not found")
    return responses.FileResponse(path, media_type=media_type)
