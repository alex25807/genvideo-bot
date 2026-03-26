from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VideoRequest:
    provider: str
    prompt: str
    model: str
    seconds: int
    size: str
    input_reference_path: Optional[str] = None
    input_reference_paths: Optional[list[str]] = None
    reference_image_type: Optional[str] = None
    user_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ProviderJobRef:
    provider: str
    external_id: str
    status: str = "queued"
    metadata: dict = field(default_factory=dict)


@dataclass
class ProviderStatus:
    status: str
    progress: int = 0
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)


@dataclass
class ProviderResult:
    file_path: str
    raw: dict = field(default_factory=dict)


@dataclass
class ProviderCapabilities:
    provider: str
    supports_remix: bool
    supports_input_reference: bool
    allowed_seconds: tuple[int, ...]
    allowed_models: tuple[str, ...]
    allowed_sizes: tuple[str, ...]
    supports_reference_images: bool = False
    max_reference_images: int = 0
    supported_reference_types: tuple[str, ...] = ()
    is_stub: bool = False

