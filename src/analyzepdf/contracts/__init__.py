"""把不同解析器的结果统一成同一种文档结果。"""

from .docling import AnalyzePDFAdapterError, adapt_analyzepdf_output
from .mineru import MinerUAdapterError, adapt_mineru_output
from .ppstructure import PPStructureAdapterError, adapt_ppstructure_output
from .validation import (
    CONTRACT_VERSION,
    ContractDocument,
    ContractValidationError,
    load_contract,
)

__all__ = [
    "CONTRACT_VERSION",
    "AnalyzePDFAdapterError",
    "ContractDocument",
    "ContractValidationError",
    "MinerUAdapterError",
    "PPStructureAdapterError",
    "adapt_analyzepdf_output",
    "adapt_mineru_output",
    "adapt_ppstructure_output",
    "load_contract",
]
