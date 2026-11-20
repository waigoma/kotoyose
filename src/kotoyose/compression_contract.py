"""圧縮の許可と runtime の観測保証を分離する契約。"""

from typing import Literal
from .execution_contract import ContractModel


class CompressionPolicy(ContractModel):
    """圧縮本文や Grant を含まない、admission 時に固定する許可。"""

    mode: Literal['runtime_managed'] = 'runtime_managed'
    actor: Literal['provider_runtime'] = 'provider_runtime'
    automatic: Literal['allowed'] = 'allowed'
    manual: Literal['unsupported'] = 'unsupported'
    events: Literal['partial'] = 'partial'
    context_usage: Literal['best_effort'] = 'best_effort'
