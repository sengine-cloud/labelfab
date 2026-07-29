"""Device failures, split by whether retrying could possibly help.

Retry *policy* deliberately lives in the agent's worker, not here: keeping the
device layer free of clocks and backoff makes it testable without either.
"""


class D30Error(Exception):
    """Base class for everything this layer raises."""

    #: Whether a caller may sensibly reconnect and try again.
    retryable = False


class D30ConnectError(D30Error):
    """Could not reach the printer, or it vanished mid-transfer.

    The overwhelmingly common cause is the D30 having auto-powered-off. Treat this
    as normal operating condition, not an error state.
    """

    retryable = True


class D30WriteTimeout(D30Error):
    """A write did not complete in time.

    On long strips this usually means the printer's buffer filled because writes
    outran the print head. Increase ``pace_factor`` before suspecting the link.
    """

    retryable = True


class D30ConfigError(D30Error):
    """The device configuration cannot be used -- a malformed address, say.

    Deliberately not a :class:`D30ConnectError`: that one means "the printer is away",
    which the worker is right to retry. This one will fail identically every time, so
    retrying it just hides a typo behind a reconnect loop.
    """


class D30NotReady(D30Error):
    """The device was used before ``connect()``. A programming fault."""


class D30GeometryError(D30Error):
    """The raster does not fit what the protocol or the tape can express.

    Never retryable: the same bytes will fail identically.
    """
