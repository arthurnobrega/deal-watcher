"""Store adapters.

Importing this package registers every bundled adapter. A new store needs a
module here plus an import below -- nothing else in the codebase changes.
"""

from .base import ParseError, StoreAdapter, available_stores, get_adapter, register
from .kabum import KabumAdapter
from .mercadolivre import MercadoLivreAdapter
from .pichau import PichauAdapter
from .terabyte import TerabyteAdapter

__all__ = [
    "KabumAdapter",
    "MercadoLivreAdapter",
    "ParseError",
    "PichauAdapter",
    "StoreAdapter",
    "TerabyteAdapter",
    "available_stores",
    "get_adapter",
    "register",
]
