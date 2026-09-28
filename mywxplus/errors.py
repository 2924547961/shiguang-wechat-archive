class MyWxPlusError(Exception):
    """Base error for mywxplus."""


class FeatureUnavailableError(MyWxPlusError):
    """A public operation has no verified implementation for this version."""


class AmbiguousTargetError(MyWxPlusError):
    """A display name maps to multiple or no verified targets."""


class MediaUnavailableError(MyWxPlusError):
    """The requested media is absent from local storage and cannot be fetched."""


class SendStatusUnknown(MyWxPlusError):
    """A send action may have occurred, but its result cannot be established."""


class BackendError(MyWxPlusError):
    """A local database, GUI, or OCR backend failed."""
