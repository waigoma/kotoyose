"""Wire 上の workspace identity と世代を一箇所で解釈する。"""

from .execution_resources import ExecutionError


def parse_workspace_ref(ref: str) -> tuple[str, int]:
    """負の世代や別種の ref を拒み、所有照合に使う identity を返す。"""
    try:
        prefix, identity, generation = ref.split(':')
        value = int(generation)
        if prefix != 'workspace' or not identity or value < 0 or str(value) != generation:
            raise ValueError()
        return identity, value
    except ValueError as exc:
        raise ExecutionError('invalid_request', 'Invalid workspace reference') from exc
