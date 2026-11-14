"""Who is making a request: the program (Client) and the person it acts for (Actor).

Both are asserted by the caller and recorded in the Audit. beets-api has no
authentication, so neither is proof: it relies on only trusted callers, such
as beets-ui behind a proxy that signs people in, being able to reach it.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException

MAX_ACTOR = 200


@dataclass(frozen=True)
class Caller:
    client: str | None = None
    actor: str | None = None


def clean_actor(value: str | None) -> str | None:
    """The Actor with surrounding space removed; refused if it is long or has control characters."""
    if value is None or not (value := value.strip()):
        return None
    if len(value) > MAX_ACTOR:
        raise HTTPException(400, f"X-Beets-API-Actor is longer than {MAX_ACTOR} characters")
    if any(not c.isprintable() for c in value):
        raise HTTPException(400, "X-Beets-API-Actor has control characters")
    return value


def caller(
    x_beets_api_client: Annotated[str | None, Header()] = None,
    x_beets_api_actor: Annotated[str | None, Header()] = None,
) -> Caller:
    return Caller(x_beets_api_client, clean_actor(x_beets_api_actor))
