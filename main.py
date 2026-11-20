"""
エントリーポイント。

起動コマンド:
  uvicorn main:app --reload
"""

from kotoyose.app import app  # noqa: F401
