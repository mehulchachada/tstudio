"""vidclip: cut a piece out of a video file without re-encoding it."""


class ClipError(Exception):
    """A problem the user can fix: bad input, missing file, impossible range."""
